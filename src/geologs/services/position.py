"""Your latest position, from the tracker you trust most."""
from datetime import datetime, timezone

from ..domain.sources import PRIORITY_FRESH_SECONDS
from .use_case import use_case


@use_case
def latest(db):
    """Your latest position: the newest fix, unless a tracker ranked above it
    (Settings) reported within PRIORITY_FRESH_SECONDS of it."""
    rows = db.points.observations(limit=1)
    if not rows:
        return None
    point = rows[0]
    newest = datetime.fromisoformat(point["timestamp"])
    for source in db.settings.get()["source_priority"]:
        if source == point["source"]:
            break
        preferred = db.points.observations(source=source, limit=1)
        if preferred and (newest - datetime.fromisoformat(preferred[0]["timestamp"])).total_seconds() <= PRIORITY_FRESH_SECONDS:
            point = preferred[0]
            break
    point["id"] = point["point_id"]
    point["age_seconds"] = max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(point["timestamp"])).total_seconds()))
    point["stale"] = point["age_seconds"] > 900
    return point
