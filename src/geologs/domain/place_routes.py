"""Routes between places: one way of getting from one place to another, stitched from every trip that took it.

A trip leaves a track: the pieces of its way a tracker recorded, in order, with
the stretches nobody recorded between them (see split_at_jumps). A sparse trip
teaches little on its own, but trips that take the same way between the same
two places agree where they overlap, so each fills the others' gaps. A route
is that way: the densest recorded piece any of its trips has for each stretch,
joined by straight "unobserved" parts where none recorded it yet.
"""
import math
from collections import Counter

from .geo import metres
from .motion import MOVEMENT_MODES
from .paths import (DETOUR_SLACK_M, MATCH_M, MAX_DETOUR, SAME_ROUTE_M, PLACE_FOLLOW_M, PLACE_FOLLOW_MAX_M, PLACE_FOLLOW_SHARE,
                    PLACE_ORDER_SLACK_M, path_length, project_onto, simplify, stay_reach, to_lonlat, to_xy)

# A trip took a route when at least this share of its fixes lie on it, in order (see score).
ROUTE_JOIN_SCORE = .9
# A trip starts a route of its own only when its tracker recorded at least this share of
# its way: three fixes across town say which way you went, not what the way looks like.
ROUTE_SEED_SHARE = .5
# A fix where a route is still unobserved lies on it when it is this share of that
# stretch's length off its straight line, at most (the road it stands for may bend).
UNOBSERVED_SHARE = .5
# Walking between two places and driving between them take different ways.
ON_FOOT = {"walking", "running"}


def observed_pieces(legs):
    """The [lon, lat] pieces of a trip's way that were recorded, in order: recorded legs, and the
    fixes on their own between unrecorded legs, as one-point pieces. The stays at the ends are not fixes."""
    pieces = []
    for index, leg in enumerate(legs):
        coordinates = leg["coordinates"]
        if leg["kind"] == "recorded":
            if pieces and len(pieces[-1]) == 1 and pieces[-1][0] == coordinates[0]:
                pieces.pop()
            pieces.append([list(point) for point in coordinates])
            continue
        ends = ([coordinates[0]] if index > 0 else []) + ([coordinates[-1]] if index < len(legs) - 1 else [])
        for point in ends:
            if not pieces or pieces[-1][-1] != point:
                pieces.append([list(point)])
    return pieces


def track_from_trip(trip):
    """What a trip between two places recorded of its way, or None when it says nothing about one.

    A round trip goes nowhere to learn; a trip with no fix on the way has nothing to tell.
    """
    if trip.get("loop") or not trip.get("route"):
        return None
    pieces = [simplify(piece) for piece in observed_pieces(trip["route"])]
    if not pieces:
        return None
    start, end = [trip["from_lon"], trip["from_lat"]], [trip["to_lon"], trip["to_lat"]]
    way = [start, *(point for piece in pieces for point in piece), end]
    mode = trip.get("mode")
    return {"source": trip["source"], "started_at": trip["start"], "ended_at": trip["end"],
            # Riding as a passenger still follows roads a drive would.
            "mode": "driving" if mode == "passenger" else mode if mode in MOVEMENT_MODES else None,
            "from": start, "to": end, "from_name": trip.get("from_name"), "to_name": trip.get("to_name"),
            "pieces": pieces, "distance_m": round(path_length(way), 1),
            "observed_m": round(sum(path_length(piece) for piece in pieces), 1)}


def same_kind(a, b):
    """Whether two modes travel the same ways (an unknown mode goes with any)."""
    return a is None or b is None or (a in ON_FOOT) == (b in ON_FOOT)


def _parts_xy(parts, lat0):
    """A route's parts as one polyline in metres, with, for each segment, whether it was
    observed and how long the part it belongs to is."""
    xy, observed, part_m = [], [], []
    for part in parts:
        length = path_length(part["coordinates"])
        for index, (lon, lat) in enumerate(part["coordinates"]):
            point = to_xy(lon, lat, lat0)
            if xy and point == xy[-1]:
                continue
            if xy:
                seam = index == 0  # parts meet end to start; a gap between them nobody saw
                observed.append(part["observed"] and not seam)
                part_m.append(0.0 if seam else length)
            xy.append(point)
    return xy, observed, part_m


def _allowed(ends):
    """How far each of a trip's fixes may lie off a route (see PLACE_FOLLOW_M): ends are [a, *fixes, b]."""
    return [min(PLACE_FOLLOW_MAX_M, max(PLACE_FOLLOW_M, PLACE_FOLLOW_SHARE * max(
        metres(before[1], before[0], fix[1], fix[0]), metres(fix[1], fix[0], after[1], after[0]))))
        for before, fix, after in zip(ends, ends[1:], ends[2:])]


