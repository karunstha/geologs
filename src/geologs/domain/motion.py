"""Speeds and travel modes read from consecutive fixes."""
from datetime import datetime

from .geo import metres
from .settings import ANALYSIS_DEFAULTS


MAX_CONNECTED_GAP_SECONDS = 900
MAX_PLAUSIBLE_SPEED_MPS = 55.6
# Transit (bus, train) is never guessed from speed; it only comes from a correction.
# Being driven (passenger: no fuel is yours) is never guessed either; you mark the drive.
MOVEMENT_MODES = ("driving", "passenger", "transit", "cycling", "running", "walking")
# Short moves cannot be told from driving by speed alone; a drive needs this much distance.
DRIVING_MIN_DISTANCE_M = 500


def mode_for(speed_mps, accuracy_m=None, speeds=None):
    """One fix's mode from its own speed; `speeds` holds the Settings thresholds."""
    if speed_mps is None or (accuracy_m is not None and accuracy_m > 150):
        return "unknown"
    speeds = speeds or ANALYSIS_DEFAULTS
    kmh = speed_mps * 3.6
    if kmh < 1.5:
        return "idle"
    if kmh < speeds["walking_max_kmh"]:
        return "walking"
    if kmh < speeds["cycling_max_kmh"]:
        return "running_cycling"
    return "driving" if kmh < 200 else "unknown"


def select_best_timed_fixes(points):
    """Keep one accuracy-best observation per source timestamp for trip timing."""
    selected = {}
    for point in points:
        key = (point["source"], point["timestamp"])
        previous = selected.get(key)
        accuracy = point["accuracy_m"] if point["accuracy_m"] is not None else float("inf")
        previous_accuracy = previous["accuracy_m"] if previous and previous["accuracy_m"] is not None else float("inf")
        if previous is None or accuracy < previous_accuracy or (accuracy == previous_accuracy and point.get("id", 0) > previous.get("id", 0)):
            selected[key] = point
    return sorted(selected.values(), key=lambda point: (point["timestamp"], point.get("id", 0)))


def same_source_state(previous, point):
    """True when polling returned the same source event at the same position."""
    external_id = previous.get("external_id")
    return (previous["source"] == point["source"] and external_id is not None
            and external_id == point.get("external_id")
            and metres(previous["lat"], previous["lon"], point["lat"], point["lon"]) <= 50)


def connected_elapsed(previous, point, held_since=None):
    """Return a plausible edge duration, including a short held-source window."""
    if previous["source"] != point["source"]:
        return None
    accurate = all(item["accuracy_m"] is None or item["accuracy_m"] <= 150 for item in (previous, point))
    if not accurate:
        return None
    current_at = datetime.fromisoformat(point["timestamp"])
    # Home Assistant may return one unchanged state for several polls while the
    # device is moving. The transition could have happened anywhere inside that
    # short window, so its first observation gives the conservative duration;
    # timing from the last poll would turn a run into a drive.
    starts = [previous]
    if held_since is not None and held_since is not previous and same_source_state(held_since, previous):
        starts.insert(0, held_since)
    for start in starts:
        elapsed = (current_at - datetime.fromisoformat(start["timestamp"])).total_seconds()
        distance = metres(start["lat"], start["lon"], point["lat"], point["lon"])
        if 0 < elapsed <= MAX_CONNECTED_GAP_SECONDS and distance / elapsed <= MAX_PLAUSIBLE_SPEED_MPS:
            return elapsed
    return None


def reported_speed(point):
    """Source-reported speed only; stored estimates repeat the chord speed."""
    if point.get("speed_origin") in ("reported", "reported_adjusted"):
        return point.get("speed_mps")
    return None


def _weighted_percentile(samples, fraction):
    """Percentile of (value, weight) pairs, so long moves outweigh short jitter."""
    ordered = sorted(samples)
    target = sum(weight for _, weight in ordered) * fraction
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0] if ordered else 0.0


def move_uncertainty(previous, point):
    """Half the combined accuracy radii: jumps between coarse indoor fixes are not speed."""
    return min((previous.get("accuracy_m") or 0) + (point.get("accuracy_m") or 0), 200) / 2


def classify_motion(moves, speeds=None):
    """Estimate walking, running, cycling, or driving from connected moves.

    `moves` holds (distance_m, seconds, reported_speed_mps or None) with an
    optional fourth positional uncertainty in metres, which is subtracted
    before speed is estimated. Speeds are weighted by distance, so a single
    GPS jump cannot turn a walk into a drive. Running and cycling overlap
    between about 12 and 16 km/h; the median speed decides, which suits
    recreational runs and commuter cycling. A median of 25 km/h or more is
    treated as driving: city traffic sampled once a minute averages about
    that, while few riders sustain it. Speeds computed from positions need
    DRIVING_MIN_DISTANCE_M before they can mean a drive; a device-reported
    speed is trusted over any distance. `speeds` holds the Settings
    thresholds; the figures above are their defaults.
    """
    speeds = speeds or ANALYSIS_DEFAULTS
    fast_kmh = speeds["driving_min_kmh"]
    samples, reported_fast, reported_any = [], 0.0, False
    for distance, seconds, reported, *uncertainty in moves:
        if seconds <= 0 or distance < 10:
            continue
        speed = max(max(0.0, distance - (uncertainty[0] if uncertainty else 0)) / seconds, reported or 0)
        samples.append((speed * 3.6, distance))
        if (reported or 0) * 3.6 >= fast_kmh:
            reported_fast += distance
        reported_any = reported_any or reported is not None
    if not samples:
        return None
    total = sum(distance for _, distance in samples)
    fast = sum(distance for kmh, distance in samples if kmh >= fast_kmh)
    median = _weighted_percentile(samples, .5)
    credible = total >= DRIVING_MIN_DISTANCE_M or reported_fast >= .35 * total
    if credible and (fast >= 1000 or fast >= .35 * total or median >= speeds["cycling_max_kmh"]):
        mode = "driving"
    elif median >= speeds["running_max_kmh"]:
        mode = "cycling"
    elif median >= speeds["walking_max_kmh"]:
        mode = "running"
    else:
        mode = "walking"
    moving_seconds = sum(move[1] for move in moves if move[1] > 0 and move[0] >= 10)
    return {"mode": mode, "median_kmh": round(median, 1), "distance_m": total, "moving_seconds": moving_seconds,
            "speed_reported": reported_any}


# A fix reported (or reached) faster than this is still on the move.
MOVING_MPS = 2.5
