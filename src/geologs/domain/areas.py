"""Area stays: time spent moving around one place (a lake loop, a park, a large store)."""
import math
from datetime import datetime

from .geo import metres
from .motion import classify_motion, MAX_CONNECTED_GAP_SECONDS, MAX_PLAUSIBLE_SPEED_MPS, reported_speed
from .settings import ANALYSIS_DEFAULTS
from .stay_detection import accurate_enough, min_stay_seconds, STOP_RADIUS_M, STRAY_MAX_POINTS


# Area stays cover time spent moving around one place: a lake loop, a park, a
# large store. Each window must last as long as a stationary stay, fit inside
# one of these bounding-box diagonals (tightest first), and circulate: its path
# must be much longer than its extent. A loop scores about 2.2 per lap, an
# out-and-back 2.0, and travel that passes through about 1.0.
AREA_SCALES_M = (250, 500, 1000, 1500, 2000, 3000)
AREA_MIN_CIRCULATION = 1.8
AREA_SAMPLE_SECONDS = 30
# Moves at or above the cycling limit (Settings) split areas; errands driven
# at 30 km/h between short stops would otherwise look like one circulating area.
AREA_MIN_ACTIVITY_EXTENT_M = 150
# Everything between parking and driving away is one visit (an out-and-back
# trail run) when it circulates as a whole and stays within this extent.
AREA_PARKED_MAX_EXTENT_M = 5000


def _area_states(points):
    """Sample accurate fixes at most every AREA_SAMPLE_SECONDS.

    Repeated positions collapse into one state that keeps its first and last
    time, so a Home Assistant position held for several polls counts once.
    """
    states = []
    for point in points:
        if not accurate_enough(point):
            continue
        at = datetime.fromisoformat(point["timestamp"])
        if states:
            last = states[-1]
            same_spot = metres(last["lat"], last["lon"], point["lat"], point["lon"]) <= 1
            if same_spot or (at - last["first_at"]).total_seconds() < AREA_SAMPLE_SECONDS:
                last.update(last_at=at, ended_at=point["timestamp"], end_id=point["id"], count=last["count"] + 1)
                continue
        states.append({"lat": point["lat"], "lon": point["lon"], "first_at": at, "last_at": at,
                       "started_at": point["timestamp"], "ended_at": point["timestamp"],
                       "start_id": point["id"], "end_id": point["id"], "count": 1,
                       "reported": reported_speed(point)})
    return states


def _state_move(previous, state):
    """Distance and conservative duration: a held state may have moved at any time after its first fix."""
    return (metres(previous["lat"], previous["lon"], state["lat"], state["lon"]),
            (state["first_at"] - previous["first_at"]).total_seconds())


def _area_runs(states, rules=None):
    """Split states at coverage gaps, implausible jumps, and driving-speed moves.

    Each run records whether a drive leads into it and out of it: a run
    between two drives is the time from parking to driving away.
    """
    rules = rules or ANALYSIS_DEFAULTS
    runs, current, after_drive = [], [], False
    for state in states:
        if current:
            previous = current[-1]
            distance, seconds = _state_move(previous, state)
            gap = (state["first_at"] - previous["last_at"]).total_seconds()
            speed = distance / seconds if seconds > 0 else float("inf")
            driving = max(speed, state["reported"] or 0) * 3.6 >= rules["cycling_max_kmh"] and distance >= 150
            if gap > MAX_CONNECTED_GAP_SECONDS or speed > MAX_PLAUSIBLE_SPEED_MPS or driving:
                runs.append((current, after_drive and driving))
                current, after_drive = [], driving
        current.append(state)
    if current:
        runs.append((current, False))
    return runs


