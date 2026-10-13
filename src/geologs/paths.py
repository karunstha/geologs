"""Known paths: routes learned from well-recorded trips, used to fill the
unrecorded stretches of sparse ones.

A trip from a dense tracker records where you went. A sparse tracker (an app
that reports only after you have moved a fair way) leaves most of a trip
unrecorded, but you usually went the way you have gone before: the same road
serves trips between many places, so a gap is matched against every known
path that passes near both of its ends, not only trips between the same two
stays. A filled stretch is still a guess and is never learned from.
"""
import json
import math
from collections import Counter
from datetime import datetime

from .core import MOVEMENT_MODES, metres

# Only trips recorded end to end, with fixes at least this close on average,
# teach a path (a minute apart at 60 km/h); shorter trips teach little but clutter the library.
LEARN_MAX_SPACING_M = 1000
LEARN_MIN_FIXES = 5
LEARN_MIN_DISTANCE_M = 300
# Stored paths keep their shape to within this.
SIMPLIFY_M = 8
# A gap's ends (a fix, or a stay's centre) must lie this close to a known path.
MATCH_M = 100
# ...and the stretch between them may wind at most this much more than the
# straight line, so a path that loops away and back is not taken for a short cut.
MAX_DETOUR = 2.0
DETOUR_SLACK_M = 300
# Stretches from different trips whose sampled points all lie this close to
# each other are one route: the most travelled one wins.
SAME_ROUTE_M = 60
SAME_ROUTE_SAMPLES = 8
MAX_CANDIDATES = 60
# A trip that is mostly filled takes its mode from the trips that taught the fill.
FILL_MODE_SHARE = .5


def _xy(lon, lat, lat0):
    return lon * 111320 * math.cos(math.radians(lat0)), lat * 110574


def _lonlat(x, y, lat0):
    return [x / (111320 * math.cos(math.radians(lat0))), y / 110574]


def _length(coordinates):
    return sum(metres(a[1], a[0], b[1], b[0]) for a, b in zip(coordinates, coordinates[1:]))