def match(route, a, b, fixes):
    """How well a trip from [lon, lat] a to b with these fixes (in order) took a route, or None.

    The route must pass within stay_reach of both ends, without a long detour.
    Returns {"score", "offset_m", "reverse", "on_a", "on_b"}: score is the share of the
    fixes that lie on the route in order (each within its limit where the route was
    observed, or within UNOBSERVED_SHARE of the stretch where it was not); a trip
    whose fixes go back on themselves along it did not take it.
    """
    lat0 = a[1]
    xy, observed, part_m = _parts_xy(route["parts"], lat0)
    if len(xy) < 2:
        return None
    reach = stay_reach(a, b)
    on_a, on_b = project_onto(xy, to_xy(a[0], a[1], lat0)), project_onto(xy, to_xy(b[0], b[1], lat0))
    if on_a[0] > reach or on_b[0] > reach:
        return None
    if abs(on_b[1] - on_a[1]) > MAX_DETOUR * metres(a[1], a[0], b[1], b[0]) + DETOUR_SLACK_M:
        return None
    reverse = on_a[1] > on_b[1]
    low, high = sorted((on_a[1], on_b[1]))
    if not fixes:
        return {"score": 1.0, "offset_m": 0.0, "reverse": reverse, "on_a": on_a, "on_b": on_b}
    matched, offsets, previous = 0, [], None
    for fix, limit in zip(fixes, _allowed([a, *fixes, b])):
        distance, along, segment, _point = project_onto(xy, to_xy(fix[0], fix[1], lat0))
        # Progress from a to b, whichever way the route runs.
        progress = (high - along) if reverse else (along - low)
        if previous is not None and progress < previous - PLACE_ORDER_SLACK_M:
            return {"score": 0.0, "offset_m": distance, "reverse": reverse, "on_a": on_a, "on_b": on_b}
        previous = progress if previous is None else max(previous, progress)
        if observed[segment]:
            if distance <= limit:
                matched += 1
                offsets.append(distance)
        elif distance <= max(limit, UNOBSERVED_SHARE * part_m[segment]):
            matched += 1
    return {"score": matched / len(fixes), "offset_m": sum(offsets) / len(offsets) if offsets else 0.0,
            "reverse": reverse, "on_a": on_a, "on_b": on_b}


def _way_parts(track):
    """A track as route parts: its recorded pieces, joined to each other and its stays by unobserved
    straight parts through any fixes on their own."""
    parts, chord = [], [track["from"]]
    for piece in track["pieces"]:
        chord.append(piece[0])
        if len(piece) > 1:
            parts += [{"coordinates": chord, "observed": False}, {"coordinates": piece, "observed": True}]
            chord = [piece[-1]]
    parts.append({"coordinates": [*chord, track["to"]], "observed": False})
    return [part for part in parts if part["observed"] or path_length(part["coordinates"]) > 0]


def _splice(chord, piece, lat0):
    """Unobserved chord parts with what a recorded piece shows of the way between its ends.

    The piece's points that lie between the chord's ends, and near enough to it,
    replace that stretch of it; returns the parts in its place, or None.
    """
    xy = [to_xy(lon, lat, lat0) for lon, lat in chord]
    length = path_length(chord)
    if length <= 0:
        return None
    inside = []
    for point in piece:
        distance, along, _segment, _at = project_onto(xy, to_xy(point[0], point[1], lat0))
        # Between the chord's ends and near enough to it, or at one of its ends: the piece passes through.
        at_end = min(metres(point[1], point[0], end[1], end[0]) for end in (chord[0], chord[-1])) <= SAME_ROUTE_M
        inside.append(at_end or (0 < along < length and distance <= UNOBSERVED_SHARE * length + MATCH_M))
    # The longest run of the piece inside the chord.
    best, run = (0, 0), None
    for index, ok in enumerate(inside + [False]):
        if ok and run is None:
            run = index
        elif not ok and run is not None:
            if index - run > best[1] - best[0]:
                best = (run, index)
            run = None
    if best[1] - best[0] < 2:
        return None
    stretch = piece[best[0]:best[1]]
    first = project_onto(xy, to_xy(stretch[0][0], stretch[0][1], lat0))[1]
    last = project_onto(xy, to_xy(stretch[-1][0], stretch[-1][1], lat0))[1]
    if last <= first:
        return None
    alongs, travelled = [0.0], 0.0
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        travelled += math.hypot(bx - ax, by - ay)
        alongs.append(travelled)
    before = [chord[0], *(point for point, along in zip(chord[1:-1], alongs[1:-1]) if along < first), stretch[0]]
    after = [stretch[-1], *(point for point, along in zip(chord[1:-1], alongs[1:-1]) if along > last), chord[-1]]
    return [{"coordinates": before, "observed": False}, {"coordinates": stretch, "observed": True},
            {"coordinates": after, "observed": False}]


def build(tracks):
    """A route's parts from the tracks of the trips that took it.

    The track that recorded most of its way lays the route down; each other one,
    densest first, fills in what is still unobserved with the pieces it recorded
    there. So where several trips recorded a stretch, the densest piece wins.
    """
    ordered = sorted(tracks, key=lambda track: (-track["observed_m"], track["started_at"]))
    parts = _way_parts(ordered[0])
    lat0 = ordered[0]["from"][1]
    for track in ordered[1:]:
        for piece in track["pieces"]:
            if len(piece) < 2:
                continue
            spliced = []
            for part in parts:
                replaced = None if part["observed"] else _splice(part["coordinates"], piece, lat0)
                spliced += replaced or [part]
            parts = spliced
    return [part for part in parts if path_length(part["coordinates"]) > 0 or part["observed"]]


