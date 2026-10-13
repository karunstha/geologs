"""Finding stays in a stream of fixes, one fix at a time."""
from datetime import datetime, timedelta

from .geo import metres
from .motion import MOVING_MPS, reported_speed
from .settings import ANALYSIS_DEFAULTS


# Apps such as OwnTracks and Overland report only while you move, so silence
# usually means you stayed put. A quiet stretch up to this long that ends at
# the same spot is time spent there, and one that ends elsewhere is time there
# until you left (estimated from the distance to the next fix).
MAX_QUIET_STAY_SECONDS = 16 * 3600
QUIET_DEPARTURE_SPEED_MPS = 30 / 3.6


STOP_RADIUS_M = 50
# A quick stop (a fuel stop, a pickup) is shorter than a stay, so it must be
# stiller: every fix within this of the stop's centre (less its accuracy),
# none coarser than this. The centre, not the first fix: the first is often
# you still rolling in.
# Creeping traffic moves farther than that over a few minutes; a red light
# rarely lasts as long as the quick-stop minimum.
QUICK_STOP_REACH_M = 30
QUICK_STOP_MAX_ACCURACY_M = 50
# A candidate keeps its fixes for that measure until it has this many; a
# stop with more fixes than this lasts too long to be a quick stop anyway.
QUICK_STOP_MAX_FIXES = 200
# Dense fixes that end this far from where they began, along a path at least
# this straight, are creeping traffic.
CREEP_MIN_NET_M = 40
CREEP_STRAIGHTNESS = .7
# A quick stop is on the way somewhere: it is entered and left while tracked,
# with no longer gap than this on either side.
QUICK_STOP_MAX_GAP_SECONDS = 300
# Apps that report only while moving go quiet when you stop. A quiet stretch
# of at least QUIET_MIN_SECONDS (longer than such apps' interval on the move,
# so it never applies to steady polling) that ends this close (allowing for
# accuracy) and this slowly is time spent still, even if the phone was
# already pulling away.
QUIET_MIN_SECONDS = 120
QUIET_STILL_MPS = 0.5
QUIET_ACCURACY_ALLOWANCE_M = 30
# Leaving after a quiet stretch, the stay lasts until shortly before the next
# fix; that estimate is trusted for a quick stop when the fix is this close.
QUIET_DEPARTURE_MAX_M = 1500
# A gap at least this long before a departure is quiet, not a fix missed on the move.
QUIET_DEPARTURE_MIN_SECONDS = 180
# A quick stop backed by a fix at least this often needs nothing more; a
# sparser one (a phone that went quiet at the pump) is only kept with a named,
# saved, or corrected place this close: a road or a red light has none.
DENSE_STOP_SECONDS_PER_FIX = 90
# A stay survives this many consecutive stray fixes when the next fix is back
# inside it; indoor GPS often jumps 60-100 m for a sample or two.
STRAY_MAX_POINTS = 2
STRAY_MAX_SECONDS = 300
# ...as long as its accuracy can explain the jump. A sharp fix farther out
# (200 m away at 3 m accuracy, as you pull away from the pump) is a departure.
STRAY_MAX_DISTANCE_M = 150


def candidate_duration(candidate):
    end = candidate.get("left_at") or candidate["end"]
    return (datetime.fromisoformat(end) - datetime.fromisoformat(candidate["start"])).total_seconds()


def min_stay_seconds(rules):
    """How long a stay must last, from the Settings in `rules`."""
    return (rules or ANALYSIS_DEFAULTS)["stay_min_minutes"] * 60


def accurate_enough(point):
    return (point["accuracy_m"] is None or point["accuracy_m"] <= 100)