def simplify(coordinates, tolerance_m=SIMPLIFY_M):
    """Douglas-Peucker over [lon, lat] pairs."""
    if len(coordinates) < 3:
        return [list(point) for point in coordinates]
    lat0 = coordinates[0][1]
    xy = [_xy(lon, lat, lat0) for lon, lat in coordinates]
    keep = [False] * len(xy)
    keep[0] = keep[-1] = True
    stack = [(0, len(xy) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = xy[first], xy[last]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        farthest, index = 0.0, None
        for i in range(first + 1, last):
            px, py = xy[i]
            distance = (abs(dy * (px - ax) - dx * (py - ay)) / span) if span else math.hypot(px - ax, py - ay)
            if distance > farthest:
                farthest, index = distance, i
        if index is not None and farthest > tolerance_m:
            keep[index] = True
            stack += [(first, index), (index, last)]
    return [list(coordinates[i]) for i in range(len(coordinates)) if keep[i]]


def learnable(trip):
    """A trip recorded end to end, densely enough to show the way it went."""
    segments = trip.get("route_segments") or []
    distance_m = trip.get("distance_km", 0) * 1000
    points = trip.get("recorded_points") or 0
    return (len(segments) == 1 and not trip.get("route_gaps") and trip.get("mode") in MOVEMENT_MODES
            and distance_m >= LEARN_MIN_DISTANCE_M and points >= LEARN_MIN_FIXES
            and distance_m / (points - 1) <= LEARN_MAX_SPACING_M)


def _bounds(coordinates):
    lons, lats = [point[0] for point in coordinates], [point[1] for point in coordinates]
    return min(lats), min(lons), max(lats), max(lons)


def learn_paths(store, trips, start, end, source=None):
    """Keep known_paths in step with the learnable trips that start in [start, end].

    Only rows that differ are written, so learning the same day again changes
    nothing (and keeps cached days valid). Returns how many trips taught a path.
    """
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    wanted = {}
    for trip in trips:
        if first <= datetime.fromisoformat(trip["start"]) <= last and learnable(trip):
            coordinates = simplify(trip["route_segments"][0])
            wanted[(trip["source"], trip["start"])] = {
                "ended_at": trip["end"], "mode": trip["mode"], "distance_m": round(_length(coordinates), 1),
                "coordinates": json.dumps(coordinates, separators=(",", ":"))}
    clauses, args = ["started_at>=?", "started_at<=?"], [start, end]
    if source:
        clauses.append("source=?")
        args.append(source)
    existing = {(row["source"], row["started_at"]): row
                for row in store._rows("SELECT * FROM known_paths WHERE " + " AND ".join(clauses), args)}
    # Rows are compared as stored; a timestamp spelled differently is a different trip.
    existing = {key: row for key, row in existing.items()
                if first <= datetime.fromisoformat(row["started_at"]) <= last}
    changed = False
    for key, row in existing.items():
        want = wanted.get(key)
        if want is None or any(row[field] != want[field] for field in ("ended_at", "mode", "coordinates")):
            store.db.execute("DELETE FROM known_paths WHERE id=?", (row["id"],))
            changed = True
    for (path_source, started_at), want in wanted.items():
        row = existing.get((path_source, started_at))
        if row is not None and all(row[field] == want[field] for field in ("ended_at", "mode", "coordinates")):
            continue
        min_lat, min_lon, max_lat, max_lon = _bounds(json.loads(want["coordinates"]))
        store.db.execute("""INSERT INTO known_paths
            (source,started_at,ended_at,mode,distance_m,min_lat,min_lon,max_lat,max_lon,coordinates)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (path_source, started_at, want["ended_at"], want["mode"], want["distance_m"],
             min_lat, min_lon, max_lat, max_lon, want["coordinates"]))
        changed = True
    if changed:
        store.db.commit()
    return len(wanted)


def signature(store):
    """Changes whenever the library does, for cache keys of days that use it."""
    row = store.db.execute("SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS max_id FROM known_paths").fetchone()
    return f"{row['count']}:{row['max_id']}"


def _project(xy, point):
    """Nearest point on a polyline: (distance, distance along it, index of its segment, position)."""
    best, along = None, 0.0
    px, py = point
    for i, ((ax, ay), (bx, by)) in enumerate(zip(xy, xy[1:])):
        dx, dy = bx - ax, by - ay
        span2 = dx * dx + dy * dy
        t = 0.0 if not span2 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span2))
        x, y = ax + t * dx, ay + t * dy
        distance = math.hypot(px - x, py - y)
        if best is None or distance < best[0]:
            best = (distance, along + t * math.sqrt(span2), i, (x, y))
        along += math.sqrt(span2)
    return best


def _stretch(xy, a, b):
    """The part of a polyline between two projections, in the order a to b."""
    reverse = a[1] > b[1]
    if reverse:
        a, b = b, a
    middle = xy[a[2] + 1:b[2] + 1]
    points = [a[3], *middle, b[3]]
    return (points[::-1] if reverse else points), reverse


def _samples(points, count):
    """count points spread evenly along a polyline."""
    lengths = [0.0]
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        lengths.append(lengths[-1] + math.hypot(bx - ax, by - ay))
    total, result, i = lengths[-1], [], 0
    for k in range(count):
        target = total * k / max(1, count - 1)
        while i < len(lengths) - 2 and lengths[i + 1] < target:
            i += 1
        span = lengths[i + 1] - lengths[i] if i + 1 < len(lengths) else 0
        t = (target - lengths[i]) / span if span else 0
        (ax, ay), (bx, by) = points[i], points[min(i + 1, len(points) - 1)]
        result.append((ax + t * (bx - ax), ay + t * (by - ay)))
    return result


def _near(samples, points):
    return all(_project(points, sample)[0] <= SAME_ROUTE_M for sample in samples) if len(points) > 1 else False


def fill_gap(store, a, b):
    """The likeliest known route from [lon, lat] a to b, or None.

    Returns {"coordinates", "past_trips", "mode", "distance_m"}: past_trips
    counts the trips that went this way between these two points.
    """
    straight = metres(a[1], a[0], b[1], b[0])
    pad_lat = MATCH_M / 110574
    pad_lon = MATCH_M / (111320 * math.cos(math.radians(a[1])))
    rows = store._rows("""SELECT mode,started_at,coordinates FROM known_paths
        WHERE min_lat<=? AND max_lat>=? AND min_lon<=? AND max_lon>=? ORDER BY started_at DESC LIMIT ?""",
        (min(a[1], b[1]) + pad_lat, max(a[1], b[1]) - pad_lat, min(a[0], b[0]) + pad_lon, max(a[0], b[0]) - pad_lon,
         MAX_CANDIDATES))
    lat0 = a[1]
    start, end = _xy(a[0], a[1], lat0), _xy(b[0], b[1], lat0)
    candidates = []
    for row in rows:
        xy = [_xy(lon, lat, lat0) for lon, lat in json.loads(row["coordinates"])]
        if len(xy) < 2:
            continue
        on_a, on_b = _project(xy, start), _project(xy, end)
        if on_a[0] > MATCH_M or on_b[0] > MATCH_M:
            continue
        stretch, reverse = _stretch(xy, on_a, on_b)
        length = abs(on_b[1] - on_a[1])
        if length > MAX_DETOUR * straight + DETOUR_SLACK_M:
            continue
        candidates.append({"points": stretch, "reverse": reverse, "mode": row["mode"],
                           "started_at": row["started_at"], "length": length})
    if not candidates:
        return None
    for candidate in candidates:
        candidate["samples"] = _samples(candidate["points"], SAME_ROUTE_SAMPLES)
    for candidate in candidates:
        candidate["route"] = [other for other in candidates
                              if other is candidate or (_near(candidate["samples"], other["points"])
                                                        and _near(other["samples"], candidate["points"]))]
    # The most travelled route, then one travelled this way round, then the latest.
    best = max(candidates, key=lambda item: (len(item["route"]), not item["reverse"], item["started_at"]))
    points = [start, *best["points"], end]
    coordinates = [[a[0], a[1]], *(_lonlat(x, y, lat0) for x, y in best["points"]), [b[0], b[1]]]
    modes = Counter(item["mode"] for item in best["route"])
    return {"coordinates": coordinates, "past_trips": len(best["route"]), "mode": modes.most_common(1)[0][0],
            "distance_m": round(sum(math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:])), 1)}


def fill_trip(store, trip):
    """Replace a trip's unrecorded legs with known routes where there is one.

    Filled legs move from route_gaps to route_fills and count toward the
    trip's distance. A trip mostly made of them takes their mode, unless you
    corrected it: a few fixes rolling away from home say little about how
    you crossed town.
    """
    fills, gaps = [], []
    for gap in trip.get("route_gaps") or []:
        fill = fill_gap(store, gap[0], gap[-1])
        if fill:
            fills.append(fill)
        else:
            gaps.append(gap)
    trip["route_fills"], trip["route_gaps"] = fills, gaps
    if not fills:
        return trip
    filled_km = sum(fill["distance_m"] for fill in fills) / 1000
    recorded_km = trip["distance_km"]
    trip.update(recorded_km=recorded_km, filled_km=round(filled_km, 2), distance_km=round(recorded_km + filled_km, 2))
    by_mode = Counter()
    for fill in fills:
        by_mode[fill["mode"]] += fill["distance_m"]
    mode = by_mode.most_common(1)[0][0]
    if filled_km > FILL_MODE_SHARE * trip["distance_km"] and mode != trip["mode"] and not trip.get("mode_corrected"):
        trip.update(detected_mode=trip["mode"], mode=mode, modes={mode: sum(trip["modes"].values())},
                    mode_from_paths=True)
    return trip


def report(store):
    """What the library holds: paths by mode and by source."""
    rows = store._rows("SELECT source,mode,distance_m,started_at FROM known_paths")
    return {"paths": len(rows),
            "km": round(sum(row["distance_m"] for row in rows) / 1000, 1),
            "by_mode": dict(Counter(row["mode"] for row in rows).most_common()),
            "by_source": dict(Counter(row["source"] for row in rows).most_common()),
            "first": min((row["started_at"] for row in rows), default=None),
            "last": max((row["started_at"] for row in rows), default=None)}
