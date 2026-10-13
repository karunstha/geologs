"""Path geometry: simplifying recorded routes and matching gaps against known ones."""
import math

from .geo import metres
from .motion import MOVEMENT_MODES
from collections import Counter


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


def to_xy(lon, lat, lat0):
    return lon * 111320 * math.cos(math.radians(lat0)), lat * 110574


def to_lonlat(x, y, lat0):
    return [x / (111320 * math.cos(math.radians(lat0))), y / 110574]


def path_length(coordinates):
    return sum(metres(a[1], a[0], b[1], b[0]) for a, b in zip(coordinates, coordinates[1:]))


def simplify(coordinates, tolerance_m=SIMPLIFY_M):
    """Douglas-Peucker over [lon, lat] pairs."""
    if len(coordinates) < 3:
        return [list(point) for point in coordinates]
    lat0 = coordinates[0][1]
    xy = [to_xy(lon, lat, lat0) for lon, lat in coordinates]
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


def path_bounds(coordinates):
    lons, lats = [point[0] for point in coordinates], [point[1] for point in coordinates]
    return min(lats), min(lons), max(lats), max(lons)


def project_onto(xy, point):
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


def stretch_between(xy, a, b):
    """The part of a polyline between two projections, in the order a to b."""
    reverse = a[1] > b[1]
    if reverse:
        a, b = b, a
    middle = xy[a[2] + 1:b[2] + 1]
    points = [a[3], *middle, b[3]]
    return (points[::-1] if reverse else points), reverse


def samples_along(points, count):
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


def follows(samples, points):
    return all(project_onto(points, sample)[0] <= SAME_ROUTE_M for sample in samples) if len(points) > 1 else False


def search_box(a, b):
    """(min_lat, max_lat, min_lon, max_lon) that a known path must reach over to pass near both [lon, lat] ends."""
    pad_lat = MATCH_M / 110574
    pad_lon = MATCH_M / (111320 * math.cos(math.radians(a[1])))
    return (min(a[1], b[1]) + pad_lat, max(a[1], b[1]) - pad_lat, min(a[0], b[0]) + pad_lon, max(a[0], b[0]) - pad_lon)


def best_route(a, b, known):
    """The likeliest of `known` paths from [lon, lat] a to b, or None.

    `known` holds paths as {"mode", "started_at", "coordinates"}. Returns
    {"coordinates", "past_trips", "mode", "distance_m"}: past_trips counts
    the trips that went this way between these two points.
    """
    straight = metres(a[1], a[0], b[1], b[0])
    lat0 = a[1]
    start, end = to_xy(a[0], a[1], lat0), to_xy(b[0], b[1], lat0)
    candidates = []
    for row in known:
        xy = [to_xy(lon, lat, lat0) for lon, lat in row["coordinates"]]
        if len(xy) < 2:
            continue
        on_a, on_b = project_onto(xy, start), project_onto(xy, end)
        if on_a[0] > MATCH_M or on_b[0] > MATCH_M:
            continue
        stretch, reverse = stretch_between(xy, on_a, on_b)
        length = abs(on_b[1] - on_a[1])
        if length > MAX_DETOUR * straight + DETOUR_SLACK_M:
            continue
        candidates.append({"points": stretch, "reverse": reverse, "mode": row["mode"],
                           "started_at": row["started_at"], "length": length})
    if not candidates:
        return None
    for candidate in candidates:
        candidate["samples"] = samples_along(candidate["points"], SAME_ROUTE_SAMPLES)
    for candidate in candidates:
        candidate["route"] = [other for other in candidates
                              if other is candidate or (follows(candidate["samples"], other["points"])
                                                        and follows(other["samples"], candidate["points"]))]
    # The most travelled route, then one travelled this way round, then the latest.
    best = max(candidates, key=lambda item: (len(item["route"]), not item["reverse"], item["started_at"]))
    points = [start, *best["points"], end]
    coordinates = [[a[0], a[1]], *(to_lonlat(x, y, lat0) for x, y in best["points"]), [b[0], b[1]]]
    modes = Counter(item["mode"] for item in best["route"])
    return {"coordinates": coordinates, "past_trips": len(best["route"]), "mode": modes.most_common(1)[0][0],
            "distance_m": round(sum(math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:])), 1)}


def with_fills(trip, fills, gaps):
    """A copy of a trip whose unrecorded legs were filled from known routes where there was one.

    Filled legs move from route_gaps to route_fills and count toward the
    trip's distance. A trip mostly made of them takes their mode, unless you
    corrected it: a few fixes rolling away from home say little about how
    you crossed town.
    """
    trip = {**trip, "route_fills": fills, "route_gaps": gaps}
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

