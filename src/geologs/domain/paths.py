"""Path geometry: simplifying recorded routes and matching gaps against known ones."""
import math

from .geo import metres
from .motion import MOVEMENT_MODES
from collections import Counter


# Only trips recorded end to end teach a path, and only when no two accurate
# fixes lie farther apart than this: a longer straight chord cuts corners a
# road doesn't (a tracker that holds one position for several polls, then
# jumps kilometres, records a drive that only looks dense); a fix a minute at
# 45 km/h is fine. Shorter trips teach little but clutter the library.
LEARN_MAX_GAP_M = 750
LEARN_MIN_FIXES = 5
LEARN_MIN_DISTANCE_M = 300
# Stored paths keep their shape to within this.
SIMPLIFY_M = 8
# A gap's ends (a fix, or a stay's centre) must lie this close to a known path.
MATCH_M = 100
# ...or this close, when that end is a stay rather than a fix: the way to a campus is
# the same whichever of its buildings you end up in, and a straight leg joins the rest.
# A short leg (a walk between two buildings) gets no more than a quarter of its length.
STAY_MATCH_M = 400


def stay_reach(a, b):
    """How far a route may pass from a stay at the end of the leg from a to b."""
    return max(MATCH_M, min(STAY_MATCH_M, metres(a[1], a[0], b[1], b[0]) / 4))
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
    """A trip recorded densely enough to show the way it went.

    One recorded piece, and every stretch around it that nothing recorded no
    longer than a hole the piece may have: an app that reports only once you
    are moving leaves a short leg between the stay and the first fix (and the
    last fix and the next stay) on nearly every trip.
    """
    legs = trip.get("route")
    if legs is None:  # a trip built before trips kept their legs
        legs = [{"kind": "recorded", "coordinates": segment} for segment in trip.get("route_segments") or []]
        legs += [{"kind": "gap", "coordinates": [[0, 0], [1, 1]]} for _gap in trip.get("route_gaps") or []]
    distance_m = trip.get("distance_km", 0) * 1000
    points = trip.get("recorded_points") or 0
    return (sum(leg["kind"] == "recorded" for leg in legs) == 1
            and all(leg["kind"] == "recorded" or path_length(leg["coordinates"]) <= LEARN_MAX_GAP_M for leg in legs)
            and trip.get("mode") in MOVEMENT_MODES
            and distance_m >= LEARN_MIN_DISTANCE_M and points >= LEARN_MIN_FIXES
            and trip.get("max_fix_gap_m", float("inf")) <= LEARN_MAX_GAP_M)


def learned_way(trip):
    """The [lon, lat] way a learnable trip teaches: its recorded piece with the short legs to its stays."""
    way = []
    for leg in trip.get("route") or [{"coordinates": trip["route_segments"][0]}]:
        coordinates = leg["coordinates"]
        way += coordinates[1:] if way and way[-1] == coordinates[0] else coordinates
    return way


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


def search_box(a, b, reach=MATCH_M):
    """(min_lat, max_lat, min_lon, max_lon) that a known path must reach over to pass within `reach` of both [lon, lat] ends."""
    pad_lat = reach / 110574
    pad_lon = reach / (111320 * math.cos(math.radians(a[1])))
    return (min(a[1], b[1]) + pad_lat, max(a[1], b[1]) - pad_lat, min(a[0], b[0]) + pad_lon, max(a[0], b[0]) - pad_lon)


def best_route(a, b, known, reach_a=MATCH_M, reach_b=MATCH_M):
    """The likeliest of `known` paths from [lon, lat] a to b, or None.

    A path must pass within reach_a of a and reach_b of b (see STAY_MATCH_M).
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
        if on_a[0] > reach_a or on_b[0] > reach_b:
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


def with_fills(trip, filled):
    """A copy of a trip whose unrecorded legs were filled from known routes where there was one.

    `filled` maps the index of a gap leg in trip["route"] to its fill (see
    best_route). A filled leg becomes a "fill" leg in place and counts toward
    the trip's distance (an unrecorded trip's distance is its legs'). A trip
    mostly made of them takes their mode, unless you corrected it: a few fixes
    rolling away from home say little about how you crossed town.
    """
    legs = [{"kind": "fill", "coordinates": filled[index]["coordinates"], "past_trips": filled[index]["past_trips"],
             "mode": filled[index]["mode"]} if index in filled else leg for index, leg in enumerate(trip.get("route") or [])]
    fills = [filled[index] for index in sorted(filled)]
    trip = {**trip, "route": legs, "route_fills": fills,
            "route_gaps": [leg["coordinates"] for leg in legs if leg["kind"] == "gap"]}
    if not fills:
        return trip
    filled_km = sum(fill["distance_m"] for fill in fills) / 1000
    if trip.get("recorded", True):
        recorded_km = trip["distance_km"]
        distance_km = recorded_km + filled_km
    else:
        recorded_km = 0.0
        distance_km = filled_km + sum(metres(a[1], a[0], b[1], b[0]) for leg in legs if leg["kind"] == "gap"
                                      for a, b in zip(leg["coordinates"], leg["coordinates"][1:])) / 1000
    trip.update(recorded_km=recorded_km, filled_km=round(filled_km, 2), distance_km=round(distance_km, 2))
    by_mode = Counter()
    for fill in fills:
        by_mode[fill["mode"]] += fill["distance_m"]
    mode = by_mode.most_common(1)[0][0]
    if filled_km > FILL_MODE_SHARE * trip["distance_km"] and mode != trip["mode"] and not trip.get("mode_corrected"):
        trip.update(detected_mode=trip["mode"], mode=mode, modes={mode: sum(trip["modes"].values()) or 1},
                    mode_from_paths=True)
    return trip
