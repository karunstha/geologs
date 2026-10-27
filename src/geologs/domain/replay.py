"""Timing a drawn way for a replay: when the moving dot passes each point of it."""
from bisect import bisect_right

from .geo import metres

# A replay's points closer than this to the last one kept are dropped: the dot moves no differently.
REPLAY_STEP_M = 5


def timed_path(coordinates, fixes, start, end):
    """A drawn way's [lon, lat] points as [seconds, lon, lat], from `start` at its first point to
    `end` at its last (seconds since the epoch).

    A way is drawn through every fix it recorded (smoothing keeps each one as a point, and a gap
    runs from fix to fix), so a point that is one of `fixes` ([seconds, lon, lat], in time order)
    passes at that fix's time. Between two such points, and along a filled or guessed stretch no
    fix lies on, the time runs evenly with distance. A fix held in place, or one the way does not
    pass through, times nothing.
    """
    points = []
    for lon, lat in coordinates:
        if not points or [lon, lat] != points[-1]:
            points.append([lon, lat])
    if not points:
        return []
    along = [0.0]
    for a, b in zip(points, points[1:]):
        along.append(along[-1] + metres(a[1], a[0], b[1], b[0]))
    at_point = {}
    for at, (lon, lat) in enumerate(points[1:-1], 1):
        at_point.setdefault((lon, lat), []).append(at)
    anchors, index = {0: start}, 0
    for seconds, lon, lat in fixes:
        if not start < seconds < end:
            continue
        # The way's next point at this fix (it may pass the same spot again later).
        found = at_point.get((lon, lat), [])
        later = bisect_right(found, index)
        if later < len(found):
            index = found[later]
            anchors[index] = seconds
    anchors[len(points) - 1] = end
    timed, keys = [], sorted(anchors)
    for first, last in zip(keys, keys[1:]):
        span = along[last] - along[first]
        for at in range(first, last):
            share = (along[at] - along[first]) / span if span else (at - first) / (last - first)
            timed.append([anchors[first] + share * (anchors[last] - anchors[first]), *points[at]])
    timed.append([anchors[keys[-1]], *points[-1]])
    return thinned(timed)


def thinned(timed, step_m=REPLAY_STEP_M):
    """Timed points without those within step_m of the last kept; the first and last always stay."""
    kept = timed[:1]
    for point in timed[1:-1]:
        if metres(kept[-1][2], kept[-1][1], point[2], point[1]) >= step_m:
            kept.append(point)
    if len(timed) > 1:
        kept.append(timed[-1])
    return kept
