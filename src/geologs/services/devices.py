"""Trackers: the tokens devices send with, and how each device's requests are going."""
import secrets
from datetime import datetime, timezone

from ..domain.access import ingest_token_hash
from ..domain.timing import parse_time
from .use_case import use_case


@use_case
def create_ingest_token(db, raw):
    """A token one device sends locations with, shown once.

    `source` locks it to one tracker name, or to every tracker of a kind
    when it ends with ":" (such as "home_assistant:").
    """
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 60:
        raise ValueError("name is required (max 60 characters)")
    source = str(raw.get("source") or "").strip() or None
    if source and len(source) > 80:
        raise ValueError("source must be at most 80 characters")
    token = "glp_" + secrets.token_urlsafe(24)
    token_id = db.devices.add_token(name, ingest_token_hash(token), token[:8], source, parse_time(None))
    return {**next(item for item in db.devices.tokens() if item["id"] == token_id), "token": token}


@use_case
def delete_ingest_token(db, token_id):
    if not db.devices.delete_token(token_id):
        raise ValueError("that token was already revoked")
    return {"deleted": int(token_id)}


@use_case
def ingest_token_for(db, token):
    """The stored device token a request presented, or None."""
    if not token or not token.startswith("glp_"):
        return None
    return db.devices.token_by_hash(ingest_token_hash(token))


@use_case
def note_ingest_token_use(db, token_id, source):
    db.devices.note_token_use(token_id, source, parse_time(None))


@use_case
def record_ingest(db, kind, status, message=None, device=None, accepted=0):
    """Remember the latest request from each device of each tracker app, so Settings
    can say whether a phone is getting through and, when it is not, why. `device` is
    the source the request names, or empty when it cannot be told apart."""
    db.devices.record_attempt(kind, device, status, message, accepted, parse_time(None))


@use_case
def delete_ingest_device(db, kind, device):
    """Forget a tracker app's device status row (a failed or retired phone); its samples stay."""
    if not db.devices.delete_device(kind, device):
        raise ValueError("that device is already gone")
    return {"deleted": True}


@use_case
def source_activity(db, now=None):
    """Every source that has sent locations: how much, the newest fix, and when it last arrived."""
    day_ago = parse_time((now or datetime.now(timezone.utc)).timestamp() - 86400)
    return {"sources": db.points.activity(day_ago), "devices": db.devices.devices()}
