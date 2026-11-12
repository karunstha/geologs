"""Live location sharing with other people on this server.

You share with a person by their account's email, for an hour, until the end of today, or until
you stop it, at your exact position or only your town (about a kilometre). They see where you are
now, what the place is called and when your phone last reported; never your history. The
background job keeps one position per precision you share at, about once a minute, and removes
it once nothing is shared.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import ambient
from . import mail
from . import places as naming
from ..domain.places import POSSIBLE_STOP_PLACE_M
from .position import latest
from .stays import stays
from .use_case import use_case

PRECISIONS = ("exact", "approximate")
DURATIONS = ("hour", "today", "always")


def _person(user):
    return {"id": user["id"], "name": user.get("name") or None, "email": user.get("email")}


def _ends_at(duration, now, timezone_name):
    if duration == "hour":
        return now + timedelta(hours=1)
    if duration == "today":
        local = now.astimezone(ZoneInfo(timezone_name))
        return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), local.tzinfo)
    return None


@use_case
def sharing_status(db):
    """Whom you share your location with, and who shares theirs with you (with where they are)."""
    db.sharing.forget_ended()
    given = []
    for share in db.sharing.given():
        viewer = db.users.get(share["viewer_id"])
        if viewer:
            given.append({"id": share["id"], "viewer": _person(viewer), "precision": share["precision"],
                          "expires_at": share["expires_at"].isoformat() if share["expires_at"] else None,
                          "created_at": share["created_at"]})
    received = []
    for share in db.sharing.received():
        sharer = db.users.get(share["sharer_id"])
        if not sharer:
            continue
        position = None
        if share["lat"] is not None:
            position = {"lat": share["lat"], "lon": share["lon"], "place_name": share["place_name"],
                        "moving": share["moving"], "fix_at": share["fix_at"], "updated_at": share["updated_at"]}
        received.append({"id": share["id"], "sharer": _person(sharer), "precision": share["precision"],
                         "expires_at": share["expires_at"].isoformat() if share["expires_at"] else None,
                         "position": position})
    return {"given": given, "received": received, "precisions": list(PRECISIONS), "durations": list(DURATIONS)}


@use_case
def share_location(db, raw, now=None):
    """Share your location with the person whose account has raw["email"]; sharing with them again
    changes the share. raw["precision"] is exact or approximate, raw["duration"] hour, today or always."""
    email = str(raw.get("email") or "").strip()
    precision = raw.get("precision") or "exact"
    duration = raw.get("duration") or "always"
    if precision not in PRECISIONS:
        raise ValueError("precision must be exact or approximate")
    if duration not in DURATIONS:
        raise ValueError("duration must be hour, today or always")
    viewer = db.users.by_email(email) if email else None
    if not viewer or viewer.get("disabled_at") or db.demo.is_demo(viewer["id"]):
        raise ValueError("No one on this server has that email")
    if viewer["id"] == db.user_id:
        raise ValueError("That is your own account")
    now = now or datetime.now(timezone.utc)
    ends = _ends_at(duration, now, db.settings.get()["timezone"])
    db.sharing.save(viewer["id"], precision, ends, now.isoformat(timespec="seconds"))
    refresh_live_position(db, now)
    sharer = db.users.get(db.user_id)
    who = (sharer.get("name") or sharer.get("email") or "Someone") if sharer else "Someone"
    what = "the town they are in" if precision == "approximate" else "where they are"
    until = {"hour": " for an hour", "today": " until the end of today", "always": ""}[duration]
    mail.queue(db, "location-share", viewer["email"], f"{who} shares their location with you",
               f"{who} shares {what} with you on GeoLogs{until}.\n\nSee it on the map: {mail.link('') or 'your GeoLogs server'}\n")
    return sharing_status(db)


@use_case
def stop_sharing(db, share_id):
    """Stop sharing your location with one person."""
    if not db.sharing.stop(share_id):
        raise ValueError("That share has already stopped")
    refresh_live_position(db)
    return sharing_status(db)


@use_case
def leave_share(db, share_id):
    """Stop seeing someone's location; they can share it with you again."""
    if not db.sharing.leave(share_id):
        raise ValueError("That share has already stopped")
    return sharing_status(db)


def _place_name(db, point):
    """What to call where you are: the place of the stay you are at (your own name for it, a saved
    place, or one already looked up nearby), or None while you are on the move."""
    ongoing = next((stay for stay in stays(db, start=point["timestamp"], source=point["source"], limit=5)
                    if stay["ongoing"] and stay["started_at"] <= point["timestamp"]), None)
    if not ongoing:
        return None, True
    if ongoing.get("place_name"):
        return ongoing["place_name"], False
    if ongoing.get("zone_name"):
        return ongoing["zone_name"], False
    # The lookup cache only: a share must not spend the place provider's quota.
    found = naming.named_place_near(db, ongoing["lat"], ongoing["lon"], POSSIBLE_STOP_PLACE_M, lookup=False)
    return (found or {}).get("place_name") or None, False


def _town(db, lat, lon):
    try:
        town = ambient.place_of(db, lat, lon)
    except Exception as exc:
        print(f"GeoLogs locality: {exc}", flush=True)
        return None
    return (town or {}).get("city") or (town or {}).get("region")


@use_case
def refresh_live_position(db, now=None):
    """Keep the position your shares show up to date (the background job runs this each minute):
    exact, named as above; approximate, rounded to about a kilometre and named by its town. With
    nothing shared, no position is kept."""
    db.sharing.forget_ended()
    precisions = db.sharing.precisions_shared()
    point = latest(db) if precisions else None
    if not point:
        db.sharing.keep_positions(())
        return None
    now = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    name, moving = _place_name(db, point)
    if "exact" in precisions:
        db.sharing.set_position("exact", point["lat"], point["lon"], name, moving, point["timestamp"], now)
    if "approximate" in precisions:
        lat, lon = round(point["lat"], 2), round(point["lon"], 2)
        db.sharing.set_position("approximate", lat, lon, _town(db, lat, lon), moving, point["timestamp"], now)
    db.sharing.keep_positions(precisions)
    return precisions