def new_candidate(point, previous=None, after_stay=False):
    """previous is the fix before `point`; after_stay, whether it ended a stay."""
    arrival_gap = arrival_m = None
    if previous:
        arrival_gap = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(previous["timestamp"])).total_seconds()
        arrival_m = metres(previous["lat"], previous["lon"], point["lat"], point["lon"])
    return {"start_id": point["id"], "end_id": point["id"], "start": point["timestamp"], "end": point["timestamp"],
            "first_lat": point["lat"], "first_lon": point["lon"], "lat": point["lat"], "lon": point["lon"], "count": 1,
            "reach": 0.0, "worst_accuracy": point["accuracy_m"] or 0, "arrival_gap": arrival_gap,
            "arrival_m": arrival_m, "after_stay": after_stay, "moving": (reported_speed(point) or 0) > MOVING_MPS,
            "fixes": [[point["lat"], point["lon"], point["accuracy_m"] or 0]],
            "event": point.get("external_id"), "proven_end": point["timestamp"]}


def candidate_kind(candidate, ongoing, rules):
    """'stationary' for a stay, 'stop' or 'pause' for a quick stop on the way, else None.

    A quick stop must be proven still: by fresh fixes (a Home Assistant state
    held for several polls, one external_id, may hide driving, so only the
    time until its last new state counts) or by the quiet of an app that
    reports only while moving. It must be entered while tracked and left
    either while tracked or for somewhere close, and is only known once left.
    A 'stop' has fixes close together within QUICK_STOP_REACH_M; a sparser
    or looser one is a 'pause', kept only where there is a place (stays()).
    """
    if candidate is None:
        return None
    if candidate["count"] < 2:
        return "stationary" if lone_stay_start(candidate, rules) else None
    if candidate_duration(candidate) >= min_stay_seconds(rules):
        return "stationary"
    quick_minutes = (rules or ANALYSIS_DEFAULTS)["quick_stop_minutes"]
    if not quick_minutes or ongoing or "reach" not in candidate:  # older stored candidates never qualify
        return None
    proven_end = max(datetime.fromisoformat(value) for value in (candidate["proven_end"], candidate.get("left_at"))
                     if value)
    proven = (proven_end - datetime.fromisoformat(candidate["start"])).total_seconds()
    arrived = candidate["arrival_gap"] is not None and candidate["arrival_gap"] <= QUICK_STOP_MAX_GAP_SECONDS
    departure_gap = candidate.get("departure_gap")
    left = departure_gap is not None and (departure_gap <= QUICK_STOP_MAX_GAP_SECONDS
                                          or (candidate.get("departure_m") or float("inf")) <= QUIET_DEPARTURE_MAX_M)
    if proven < quick_minutes * 60 or not (arrived and left) or candidate["worst_accuracy"] > QUICK_STOP_MAX_ACCURACY_M:
        return None
    dense = candidate["count"] >= max(2, proven / DENSE_STOP_SECONDS_PER_FIX)
    if dense and _creeping(candidate):
        return None
    spread = _spread(candidate)
    # Frequent fixes that never stray from where the stop began need no place to confirm them.
    if dense and spread <= QUICK_STOP_REACH_M and candidate["reach"] <= QUICK_STOP_REACH_M:
        return "stop"
    loose = STOP_RADIUS_M + min(candidate["worst_accuracy"], QUIET_ACCURACY_ALLOWANCE_M)
    return "pause" if spread <= loose else None


def lone_stay_start(candidate, rules):
    """When a candidate's one fix is a stay, its estimated arrival; else None.

    An app that reports only while moving may send a single fix from a place
    it then stays at. Quiet on both sides makes it a stay, as long as the fix
    was not reported or reached on the move. You arrived about as soon as you
    could get there from the fix before (unless that ended a stay, which
    already claims the quiet), and left about when it takes to reach the next.
    """
    left_at, gap, distance = candidate.get("left_at"), candidate["arrival_gap"], candidate.get("arrival_m")
    if (left_at is None or gap is None or distance is None or candidate.get("moving", True)
            or candidate["worst_accuracy"] > QUICK_STOP_MAX_ACCURACY_M):
        return None
    start = datetime.fromisoformat(candidate["start"])
    if gap < QUIET_DEPARTURE_MIN_SECONDS:
        if gap <= 0 or distance / gap > MOVING_MPS:
            return None
    elif not candidate.get("after_stay"):
        start = min(start, start - timedelta(seconds=gap) + timedelta(seconds=distance / QUIET_DEPARTURE_SPEED_MPS))
    if (datetime.fromisoformat(left_at) - start).total_seconds() < min_stay_seconds(rules):
        return None
    return start.isoformat(timespec="seconds")


