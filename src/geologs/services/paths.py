"""The path library: learning routes from well-recorded trips, and filling sparse trips from it."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from ..domain.paths import best_route, learnable, MAX_CANDIDATES, path_bounds, path_length, search_box, simplify, with_fills
from .stays import anchored_trips
from .use_case import use_case
from ..domain.timing import parse_time
from ..domain.paths import learned_way
from ..domain.paths import MATCH_M
from ..domain.paths import stay_reach


@use_case
def learn_paths(db, trips, start, end, source=None):
    """Keep known_paths in step with the learnable trips that start in [start, end].

    Only rows that differ are written, so learning the same day again changes
    nothing (and keeps cached days valid). Returns how many trips taught a path.
    """
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    excluded = db.paths.excluded_trips(start, end, source)
    wanted = {}
    for trip in trips:
        if (first <= datetime.fromisoformat(trip["start"]) <= last and learnable(trip)
                and (trip["source"], trip["start"]) not in excluded):
            coordinates = simplify(learned_way(trip))
            wanted[(trip["source"], trip["start"])] = {
                # Riding as a passenger still follows roads a drive would.
                "ended_at": trip["end"], "mode": "driving" if trip["mode"] == "passenger" else trip["mode"],
                "distance_m": round(path_length(coordinates), 1),
                "coordinates": json.dumps(coordinates, separators=(",", ":"))}
    # Rows are compared as stored; a timestamp spelled differently is a different trip.
    existing = {(row["source"], row["started_at"]): row for row in db.paths.starting_between(start, end, source)
                if first <= datetime.fromisoformat(row["started_at"]) <= last}
    for key, row in existing.items():
        want = wanted.get(key)
        if want is None or any(row[field] != want[field] for field in ("ended_at", "mode", "coordinates")):
            db.paths.delete(row["id"])
    for (path_source, started_at), want in wanted.items():
        row = existing.get((path_source, started_at))
        if row is not None and all(row[field] == want[field] for field in ("ended_at", "mode", "coordinates")):
            continue
        db.paths.add(path_source, started_at, want["ended_at"], want["mode"], want["distance_m"],
                     path_bounds(json.loads(want["coordinates"])), want["coordinates"])
    return len(wanted)


@use_case
def fill_gap(db, a, b, reach_a=MATCH_M, reach_b=MATCH_M):
    """The likeliest known route from [lon, lat] a to b, or None (see best_route)."""
    known = [{**row, "coordinates": json.loads(row["coordinates"])}
             for row in db.paths.covering(*search_box(a, b, max(reach_a, reach_b)), MAX_CANDIDATES)]
    return best_route(a, b, known, reach_a, reach_b)


@use_case
def fill_trip(db, trip):
    """Fill a trip's unrecorded legs with known routes where there is one (see with_fills).

    The first leg starts at the stay the trip left and the last ends at the
    stay it reached; a route may pass farther from a stay than from a fix.
    """
    legs, filled = trip.get("route") or [], {}
    for index, leg in enumerate(legs):
        if leg["kind"] == "gap":
            a, b = leg["coordinates"][0], leg["coordinates"][-1]
            fill = fill_gap(db, a, b, stay_reach(a, b) if index == 0 else MATCH_M,
                            stay_reach(a, b) if index == len(legs) - 1 else MATCH_M)
            if fill:
                filled[index] = fill
    return with_fills(trip, filled)


@use_case
def report(db):
    """What the library holds: paths by mode and by source."""
    rows = db.paths.summary_rows()
    return {"paths": len(rows),
            "km": round(sum(row["distance_m"] for row in rows) / 1000, 1),
            "by_mode": dict(Counter(row["mode"] for row in rows).most_common()),
            "by_source": dict(Counter(row["source"] for row in rows).most_common()),
            "first": min((row["started_at"] for row in rows), default=None),
            "last": max((row["started_at"] for row in rows), default=None)}


@use_case
def list_paths(db, limit=200):
    """What the library holds (see report), with its latest paths and the ones you excluded: which trips
    taught a route, for checking."""
    return {**report(db), "recent": db.paths.recent(limit), "excluded": db.paths.excluded()}


@use_case
def exclude_path(db, source, started_at):
    """Stop using a learned path (a trip that recorded the way badly) and never learn it again."""
    path = db.paths.get(source, started_at)
    if path is None:
        raise ValueError("that path is not in the library")
    db.paths.exclude(path, parse_time(None))
    db.paths.delete(path["id"])
    return list_paths(db)


@use_case
def include_path(db, source, started_at):
    """Undo an exclusion: the trip's path is learned again, if its trip still qualifies."""
    if not db.paths.include(source, started_at):
        raise ValueError("that path was not excluded")
    learn_path_history(db, source=source, start=started_at, end=started_at)
    return list_paths(db)


@use_case
def learn_path_history(db, days=7, source=None, start=None, end=None):
    """Teach the path library from trips on record (all of them, or one source's, or a span), a week at a time.

    Returns how many trips there were and how many were recorded well enough
    to learn from, with what the library now holds.
    """
    first_fix, last_fix = db.points.span()
    trips = learned = 0
    if first_fix:
        window = datetime.fromisoformat(start or first_fix).astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        last = datetime.fromisoformat(end or last_fix)
        while window <= last:
            first = window
            window += timedelta(days=days)
            window_start, window_end = first.isoformat(), (window - timedelta(microseconds=1)).isoformat()
            found = [trip for trip in anchored_trips(db, window_start, window_end, source, lookup_places=False)
                     if first <= datetime.fromisoformat(trip["start"]) < window]
            trips += len(found)
            learned += learn_paths(db, found, window_start, window_end, source)
    return {"trips": trips, "learned": learned, **report(db)}


@use_case
def learn_recent_paths(db, now=None, days=2):
    """Keep the library in step with the last `days` days once new samples have arrived.

    The background worker runs this, so reading a day only uses the library:
    a trip earlier today can fill in a sparse one later today.
    """
    seen = db.points.watermark()
    if db.settings.value("paths_learned_through") == seen:
        return None
    now = now or datetime.now(timezone.utc)
    result = learn_path_history(db, start=(now - timedelta(days=days)).isoformat(), end=now.isoformat())
    db.settings.set_value("paths_learned_through", seen)
    return result
