"""Your corrections to what was detected: travel modes and added or removed stays."""
from datetime import datetime


# A correction applies to a trip or area stay that overlaps its window by at
# least this share, so it survives trips being recalculated a minute either way.
MODE_OVERRIDE_MIN_OVERLAP = .5


def windows_overlap(item, started_at, ended_at, share=MODE_OVERRIDE_MIN_OVERLAP):
    """Whether item's started_at..ended_at covers `share` of the shorter of it and the window."""
    start, end = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
    item_start, item_end = datetime.fromisoformat(item["started_at"]), datetime.fromisoformat(item["ended_at"])
    overlap = (min(end, item_end) - max(start, item_start)).total_seconds()
    shorter = max(1.0, min((end - start).total_seconds(), (item_end - item_start).total_seconds()))
    return overlap >= share * shorter


def mode_override_for(overrides, source, target, started_at, ended_at):
    """The correction for a trip or area stay: same source and target, overlapping most of either span."""
    start, end = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
    best = None
    for item in overrides:
        if item["source"] != source or item["target"] != target:
            continue
        item_start, item_end = datetime.fromisoformat(item["started_at"]), datetime.fromisoformat(item["ended_at"])
        overlap = (min(end, item_end) - max(start, item_start)).total_seconds()
        shorter = max(1.0, min((end - start).total_seconds(), (item_end - item_start).total_seconds()))
        if overlap >= MODE_OVERRIDE_MIN_OVERLAP * shorter and (best is None or overlap > best[0]):
            best = (overlap, item)
    return best[1] if best else None


def apply_stay_edits(rows, edits):
    """Detected stays (newest first) with your edits applied, newest first.

    A removed window drops the stays it mostly covers. An added stop replaces
    any detected stay it overlaps and gets a negative id (-edit id) so it never
    collides with a detected one. Quick stops inside an area stay (a pause on
    a lake loop) are part of that visit, not a stop on the way.
    """
    areas = [row for row in rows if row["kind"] == "area"]
    removed = [edit for edit in edits if edit["action"] == "remove"]
    added = [edit for edit in edits if edit["action"] == "add"]

    def covered(row, windows, share=MODE_OVERRIDE_MIN_OVERLAP):
        return any(item["source"] == row["source"] and windows_overlap(item, row["started_at"], row["ended_at"], share)
                   for item in windows)

    kept = [row for row in rows
            if not covered(row, removed)
            and not covered(row, added, share=.01)
            and not (row["kind"] == "stop" and covered(row, areas, share=.01))]
    for edit in added:
        kept.append({"id": -edit["id"], "source": edit["source"], "started_at": edit["started_at"],
                     "ended_at": edit["ended_at"], "lat": edit["lat"], "lon": edit["lon"], "kind": "manual",
                     "point_count": 0, "ongoing": 0, "activity": None, "distance_m": None, "radius_m": None,
                     "went_elsewhere": None, "previous_stay_id": None, "start_observation_id": None,
                     "end_observation_id": None, "edit_id": edit["id"]})
    kept.sort(key=lambda row: (row["started_at"], row["id"]), reverse=True)
    return kept
