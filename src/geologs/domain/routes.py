"""Drawing recorded paths: which fixes connect, and the legs between them nobody recorded."""
from .geo import metres
from .motion import connected_elapsed, same_source_state


# A trip's unrecorded leg shorter than this (its first fix inside the stay it left) is not drawn.
ROUTE_GAP_MIN_M = 25


def route_segments_from_points(points, keep_lone=False):
    """Connect plausible fixes per source and skip readings too inaccurate to route.

    keep_lone also returns a fix connected to neither neighbour, as a one-point segment.
    """
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
                current = [[point["lon"], point["lat"]]]
            else:
                current.append([point["lon"], point["lat"]])
            if previous is None or not same_source_state(previous, point):
                held_since = point
            previous = point
        if len(current) > 1 or current and keep_lone:
            segments.append(current)
    return segments


def route_legs(pieces, origin, destination):
    """A trip's way from one stay to the next, in order: {"kind", "coordinates"} legs.

    Recorded pieces (in time order) are "recorded" legs; where they stop short
    of the stays or of each other, a straight "gap" leg joins them: you still
    went that way. A sparse tracker leaves most of a trip like this, and a fix
    on its own is only a point the way passed through.
    """
    ends = [[[origin["lon"], origin["lat"]]], *pieces, [[destination["lon"], destination["lat"]]]]
    legs = []
    for index, (before, after) in enumerate(zip(ends, ends[1:])):
        if metres(before[-1][1], before[-1][0], after[0][1], after[0][0]) > ROUTE_GAP_MIN_M:
            legs.append({"kind": "gap", "coordinates": [before[-1], after[0]]})
        if index < len(pieces) and len(after) > 1:
            legs.append({"kind": "recorded", "coordinates": after})
    return legs


def route_gaps(pieces, origin, destination):
    """Straight [lon, lat] legs over a trip's unrecorded stretches (see route_legs)."""
    return [leg["coordinates"] for leg in route_legs(pieces, origin, destination) if leg["kind"] == "gap"]


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
