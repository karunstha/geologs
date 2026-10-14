"""Drawing recorded paths: which fixes connect, and the legs between them nobody recorded."""
from datetime import datetime

from .geo import metres
from .motion import connected_elapsed, same_source_state


# A trip's unrecorded leg shorter than this (its first fix inside the stay it left) is not drawn.
ROUTE_GAP_MIN_M = 25
# Consecutive fixes this far apart (a fix a minute at city speed, 45 km/h) are not a
# recorded way when the tracker had not reported a new position for this long before
# the second: a straight chord cuts corners a road doesn't. That covers a tracker that
# reports every few minutes, and one that holds its last position for several polls,
# then leaps; a tracker reporting a new position every minute records the way at any speed.
ROUTE_JUMP_M = 750
ROUTE_JUMP_SECONDS = 120


def route_segments_from_points(points, keep_lone=False):
    """Connect plausible fixes per source and skip readings too inaccurate to route.

    keep_lone also returns a fix connected to neither neighbour, as a one-point segment.
    """
    return [[[point["lon"], point["lat"]] for point in piece]
            for run in connected_runs(points, keep_lone) for piece in split_at_jumps(run)
            if len(piece) > 1 or keep_lone]


def connected_runs(points, keep_lone=False):
    """The fixes route_segments_from_points connects, as runs of point rows."""
    segments = []
    by_source = {}
    for point in points:
        if point["accuracy_m"] is None or point["accuracy_m"] <= 150:
            by_source.setdefault(point["source"], []).append(point)
    for source_points in by_source.values():
        source_points.sort(key=lambda point: (point["timestamp"], point.get("id", 0)))
        current, previous, held_since = [], None, None
        for point in source_points:
            connected = previous is not None and connected_elapsed(previous, point, held_since) is not None
            if not connected:
                if len(current) > 1 or current and keep_lone:
                    segments.append(current)
                current = [point]
            else:
                current.append(point)
            if previous is None or not same_source_state(previous, point):
                held_since = point
            previous = point
        if len(current) > 1 or current and keep_lone:
            segments.append(current)
    return segments


def jumps(run):
    """For each consecutive pair of a run of fixes, whether nobody recorded the way between them
    (see ROUTE_JUMP_M): the time is counted from the first poll of the position it leaves."""
    result, held = [], None
    for before, point in zip(run, run[1:]):
        if held is None or (held["lat"], held["lon"]) != (before["lat"], before["lon"]):
            held = before
        seconds = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(held["timestamp"])).total_seconds()
        result.append(seconds > ROUTE_JUMP_SECONDS
                      and metres(before["lat"], before["lon"], point["lat"], point["lon"]) > ROUTE_JUMP_M)
    return result


def split_at_jumps(run):
    """A connected run of fixes as the runs between its jumps (see jumps)."""
    runs = [[run[0]]] if run else []
    for point, jump in zip(run[1:], jumps(run)):
        if jump:
            runs.append([])
        runs[-1].append(point)
    return runs


def split_at_steps(coordinates, max_m=ROUTE_JUMP_M):
    """[lon, lat] fixes as the pieces between steps longer than max_m: a tracker watching all along,
    but too sparsely there for a straight line to be the road."""
    pieces = [[coordinates[0]]] if coordinates else []
    for before, point in zip(coordinates, coordinates[1:]):
        if metres(before[1], before[0], point[1], point[0]) > max_m:
            pieces.append([])
        pieces[-1].append(point)
    return pieces


def guessed_stretch_m(fixes):
    """The longest jump between consecutive fixes (see jumps): how much of a way was guessed."""
    return max((metres(before["lat"], before["lon"], point["lat"], point["lon"])
                for before, point, jump in zip(fixes, fixes[1:], jumps(fixes)) if jump), default=0.0)


def route_legs(pieces, origin, destination, jumps=(), fixes=None, sparse=()):
    """A trip's way from one stay to the next, in order: {"kind", "coordinates"} legs.

    Recorded pieces (in time order) are "recorded" legs; where they stop short
    of the stays or of each other, a straight "gap" leg joins them: you still
    went that way. A sparse tracker leaves most of a trip like this, and a fix
    on its own is only a point the way passed through. `jumps` holds the
    indexes of pieces split from the one before at a jump (see split_at_jumps):
    the gap before one is marked "jump", as the trip's distance already runs along it.
    `sparse` holds the indexes of pieces split from the one before at a step too long to be the
    road although the tracker was watching (see split_at_steps): the gap before one is marked
    "sparse", and the trip's distance runs along it too. `fixes`, when given, holds each piece's
    fixes before smoothing: a recorded leg keeps them as "fixes", for what they say about the
    way (see place_routes.observed_pieces).
    """
    ends = [[[origin["lon"], origin["lat"]]], *pieces, [[destination["lon"], destination["lat"]]]]
    legs = []
    for index, (before, after) in enumerate(zip(ends, ends[1:])):
        if index in jumps:
            legs.append({"kind": "gap", "coordinates": [before[-1], after[0]], "jump": True})
        elif index in sparse:
            legs.append({"kind": "gap", "coordinates": [before[-1], after[0]], "sparse": True})
        elif metres(before[-1][1], before[-1][0], after[0][1], after[0][0]) > ROUTE_GAP_MIN_M:
            legs.append({"kind": "gap", "coordinates": [before[-1], after[0]]})
        if index < len(pieces) and len(after) > 1:
            legs.append({"kind": "recorded", "coordinates": after, **({"fixes": fixes[index]} if fixes else {})})
    return legs


def route_gaps(pieces, origin, destination):
    """Straight [lon, lat] legs over a trip's unrecorded stretches (see route_legs)."""
    return [leg["coordinates"] for leg in route_legs(pieces, origin, destination) if leg["kind"] == "gap"]


def mostly_unrecorded(legs):
    """Whether nobody recorded most of a trip's way, so a known route between its places draws it better."""
    return legs_length_m(legs, "gap") > legs_length_m(legs, "recorded")


def legs_length_m(legs, kind):
    """How far a trip's legs of one kind run."""
    return sum(metres(a[1], a[0], b[1], b[0]) for leg in legs if leg["kind"] == kind
               for a, b in zip(leg["coordinates"], leg["coordinates"][1:]))


def smooth_route_segment(coordinates, samples_per_leg=4):
    """Interpolate a GPS path while passing through every recorded fix."""
    if len(coordinates) < 3:
        return coordinates
    result = []
    for index in range(len(coordinates) - 1):
        p0 = coordinates[max(0, index - 1)]
        p1 = coordinates[index]
        p2 = coordinates[index + 1]
        p3 = coordinates[min(len(coordinates) - 1, index + 2)]
        for step in range(samples_per_leg):
            t = step / samples_per_leg
            t2, t3 = t * t, t * t * t
            point = []
            for axis in (0, 1):
                value = .5 * ((2 * p1[axis]) + (-p0[axis] + p2[axis]) * t
                              + (2*p0[axis] - 5*p1[axis] + 4*p2[axis] - p3[axis]) * t2
                              + (-p0[axis] + 3*p1[axis] - 3*p2[axis] + p3[axis]) * t3)
                point.append(min(max(value, min(p1[axis], p2[axis])), max(p1[axis], p2[axis])))
            result.append(point)
    result.append(coordinates[-1])
    return result
