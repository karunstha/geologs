"""Quiet trackers: warn when a phone stops sending (its app was closed, or lost location access).

Each tracker can be watched with a number of hours. The background worker checks every minute:
once a watched tracker's newest fix is older than that, one notice says it went quiet, and one more
says when it is back. Notices reach the browser (it polls them), the person's confirmed email when
this server can send mail, and the owner's webhook. Settings and state are the user's own
app_settings values, so they are held to the same row-level security as everything else.
"""
from datetime import datetime, timedelta, timezone

from .. import config
from ..data import OWNER_ID
from ..integrations import webhooks
from . import mail
from .use_case import use_case

WATCHED = "quiet_trackers"  # {source: {"hours": 6, "alerted_at": ISO time while it is quiet, else None}}
NOTICES = "tracker_notices"  # the newest notices, for the browser, newest last
KEEP_NOTICES = 20
HOURS = (2, 6, 12, 24, 48)


def tracker_label(source):
    """ "alex/phone (owntracks)" from "owntracks:alex/phone"; what the person would call it."""
    kind, _, device = str(source).partition(":")
    return f"{device} ({kind})" if device else kind


def _stamp(at):
    return at.isoformat(timespec="seconds")


@use_case
def quiet_alerts(db):
    return db.settings.value(WATCHED, {}) or {}


@use_case
def set_quiet_alert(db, raw):
    """Watch a tracker (`hours` from HOURS) or stop watching it (`hours` 0 or missing)."""
    source = str(raw.get("source") or "").strip()
    if not source or len(source) > 160:
        raise ValueError("source is required")
    hours = int(raw.get("hours") or 0)
    if hours and hours not in HOURS:
        raise ValueError(f"hours must be one of {', '.join(map(str, HOURS))}, or 0 to stop")
    watched = db.settings.value(WATCHED, {}) or {}
    if hours:
        if db.points.newest_fix(source) is None:
            raise ValueError("that tracker has not sent any locations")
        # A changed limit starts over: a tracker already quiet longer than it is told once, again.
        watched[source] = {"hours": hours, "alerted_at": None}
    else:
        watched.pop(source, None)
    db.settings.set_value(WATCHED, watched)
    return watched


@use_case
def tracker_notices(db, since_id=0):
    return [notice for notice in db.settings.value(NOTICES, []) or [] if notice["id"] > int(since_id or 0)]


def _quiet_message(name, label, hours, last_fix, url):
    return (f"{label} has gone quiet",
            f"Hi{' ' + name if name else ''},\n\nGeoLogs has had nothing from {label} for over {hours} hours: its newest "
            f"location is from {last_fix} (UTC).\n\nThe app may have been closed, or lost permission to read your "
            f"location. Open it on the phone to start it again.\n\n{url or ''}\n\n"
            "You get one message when it goes quiet and one when it is back. Change this in Settings → Devices.\n")


def _back_message(name, label, url):
    return (f"{label} is sending again",
            f"Hi{' ' + name if name else ''},\n\nGeoLogs is receiving locations from {label} again.\n\n{url or ''}\n")


@use_case
def _note_quiet_trackers(db, now):
    """Decide which watched trackers went quiet or came back, record their notices and queue their
    email; returns the new notices. Commits, so the webhook (slow, outside) is sent afterwards."""
    watched = db.settings.value(WATCHED, {}) or {}
    if not watched:
        return []
    notices = db.settings.value(NOTICES, []) or []
    next_id = max((notice["id"] for notice in notices), default=0) + 1
    user = db.users.get(db.user_id)
    email = user and user["email"] and user["email_verified_at"] and mail.can_send()
    new = []
    for source, watch in watched.items():
        last_fix = db.points.newest_fix(source)
        if last_fix is None:
            continue
        quiet = now - datetime.fromisoformat(last_fix) > timedelta(hours=watch["hours"])
        if quiet == bool(watch.get("alerted_at")):
            continue
        watch["alerted_at"] = _stamp(now) if quiet else None
        notice = {"id": next_id, "kind": "quiet" if quiet else "back", "source": source, "label": tracker_label(source),
                  "hours": watch["hours"], "last_fix": last_fix, "at": _stamp(now)}
        next_id += 1
        new.append(notice)
        if email:
            subject, body = (_quiet_message(user["name"], notice["label"], watch["hours"], last_fix, config.public_url())
                             if quiet else _back_message(user["name"], notice["label"], config.public_url()))
            mail.queue(db, "tracker " + notice["kind"], user["email"], subject, body)
    if new:
        db.settings.set_value(WATCHED, watched)
        db.settings.set_value(NOTICES, (notices + new)[-KEEP_NOTICES:])
    return new


def check_quiet_trackers(db, now=None):
    """The background worker's job, per user: note quiet and returning trackers, then tell the
    owner's webhook (the server owner's, so other users' trackers never go to it)."""
    notices = _note_quiet_trackers(db, now or datetime.now(timezone.utc))
    target = webhooks.url() if notices and db.user_id == OWNER_ID else None
    for notice in notices if target else ():
        webhooks.send(target, {"type": "tracker_" + notice["kind"], "source": notice["source"], "label": notice["label"],
                               "quiet_hours": notice["hours"], "last_fix_at": notice["last_fix"], "at": notice["at"]})
    return notices