def _creeping(candidate):
    """Frequent fixes that keep heading one way: stop-and-go traffic, not a stop.

    A parked car's fixes wander around one spot; a queue moves on steadily, so
    its fixes end far from where they began along a fairly straight path.
    """
    fixes = candidate.get("fixes")
    if not fixes or len(fixes) < 3:
        return False
    path = sum(metres(a[0], a[1], b[0], b[1]) for a, b in zip(fixes, fixes[1:]))
    net = metres(fixes[0][0], fixes[0][1], fixes[-1][0], fixes[-1][1])
    return net >= CREEP_MIN_NET_M and net >= CREEP_STRAIGHTNESS * path


def _spread(candidate):
    """How far the stop's fixes lie from its centre, beyond what their accuracy explains."""
    fixes = candidate.get("fixes")
    if not fixes:  # past QUICK_STOP_MAX_FIXES, or stored before fixes were kept
        return candidate["reach"]
    return max(max(0.0, metres(lat, lon, candidate["lat"], candidate["lon"]) - accuracy) for lat, lon, accuracy in fixes)


def stray_fix(point):
    """The fields a held-back stray fix needs to be replayed later."""
    return {key: point.get(key) for key in ("id", "timestamp", "lat", "lon", "accuracy_m", "speed_mps", "speed_origin",
                                            "external_id")}


def stay_record(candidate, ongoing, rules=None):
    """The stay a candidate makes, or None while it is none (or it turned out to be traffic)."""
    kind = candidate_kind(candidate, ongoing, rules)
    if kind is None:
        return None
    return {"start_id": candidate["start_id"], "end_id": candidate["end_id"],
            "started_at": candidate["start"] if candidate["count"] >= 2 else lone_stay_start(candidate, rules),
            "ended_at": candidate.get("left_at") or candidate["end"],
            "lat": candidate["lat"], "lon": candidate["lon"], "count": candidate["count"], "ongoing": ongoing, "kind": kind}


def report(candidate, ongoing, rules, stays):
    """Tell `stays` what a candidate that grew or ended makes now, and remember whether it is kept.

    `stays` keeps a stay (keep(record), see stay_record) and drops one that
    turned out not to be (withdraw(start_id)): a quick stop that was creeping traffic.
    """
    record = stay_record(candidate, ongoing, rules)
    if record is not None:
        stays.keep(record)
        candidate["saved"] = True
    elif candidate.get("saved"):
        stays.withdraw(candidate["start_id"])
        candidate["saved"] = False