def _run_windows(states, parked=False, rules=None):
    """Find maximal circulating windows in one connected run, tightest area first."""
    n = len(states)
    stay_seconds = min_stay_seconds(rules)
    if n < 3 or (states[-1]["last_at"] - states[0]["first_at"]).total_seconds() < stay_seconds:
        return []
    lat0, lon0 = states[0]["lat"], states[0]["lon"]
    x_scale = 111320 * math.cos(math.radians(lat0))
    xy = [((state["lon"] - lon0) * x_scale, (state["lat"] - lat0) * 110574) for state in states]
    path = [0.0]
    for previous, state in zip(states, states[1:]):
        path.append(path[-1] + _state_move(previous, state)[0])

    def extent(i, j):
        xs = [x for x, _ in xy[i:j + 1]]
        ys = [y for _, y in xy[i:j + 1]]
        return math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    def valid(i, j):
        return (j - i >= 2
                and (states[j]["last_at"] - states[i]["first_at"]).total_seconds() >= stay_seconds
                and path[j] - path[i] >= AREA_MIN_CIRCULATION * max(extent(i, j), STOP_RADIUS_M))

    def trim(i, j):
        # Drop approach and exit legs that reach beyond the core of the area.
        for _ in range(4):
            cx = sum(x for x, _ in xy[i:j + 1]) / (j - i + 1)
            cy = sum(y for _, y in xy[i:j + 1]) / (j - i + 1)
            distances = [math.hypot(x - cx, y - cy) for x, y in xy[i:j + 1]]
            limit = max(sorted(distances)[int(.8 * (len(distances) - 1))] * 1.25, STOP_RADIUS_M)
            a, b = i, j
            while b > a and distances[b - i] > limit:
                b -= 1
            while a < b and distances[a - i] > limit:
                a += 1
            if (a, b) == (i, j) or not valid(a, b):
                break
            i, j = a, b
        return i, j

    taken = [False] * n
    spans = []
    for scale in AREA_SCALES_M:
        i = 0
        while i < n:
            if taken[i]:
                i += 1
                continue
            low_x = high_x = xy[i][0]
            low_y = high_y = xy[i][1]
            j = i
            while j + 1 < n and not taken[j + 1]:
                x, y = xy[j + 1]
                box = min(low_x, x), max(high_x, x), min(low_y, y), max(high_y, y)
                if math.hypot(box[1] - box[0], box[3] - box[2]) > scale:
                    break
                low_x, high_x, low_y, high_y = box
                j += 1
            if not valid(i, j):
                i += 1
                continue
            i, j = trim(i, j)
            spans.append((i, j))
            for index in range(i, j + 1):
                taken[index] = True
            i = j + 1
    spans.sort()
    windows = [_area_window(states, path, i, j, rules) for i, j in spans]
    # Adjacent windows over overlapping ground are one visit found at two scales.
    merged = []
    for (i, j), window in zip(spans, windows):
        if merged:
            (first, last), previous = merged[-1]
            touching = i - last - 1 <= STRAY_MAX_POINTS
            overlapping = metres(previous["lat"], previous["lon"], window["lat"], window["lon"]) <= previous["radius_m"] + window["radius_m"]
            if touching and overlapping:
                merged[-1] = ((first, j), _area_window(states, path, first, j, rules))
                continue
        merged.append(((i, j), window))
    # Between parking and driving away, one circulating visit is the whole
    # outing: an out-and-back trail run circulates only as a whole. Several
    # windows mean several places (two stores), so they stay separate.
    if parked and len(merged) == 1 and valid(0, n - 1) and extent(0, n - 1) <= AREA_PARKED_MAX_EXTENT_M:
        return [_area_window(states, path, 0, n - 1, rules)]
    return [window for _, window in merged]


def _area_window(states, path, i, j, rules=None):
    chosen = states[i:j + 1]
    lat = sum(state["lat"] for state in chosen) / len(chosen)
    lon = sum(state["lon"] for state in chosen) / len(chosen)
    radius = max(metres(lat, lon, state["lat"], state["lon"]) for state in chosen)
    moves = [(*_state_move(previous, state), state["reported"]) for previous, state in zip(chosen, chosen[1:])]
    rules = rules or ANALYSIS_DEFAULTS
    motion = classify_motion(moves, rules)
    activity = motion["mode"] if motion else None
    # Pottering inside a small area (or GPS jitter indoors) is a visit, not exercise.
    if motion and radius * 2 < AREA_MIN_ACTIVITY_EXTENT_M and motion["median_kmh"] < rules["walking_max_kmh"]:
        activity = None
    return {"start_id": chosen[0]["start_id"], "end_id": chosen[-1]["end_id"],
            "started_at": chosen[0]["started_at"], "ended_at": chosen[-1]["ended_at"],
            "lat": lat, "lon": lon, "radius_m": radius, "distance_m": path[j] - path[i],
            "point_count": sum(state["count"] for state in chosen), "activity": activity}


def area_windows(points, rules=None):
    """Area stays in chronological fixes that contain no stationary stay."""
    windows = []
    for run, parked in _area_runs(_area_states(points), rules):
        windows.extend(window for window in _run_windows(run, parked, rules) if window["activity"] != "driving")
    return windows


def returns_to_origin(window, previous, following):
    """A loop that leaves one stay and comes back to it is a round trip, not a visit.

    A dog walk around the block from Home stays a Home -> Home walk; a run
    around a lake that you drove to is a visit to the lake.
    """
    if not previous or not following:
        return False
    if metres(previous["lat"], previous["lon"], following["lat"], following["lon"]) > 2 * STOP_RADIUS_M:
        return False
    reach = window["radius_m"] + STOP_RADIUS_M
    return all(metres(window["lat"], window["lon"], stay["lat"], stay["lon"]) <= reach for stay in (previous, following))


def went_elsewhere_flag(value):
    return None if value is None else int(bool(value))