def route_from(tracks, members):
    """A stored route for these tracks (first is the one that started it): its ends, parts, mode and names."""
    parts = build(tracks)
    latest = max(tracks, key=lambda track: track["started_at"])
    # Weighed by what each trip recorded: a few fixes say little about how you went.
    modes = Counter()
    for track in tracks:
        if track["mode"]:
            modes[track["mode"]] += track["observed_m"]
    coordinates = [point for part in parts for point in part["coordinates"]]
    return {"from": tracks[0]["from"], "to": tracks[0]["to"],
            "mode": max(modes, key=lambda mode: (modes[mode], mode)) if modes else None,
            "from_name": latest["from_name"], "to_name": latest["to_name"], "parts": parts, "members": members,
            "distance_m": round(path_length(coordinates), 1),
            "observed_m": round(sum(path_length(part["coordinates"]) for part in parts if part["observed"]), 1),
            "latest": latest["started_at"]}


def track_fixes(track):
    return [point for piece in track["pieces"] for point in piece]


def group(tracks):
    """Routes from tracks, in the order the trips were taken.

    Each trip joins the route in its own direction it took best (see match and
    ROUTE_JOIN_SCORE), else starts one of its own when it recorded enough of its
    way (see ROUTE_SEED_SHARE). Returns routes as route_from does; members hold
    {"source", "started_at", "score"}.
    """
    routes = []
    for track in sorted(tracks, key=lambda item: item["started_at"]):
        fixes = track_fixes(track)
        best, best_key = None, None
        for route in routes:
            if not same_kind(route["mode"], track["mode"]):
                continue
            fit = match(route, track["from"], track["to"], fixes)
            if fit is None or fit["reverse"] or fit["score"] < ROUTE_JOIN_SCORE:
                continue
            key = (fit["score"], -fit["offset_m"], len(route["members"]))
            if best_key is None or key > best_key:
                best, best_key = route, key
        if best is not None:
            best["tracks"].append(track)
            best["members"].append({"source": track["source"], "started_at": track["started_at"], "score": round(best_key[0], 3)})
        elif track["distance_m"] and track["observed_m"] >= ROUTE_SEED_SHARE * track["distance_m"]:
            best = {"tracks": [track], "members": [{"source": track["source"], "started_at": track["started_at"], "score": 1.0}]}
            routes.append(best)
        else:
            continue
        best.update({key: value for key, value in route_from(best["tracks"], best["members"]).items() if key != "members"})
    return [{key: value for key, value in route.items() if key != "tracks"} for route in routes]


def route_stretch(route, fit, a, b):
    """The parts of a route a trip from [lon, lat] a to b took (see match), in its order: [lon, lat]
    lists with whether each was observed, from a to b."""
    lat0 = a[1]
    xy, observed, _part_m = _parts_xy(route["parts"], lat0)
    on_a, on_b = fit["on_a"], fit["on_b"]
    first, last = (on_b, on_a) if fit["reverse"] else (on_a, on_b)
    points = [first[3], *xy[first[2] + 1:last[2] + 1], last[3]]
    flags = [observed[first[2]], *observed[first[2] + 1:last[2] + 1]]
    if fit["reverse"]:
        points, flags = points[::-1], flags[::-1]
    parts = []
    start, end = to_xy(a[0], a[1], lat0), to_xy(b[0], b[1], lat0)
    for (p, q), flag in zip(zip([start, *points], [*points, end]), [False, *flags, False]):
        if math.hypot(q[0] - p[0], q[1] - p[1]) < .01:
            continue
        if parts and parts[-1]["observed"] == flag:
            parts[-1]["xy"].append(q)
        else:
            parts.append({"xy": [p, q], "observed": flag})
    return [{"coordinates": [to_lonlat(x, y, lat0) for x, y in part["xy"]], "observed": part["observed"],
             "distance_m": round(path_length([to_lonlat(x, y, lat0) for x, y in part["xy"]]), 1)} for part in parts]


def best_place_route(routes, a, b, fixes, mode=None):
    """The route a trip from [lon, lat] a to b most likely took, with how: (route, fit), or None.

    One in the trip's own direction it took (see ROUTE_JOIN_SCORE) wins: the
    best fit, then the most travelled. Only failing that, one the other way round.
    """
    best, best_key = None, None
    for route in routes:
        if not same_kind(route["mode"], mode):
            continue
        fit = match(route, a, b, fixes)
        if fit is None or fit["score"] < ROUTE_JOIN_SCORE:
            continue
        key = (not fit["reverse"], fit["score"], -fit["offset_m"], len(route["members"]), route["latest"])
        if best_key is None or key > best_key:
            best, best_key = (route, fit), key
    return best
