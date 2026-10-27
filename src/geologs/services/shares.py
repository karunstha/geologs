"""Links that show a day's replay to anyone holding them, frozen as the day was when made."""
from datetime import datetime

from ..domain.accounts import token_hash
from ..domain.shares import expiry, preview_image, share_options, share_token, shared_replay
from ..domain.timing import parse_time
from .replay import day_replay
from .use_case import use_case


@use_case
def create_share(db, payload):
    """A link to the replay of `date` (in `timezone`, of `source` or all), with what the options
    keep private left out (see shared_replay). The link is shown once: only its hash is kept."""
    date = str(payload.get("date") or "")
    options = share_options(payload)
    replay = day_replay(db, date, payload.get("timezone") or db.settings.get()["timezone"], payload.get("source"))
    snapshot = shared_replay(replay, options, db.settings.get()["units"])
    if not snapshot["moves"]:
        raise ValueError("Nothing is left to replay once the start, end and Home are hidden; hide less, or share another day")
    day = datetime.strptime(date, "%Y-%m-%d")
    title = options.pop("title") or f"{day:%A, %B} {day.day}"
    token, now = share_token(), parse_time(None)
    row = db.shares.add(token_hash(token), token[:4] + "…", title, date, options, snapshot, now,
                        expiry(now, options["expires_days"]))
    return {**row, "token": token, "path": "/s/" + token}


@use_case
def save_preview(db, payload):
    """The picture chat apps show for one of your links: your browser draws it from the link's own replay,
    so it shows only what the link does."""
    if not db.shares.set_preview(payload["id"], preview_image(payload.get("image"))):
        raise ValueError("no such link")
    return {"ok": True}


@use_case
def list_shares(db):
    return db.shares.mine()


@use_case
def delete_share(db, share_id):
    """Turn a link off: it stops working at once, for everyone."""
    if not db.shares.delete(share_id):
        raise ValueError("no such link")
    return {"ok": True}


def _usable(token):
    return bool(token) and len(token) <= 64


@use_case
def share_preview(db, token):
    """A link's picture (JPEG bytes), or None."""
    return db.shares.preview(token_hash(token), parse_time(None)) if _usable(token) else None


@use_case
def open_share(db, token):
    """What a link shows, or None once it is turned off or expired (or never was)."""
    if not _usable(token):
        return None
    return db.shares.shared(token_hash(token), parse_time(None))
