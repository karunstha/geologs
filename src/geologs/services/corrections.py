"""Correcting what was detected: a trip's travel mode, and stays to add or remove."""
from datetime import datetime

from ..domain.corrections import mode_override_for, windows_overlap
from ..domain.motion import MOVEMENT_MODES
from ..domain.timing import parse_time
from .use_case import use_case


@use_case
def save_mode_override(db, raw):
    """Set (or with mode "auto", clear) the travel mode of one trip or area stay.

    Trips are recalculated from raw points, so a correction is keyed by
    source and time window, not an ID. Setting it again for the same trip
    replaces it.
    """
    source = str(raw.get("source") or "").strip()[:80]
    target = str(raw.get("target") or "trip")
    mode = str(raw.get("mode") or "").strip()
    if not source:
        raise ValueError("source is required")
    if target not in ("trip", "stay"):
        raise ValueError("target must be trip or stay")
    if mode != "auto" and mode not in MOVEMENT_MODES:
        raise ValueError("mode must be one of: auto, " + ", ".join(MOVEMENT_MODES))
    started_at, ended_at = parse_time(raw["started_at"]), parse_time(raw["ended_at"])
    if ended_at <= started_at:
        raise ValueError("ended_at must be after started_at")
    existing = mode_override_for(db.corrections.mode_overrides(), source, target, started_at, ended_at)
    if mode == "auto":
        if existing:
            db.corrections.delete_mode_override(existing["id"])
        result = None
    elif existing:
        db.corrections.update_mode_override(existing["id"], started_at, ended_at, mode, parse_time(None))
        result = existing["id"]
    else:
        result = db.corrections.add_mode_override(source, target, started_at, ended_at, mode, parse_time(None))
    db.caches.clear_analysis()
    return next((item for item in db.corrections.mode_overrides() if item["id"] == result),
                {"source": source, "target": target, "mode": "auto"})


@use_case
def save_stay_edit(db, raw):
    """Add a stop the detector missed, or remove a stay it should not have found.

    An added stop sits at the median position of its source's accurate
    samples in the window. Editing the same window again replaces the edit.
    """
    source = str(raw.get("source") or "").strip()[:80]
    action = str(raw.get("action") or "")
    if not source:
        raise ValueError("source is required")
    if action not in ("add", "remove"):
        raise ValueError("action must be add or remove")
    started_at, ended_at = parse_time(raw.get("started_at")), parse_time(raw.get("ended_at"))
    seconds = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds()
    if not 0 < seconds <= 86400:
        raise ValueError("a stop must end after it starts, within a day")
    lat = lon = None
    if action == "add":
        fixes = db.points.accurate_positions(source, started_at, ended_at)
        if not fixes:
            raise ValueError("no accurate samples from that device in that time; pick a window with samples")
        lat = sorted(fix["lat"] for fix in fixes)[len(fixes) // 2]
        lon = sorted(fix["lon"] for fix in fixes)[len(fixes) // 2]
    for edit in db.corrections.stay_edits(started_at, ended_at, source):
        if edit["action"] == action and windows_overlap(edit, started_at, ended_at):
            db.corrections.delete_stay_edit(edit["id"])
    result = db.corrections.add_stay_edit(source, action, started_at, ended_at, lat, lon, parse_time(None))
    db.caches.clear_analysis()
    return next(edit for edit in db.corrections.stay_edits(source=source) if edit["id"] == result)


@use_case
def delete_stay_edit(db, edit_id):
    """Undo an added or removed stop."""
    if not db.corrections.delete_stay_edit(edit_id):
        raise ValueError("that change was already undone")
    db.caches.clear_analysis()
    return {"deleted": int(edit_id)}