def advance(candidate, point, rules, stays, previous=None, after_stay=False):
    """Feed one accurate fix to the stationary-stay candidate and return the new candidate.

    Whenever the candidate grows or ends, `stays` hears what it makes (see
    report). previous is the fix before `point` when no candidate is open,
    and after_stay whether it ended a stay.
    """
    if candidate is None:
        return new_candidate(point, previous, after_stay)
    elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(candidate["end"])).total_seconds()
    strays = candidate.get("strays") or []
    to_centre = metres(point["lat"], point["lon"], candidate["lat"], candidate["lon"])
    if 0 < elapsed <= MAX_QUIET_STAY_SECONDS:
        moving = (reported_speed(point) or 0) > MOVING_MPS
        # The first fix anchors the stay so a slow creep cannot drag it along,
        # but it is often you still rolling in: allow for both fixes' accuracy.
        first_accuracy = (candidate.get("fixes") or [[0, 0, 0]])[0][2]
        anchor = STOP_RADIUS_M + min((point["accuracy_m"] or 0) + first_accuracy, QUIET_ACCURACY_ALLOWANCE_M)
        # Back at the same spot after any quiet stretch (an app that reports
        # only while moving): you never left.
        inside = (not moving
                  and metres(point["lat"], point["lon"], candidate["first_lat"], candidate["first_lon"]) <= anchor
                  and to_centre <= STOP_RADIUS_M)
        allowance = min((point["accuracy_m"] or 0) + (candidate.get("last_accuracy") or 0), QUIET_ACCURACY_ALLOWANCE_M)
        # Held-back strays mean fixes kept coming: that is not quiet.
        quiet = not strays and elapsed >= QUIET_MIN_SECONDS and to_centre <= STOP_RADIUS_M + allowance and to_centre / elapsed < QUIET_STILL_MPS
        if inside or quiet:
            # Back inside: the held-back fixes were GPS strays, not a departure.
            candidate.pop("strays", None)
            count = candidate["count"] + 1
            candidate["lat"] += (point["lat"] - candidate["lat"]) / count
            candidate["lon"] += (point["lon"] - candidate["lon"]) / count
            candidate["count"] = count
            candidate["end"] = point["timestamp"]
            candidate["end_id"] = point["id"]
            candidate["last_accuracy"] = point["accuracy_m"] or 0
            if "reach" in candidate:  # stored JSON stays finite: older candidates lack it and never qualify
                candidate["reach"] = max(candidate["reach"],
                                         metres(point["lat"], point["lon"], candidate["first_lat"], candidate["first_lon"]))
                candidate["worst_accuracy"] = max(candidate["worst_accuracy"], point["accuracy_m"] or 0)
                if candidate.get("fixes") is not None:
                    candidate["fixes"].append([point["lat"], point["lon"], point["accuracy_m"] or 0])
                    if len(candidate["fixes"]) > QUICK_STOP_MAX_FIXES:
                        candidate["fixes"] = None
                if point.get("external_id") is None or point.get("external_id") != candidate["event"]:
                    candidate["event"], candidate["proven_end"] = point.get("external_id"), point["timestamp"]
            report(candidate, True, rules, stays)
            return candidate
        stray = to_centre - (point["accuracy_m"] or 0) <= STRAY_MAX_DISTANCE_M
        if candidate["count"] >= 2 and stray and len(strays) < STRAY_MAX_POINTS and elapsed <= STRAY_MAX_SECONDS:
            candidate["strays"] = strays + [stray_fix(point)]
            return candidate
    # A real departure (or gap) ends the stay at its last inside fix. The
    # held-back fixes belong to whatever comes next, so replay them.
    candidate["departure_gap"] = elapsed
    if QUIET_DEPARTURE_MIN_SECONDS <= elapsed <= MAX_QUIET_STAY_SECONDS and not strays:
        # Left after a quiet stretch: you were here until about the time it
        # takes to reach the next fix.
        speed = max(reported_speed(point) or 0, QUIET_DEPARTURE_SPEED_MPS)
        left = datetime.fromisoformat(point["timestamp"]) - timedelta(seconds=to_centre / speed)
        candidate["left_at"] = max(datetime.fromisoformat(candidate["end"]), left).isoformat(timespec="seconds")
        candidate["departure_m"] = round(to_centre, 1)
    report(candidate, False, rules, stays)
    last = (candidate.get("fixes") or [[candidate["lat"], candidate["lon"]]])[-1]
    previous = {"timestamp": candidate["end"], "lat": last[0], "lon": last[1]}
    after_stay, candidate = bool(candidate.get("saved")), None
    for item in strays + [point]:
        candidate = advance(candidate, item, rules, stays, previous, after_stay)
        previous, after_stay = item, False
    return candidate
