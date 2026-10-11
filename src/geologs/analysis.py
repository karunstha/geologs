"""Observed stays and repeatable place patterns derived from source GPS samples."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import (ANALYSIS_DEFAULTS, MAX_CONNECTED_GAP_SECONDS, MAX_QUIET_STAY_SECONDS, QUIET_DEPARTURE_SPEED_MPS, MODE_OVERRIDE_MIN_OVERLAP, MAX_PLAUSIBLE_SPEED_MPS, MAX_SAMPLE_GAP_SECONDS, MOVEMENT_MODES,
                   _label_place, classify_motion, is_excursion, is_home_place, label_at, place_in_zone, zone_at, mode_override_for, went_elsewhere_between, windows_overlap, metres, name_matches, named_place_near,
                   nearby_places,
                   parse_time, place_for_coordinate,
                   places_provider_identity, reported_speed, search_text, street_key,
                   route_segments_from_points, smooth_route_segment, stats_from_points, trips_between_stays)


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
# A quick stop backed by a fix at least this often needs nothing more; a
# sparser one (a phone that went quiet at the pump) is only kept with a named,
# saved, or corrected place this close: a road or a red light has none.
DENSE_STOP_SECONDS_PER_FIX = 90
POSSIBLE_STOP_PLACE_M = 75
# A stay survives this many consecutive stray fixes when the next fix is back
# inside it; indoor GPS often jumps 60-100 m for a sample or two.
STRAY_MAX_POINTS = 2
STRAY_MAX_SECONDS = 300
# ...as long as its accuracy can explain the jump. A sharp fix farther out
# (200 m away at 3 m accuracy, as you pull away from the pump) is a departure.
STRAY_MAX_DISTANCE_M = 150
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
# Places known only by a street address merge with others on the same street
# this close: indoor GPS drifts 100 m or more, and every drift picks another
# house number.
ADDRESS_MERGE_M = 200
# Name-only reminders check the places around you after this long still. A
# full 15 minute stay would miss a quick coffee run.
NAME_REMINDER_DWELL_SECONDS = 180
NAME_REMINDER_RADIUS_M = 150
# Return-home events shortly after midnight belong to the prior evening as
# well as the morning on which they happened. This is deliberately a factual
# window, not an inference about whether someone stayed somewhere overnight.
HOME_RETURN_GRACE_HOURS = 6


def sync_timeline(store, sources=None, rebuild=False, since_ids=None):
    """Materialize an efficient timeline from raw observations and confirmed stays.

    Samples while travelling remain available. A confirmed stationary run keeps
    only its arrival and latest/departure observations in the timeline; raw
    observations remain untouched in point_observations.
    """
    if sources is None:
        sources = [row["source"] for row in store._rows("SELECT DISTINCT source FROM point_observations")]
    since_ids = since_ids or {}
    stay_seconds = _stay_seconds(store.settings())
    for source in sources:
        since_id = 0 if rebuild else since_ids.get(source, 0)
        if rebuild:
            store.db.execute("DELETE FROM timeline_fixes WHERE source=?", (source,))
        store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT id,source,'movement',NULL FROM point_observations
            WHERE source=? AND id>? ON CONFLICT(observation_id) DO NOTHING""", (source, since_id))
        store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
            SELECT id FROM (SELECT id,ROW_NUMBER() OVER (
                PARTITION BY source,timestamp ORDER BY COALESCE(accuracy_m,1000000000),id DESC) AS rank
                FROM point_observations WHERE source=? AND timestamp IN (
                    SELECT timestamp FROM point_observations WHERE source=? AND id>?)) ranked WHERE rank>1)""",
            (source, source, since_id))
        # Closing a stay changes no observation ID, so it would not be selected
        # by the incremental end_observation_id query below. Promote its former
        # `latest` edge when the first point outside the stop arrives.
        store.db.execute("""UPDATE timeline_fixes SET role='departure' WHERE source=? AND role='latest'
            AND stay_id IN (SELECT id FROM observed_stays_v2 WHERE source=? AND ongoing=0)""",
            (source, source))
        # Area stays are movement; only stationary runs are compacted to their edges.
        stay_query = "SELECT * FROM observed_stays_v2 WHERE source=? AND kind='stationary'"
        stay_args = [source]
        if not rebuild:
            stay_query += " AND end_observation_id>?"
            stay_args.append(since_id)
        stay_query += " ORDER BY started_at,id"
        for stay in store._rows(stay_query, stay_args):
            store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, stay["started_at"], stay["ended_at"], stay["start_observation_id"], stay["end_observation_id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["start_observation_id"], source, "arrival", stay["id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["end_observation_id"], source, "latest" if stay["ongoing"] else "departure", stay["id"]))
        state = store.db.execute("SELECT candidate FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        candidate = json.loads(state["candidate"]) if state and state["candidate"] else None
        if candidate and candidate["count"] >= 2 and _duration(candidate) < stay_seconds:
            store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, candidate["start"], candidate["end"], candidate["start_id"], candidate["end_id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (candidate["start_id"], source, "arrival"))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (candidate["end_id"], source, "latest"))


def _duration(candidate):
    end = candidate.get("left_at") or candidate["end"]
    return (datetime.fromisoformat(end) - datetime.fromisoformat(candidate["start"])).total_seconds()


def _stay_seconds(rules):
    """How long a stay must last, from the Settings in `rules`."""
    return (rules or ANALYSIS_DEFAULTS)["stay_min_minutes"] * 60


def _eligible(point):
    return (point["accuracy_m"] is None or point["accuracy_m"] <= 100)


def _new_candidate(point, previous_at=None):
    arrival_gap = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(previous_at)).total_seconds() \
        if previous_at else None
    return {"start_id": point["id"], "end_id": point["id"], "start": point["timestamp"], "end": point["timestamp"],
            "first_lat": point["lat"], "first_lon": point["lon"], "lat": point["lat"], "lon": point["lon"], "count": 1,
            "reach": 0.0, "worst_accuracy": point["accuracy_m"] or 0, "arrival_gap": arrival_gap,
            "fixes": [[point["lat"], point["lon"], point["accuracy_m"] or 0]],
            "event": point.get("external_id"), "proven_end": point["timestamp"]}


def _candidate_kind(candidate, ongoing, rules):
    """'stationary' for a stay, 'stop' or 'pause' for a quick stop on the way, else None.

    A quick stop must be proven still: by fresh fixes (a Home Assistant state
    held for several polls, one external_id, may hide driving, so only the
    time until its last new state counts) or by the quiet of an app that
    reports only while moving. It must be entered while tracked and left
    either while tracked or for somewhere close, and is only known once left.
    A 'stop' has fixes close together within QUICK_STOP_REACH_M; a sparser
    or looser one is a 'pause', kept only where there is a place (stays()).
    """
    if candidate is None or candidate["count"] < 2:
        return None
    if _duration(candidate) >= _stay_seconds(rules):
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


def _save_candidate(store, source, candidate, ongoing, rules=None):
    kind = _candidate_kind(candidate, ongoing, rules)
    if kind is None:
        # A quick stop that turned out to be creeping traffic is withdrawn.
        if candidate and candidate.get("saved"):
            store.db.execute("DELETE FROM observed_stays_v2 WHERE start_observation_id=?", (candidate["start_id"],))
            candidate["saved"] = False
        return
    store.db.execute("""INSERT INTO observed_stays_v2
        (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing,kind)
        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(start_observation_id) DO UPDATE SET
        end_observation_id=excluded.end_observation_id,ended_at=excluded.ended_at,
        lat=excluded.lat,lon=excluded.lon,point_count=excluded.point_count,ongoing=excluded.ongoing,
        kind=excluded.kind,activity=NULL,distance_m=NULL,radius_m=NULL""",
        (source, candidate["start_id"], candidate["end_id"], candidate["start"], candidate.get("left_at") or candidate["end"],
         candidate["lat"], candidate["lon"], candidate["count"], int(ongoing), kind))
    candidate["saved"] = True


def _stray_fix(point):
    """The fields a held-back stray fix needs to be replayed later."""
    return {key: point.get(key) for key in ("id", "timestamp", "lat", "lon", "accuracy_m", "speed_mps", "speed_origin",
                                            "external_id")}


def _advance_stay(store, source, candidate, point, rules=None, previous_at=None):
    """Feed one accurate fix to the stationary-stay candidate and return the new candidate.

    previous_at is the time of the fix before `point` when no candidate is open.
    """
    if candidate is None:
        return _new_candidate(point, previous_at)
    elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(candidate["end"])).total_seconds()
    strays = candidate.get("strays") or []
    to_centre = metres(point["lat"], point["lon"], candidate["lat"], candidate["lon"])
    if 0 < elapsed <= MAX_QUIET_STAY_SECONDS:
        moving = (reported_speed(point) or 0) > 2.5
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
            _save_candidate(store, source, candidate, True, rules)
            return candidate
        stray = to_centre - (point["accuracy_m"] or 0) <= STRAY_MAX_DISTANCE_M
        if candidate["count"] >= 2 and stray and len(strays) < STRAY_MAX_POINTS and elapsed <= STRAY_MAX_SECONDS:
            candidate["strays"] = strays + [_stray_fix(point)]
            return candidate
    # A real departure (or gap) ends the stay at its last inside fix. The
    # held-back fixes belong to whatever comes next, so replay them.
    candidate["departure_gap"] = elapsed
    if 180 <= elapsed <= MAX_QUIET_STAY_SECONDS and not strays:
        # Left after a quiet stretch: you were here until about the time it
        # takes to reach the next fix.
        speed = max(reported_speed(point) or 0, QUIET_DEPARTURE_SPEED_MPS)
        left = datetime.fromisoformat(point["timestamp"]) - timedelta(seconds=to_centre / speed)
        candidate["left_at"] = max(datetime.fromisoformat(candidate["end"]), left).isoformat(timespec="seconds")
        candidate["departure_m"] = round(to_centre, 1)
    _save_candidate(store, source, candidate, False, rules)
    previous_at, candidate = candidate["end"], None
    for item in strays + [point]:
        candidate = _advance_stay(store, source, candidate, item, rules, previous_at)
        previous_at = item["timestamp"]
    return candidate


def _area_states(points):
    """Sample accurate fixes at most every AREA_SAMPLE_SECONDS.

    Repeated positions collapse into one state that keeps its first and last
    time, so a Home Assistant position held for several polls counts once.
    """
    states = []
    for point in points:
        if not _eligible(point):
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
    stay_seconds = _stay_seconds(rules)
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


def _returns_to_origin(window, previous, following):
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


def _delete_area_stays(store, source, since):
    if since is None:
        store.db.execute("DELETE FROM observed_stays_v2 WHERE source=? AND kind='area'", (source,))
    else:
        store.db.execute("DELETE FROM observed_stays_v2 WHERE source=? AND kind='area' AND started_at>=?", (source, since))


def _flag(value):
    return None if value is None else int(bool(value))


def _refresh_area_stays(store, source, since=None, rules=None):
    """Detect area stays in travel after `since` (all history when None).

    Travel intervals lie before the first stationary stay, between stationary
    stays, and after the last one; stationary fixes are never part of an area.
    Every stay also records went_elsewhere: whether the travel since the stay
    before it detoured somewhere off the direct path (1), went straight
    between the two (0), or is unknown because of a coverage gap (NULL).
    """
    query = "SELECT id,started_at,ended_at,lat,lon,radius_m FROM observed_stays_v2 WHERE source=? AND kind='stationary'"
    args = [source]
    if since is not None:
        query += " AND ended_at>=?"
        args.append(since)
    anchors = store._rows(query + " ORDER BY started_at,id", args)
    # The stay that ended at `since` (of either kind) precedes the recomputed travel.
    head = None
    if since is not None:
        # Quick stops are not anchors here, as in a rebuild, so live and rebuilt flags agree.
        head = next(iter(store._rows("""SELECT id,started_at,ended_at,lat,lon,radius_m FROM observed_stays_v2
            WHERE source=? AND ended_at<=? AND kind NOT IN ('stop','pause') ORDER BY ended_at DESC,id DESC LIMIT 1""", (source, since))), None)
    intervals = [(head, anchors[0] if anchors else None)] + list(zip(anchors, anchors[1:]))
    if anchors:
        intervals.append((anchors[-1], None))
    for previous, following in intervals:
        after = previous["ended_at"] if previous else since
        before = following["started_at"] if following else None
        if after and before and before <= after:
            continue
        clauses, args = ["o.source=?"], [source]
        if after:
            clauses.append("o.timestamp>?"); args.append(after)
        if before:
            clauses.append("o.timestamp<?"); args.append(before)
        points = store._rows("""SELECT * FROM (
            SELECT o.id,o.timestamp,o.lat,o.lon,o.accuracy_m,o.speed_mps,o.speed_origin,
                ROW_NUMBER() OVER (PARTITION BY o.source,o.timestamp
                    ORDER BY COALESCE(o.accuracy_m,1000000000),o.id DESC) AS observation_rank
            FROM point_observations o WHERE """ + " AND ".join(clauses) + """) ranked
            WHERE observation_rank=1 ORDER BY timestamp,id""", args)
        windows = []
        long_enough = not (after and before) or (datetime.fromisoformat(before) - datetime.fromisoformat(after)).total_seconds() >= _stay_seconds(rules)
        if long_enough:
            last_id = next((point["id"] for point in reversed(points) if _eligible(point)), None)
            for window in area_windows(points, rules):
                if not _returns_to_origin(window, previous, following):
                    window["ongoing"] = following is None and window["end_id"] == last_id
                    windows.append(window)
        sequence = ([previous] if previous else []) + windows + ([following] if following else [])
        flags = {}
        for earlier, later in zip(sequence, sequence[1:]):
            between = [point for point in points if earlier["ended_at"] < point["timestamp"] < later["started_at"]]
            flags[id(later)] = _flag(went_elsewhere_between(between, earlier, later))
        for window in windows:
            # A quick stop inside an area (a pause on a lake loop) is part of that visit.
            store.db.execute("""DELETE FROM observed_stays_v2 WHERE source=? AND kind IN ('stop','pause')
                AND started_at<=? AND ended_at>=?""", (source, window["ended_at"], window["started_at"]))
            store.db.execute("""INSERT INTO observed_stays_v2
                (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing,
                 kind,activity,distance_m,radius_m,went_elsewhere)
                VALUES (?,?,?,?,?,?,?,?,?,'area',?,?,?,?) ON CONFLICT(start_observation_id) DO NOTHING""",
                (source, window["start_id"], window["end_id"], window["started_at"], window["ended_at"],
                 window["lat"], window["lon"], window["point_count"], int(window["ongoing"]), window["activity"],
                 round(window["distance_m"], 1), round(window["radius_m"], 1), flags.get(id(window))))
        if following:
            store.db.execute("UPDATE observed_stays_v2 SET went_elsewhere=? WHERE id=?",
                             (flags.get(id(following)), following["id"]))


def check_name_reminders(store, sources, now=None):
    """Fire name-only reminders once you have been still for a few minutes near a matching place.

    The places around the current stationary candidate are looked up once and
    kept on the candidate, so later polls (and reminders created meanwhile)
    are checked without asking the provider again. Every named place within
    NAME_REMINDER_RADIUS_M counts, not just the nearest, so a busy strip mall
    cannot hide the match. Saved places count by their own radius.
    """
    reminders = store._rows("SELECT id,text,match_name FROM reminders WHERE active=1 AND match_name IS NOT NULL")
    if not reminders:
        return []
    now = now or datetime.now(timezone.utc)
    events = []
    for source in sources:
        state = store.db.execute("SELECT candidate FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        candidate = json.loads(state["candidate"]) if state and state["candidate"] else None
        if not candidate or candidate["count"] < 2 or _duration(candidate) < NAME_REMINDER_DWELL_SECONDS:
            continue
        if (now - datetime.fromisoformat(candidate["end"])).total_seconds() > 1800:
            continue  # historical data never fires reminders
        if "nearby_names" not in candidate:
            names = [place["name"] for place in store.places() + [label for label in store.place_labels() if not label["started_at"]]
                     if metres(candidate["lat"], candidate["lon"], place["lat"], place["lon"]) <= place["radius_m"]]
            try:
                names += [place["name"] for place in
                          nearby_places(store, candidate["lat"], candidate["lon"], NAME_REMINDER_RADIUS_M)
                          if (place.get("distance_m") or 0) <= NAME_REMINDER_RADIUS_M]
            except (ValueError, OSError, KeyError):
                pass  # no provider: saved places still match
            candidate["nearby_names"] = list(dict.fromkeys(name for name in names if name))
            store.db.execute("UPDATE stay_observation_state SET candidate=? WHERE source=?",
                             (json.dumps(candidate), source))
        point = store.db.execute("SELECT point_id FROM point_observations WHERE id=?", (candidate["end_id"],)).fetchone()
        for reminder in reminders:
            matched = next((name for name in candidate["nearby_names"] if name_matches(reminder["match_name"], name)), None)
            if matched is None or point is None:
                continue
            # A reminder fires once, even when two sources stop at the same place.
            claimed = store.db.execute("""UPDATE reminders SET last_triggered_at=?,cancelled_at=NULL,active=0,repeat=0
                WHERE id=? AND active=1""", (candidate["end"], reminder["id"]))
            if not claimed.rowcount:
                continue
            event_id = store.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?) RETURNING id",
                                        (reminder["id"], point["point_id"], candidate["end"])).fetchone()["id"]
            events.append({"id": event_id, "reminder_id": reminder["id"], "text": reminder["text"],
                           "place": matched, "triggered_at": candidate["end"]})
    return events


def process_stays(store, sources=None, rebuild=False):
    """Process raw observations; a late import rebuilds that source safely."""
    if rebuild:
        store.clear_analysis_cache(commit=False)
    rules = store.settings()
    if sources is None:
        sources = [r["source"] for r in store._rows("SELECT DISTINCT source FROM point_observations")]
    processed = 0
    processed_sources = set()
    rebuilt_sources = set()
    since_ids = {}
    for source in sources:
        state = store.db.execute("SELECT * FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        last_id = state["last_observation_id"] if state else 0
        since_ids[source] = 0 if rebuild else last_id
        first_new = store.db.execute("""SELECT timestamp FROM point_observations
            WHERE source=? AND id>? ORDER BY timestamp,id LIMIT 1""", (source, last_id)).fetchone()
        area_since = None
        if rebuild or (state and first_new and first_new["timestamp"] <= state["last_timestamp"]):
            rebuilt_sources.add(source)
            store.db.execute("DELETE FROM observed_stays_v2 WHERE source=?", (source,))
            store.db.execute("DELETE FROM stay_observation_state WHERE source=?", (source,))
            state = None
            last_id = 0
        elif first_new:
            # New fixes can reshape area stays after the last settled stationary
            # stay. Remove those before stationary stays grow, so the two kinds
            # never claim the same starting fix.
            area_since = store.db.execute("""SELECT MAX(ended_at) AS ended_at FROM observed_stays_v2
                WHERE source=? AND kind='stationary' AND ended_at<=?""",
                (source, first_new["timestamp"])).fetchone()["ended_at"]
            if area_since is None:
                # A source without stationary stays (a tracker that reports only
                # while moving) resumes after its last closed area stay rather
                # than rescanning its whole history on every fix.
                area_since = store.db.execute("""SELECT MAX(ended_at) AS ended_at FROM observed_stays_v2
                    WHERE source=? AND kind='area' AND ongoing=0""", (source,)).fetchone()["ended_at"]
            _delete_area_stays(store, source, area_since)
        source_max_id = store.db.execute(
            "SELECT COALESCE(MAX(id),?) AS id FROM point_observations WHERE source=?", (last_id, source)).fetchone()["id"]
        cursor = store.db.execute("""SELECT * FROM (
            SELECT o.*,p.external_id,ROW_NUMBER() OVER (PARTITION BY o.source,o.timestamp
                ORDER BY COALESCE(o.accuracy_m,1000000000),o.id DESC) AS observation_rank
            FROM point_observations o JOIN points p ON p.id=o.point_id WHERE o.source=? AND o.id>?) ranked
            WHERE observation_rank=1 ORDER BY timestamp,id""", (source, last_id))
        candidate = json.loads(state["candidate"]) if state else None
        max_id, last_time = source_max_id, state["last_timestamp"] if state else None
        while batch := cursor.fetchmany(1000):
            for point in batch:
                processed += 1
                last_time = point["timestamp"]
                # A low-accuracy fix neither confirms nor ends a stay.
                if _eligible(point):
                    candidate = _advance_stay(store, source, candidate, dict(point), rules)
        if max_id == last_id:
            continue
        processed_sources.add(source)
        store.db.execute("""INSERT INTO stay_observation_state(source,last_observation_id,last_timestamp,candidate)
            VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            last_observation_id=excluded.last_observation_id,last_timestamp=excluded.last_timestamp,candidate=excluded.candidate""",
            (source, max_id, last_time, json.dumps(candidate)))
        _refresh_area_stays(store, source, area_since, rules)
    incremental_sources = processed_sources - rebuilt_sources
    if incremental_sources:
        sync_timeline(store, incremental_sources, since_ids=since_ids)
    if rebuilt_sources:
        sync_timeline(store, rebuilt_sources, rebuild=True)
    return {"processed_points": processed}


def _gap_seconds(first, second):
    """Time from the end of one stay to the start of the next."""
    return (datetime.fromisoformat(second["started_at"]) - datetime.fromisoformat(first["ended_at"])).total_seconds()


def _count_visits(entries):
    """Visits per group from (stay_id, previous_stay_id, went_elsewhere, group) entries.

    A stay continues the visit of the stay just before it when both are in
    the same group and the travel between them went nowhere else (went_elsewhere == 0).
    """
    group_of = {stay_id: group for stay_id, _previous, _left, group in entries}
    visits = {}
    for _stay_id, previous_id, went_elsewhere, group in entries:
        if not (went_elsewhere == 0 and previous_id is not None and group_of.get(previous_id) == group):
            visits[group] = visits.get(group, 0) + 1
    return visits


def merge_split_visits(rows, place_of, moves_between=None):
    """Join chronological stays at one place when the travel between them went nowhere else.

    Walking between two buildings of a campus, moving the car to another lot,
    or an indoor GPS jump does not end a visit. `rows` must hold every stay of
    each source in order, so a stay's went_elsewhere flag refers to the stay just
    before it (rows from stays() carry previous_stay_id to check that).
    `moves_between` maps (from_stay_id, to_stay_id) to the trip
    that crossed; it stays on the visit as an internal move.
    """
    merged, last_by_source = [], {}
    for stay in rows:
        previous = last_by_source.get(stay["source"])
        place = place_of(stay)
        continues = (previous is not None and place is not None and place_of(previous) == place
                     and stay.get("went_elsewhere") == 0
                     and stay.get("previous_stay_id", previous["_last_id"]) == previous["_last_id"])
        if continues:
            move = (moves_between or {}).get((previous["_last_id"], stay["id"]))
            gap_minutes = max(0.0, _gap_seconds(previous, stay)) / 60
            previous.update(ended_at=stay["ended_at"], end_observation_id=stay["end_observation_id"],
                            ongoing=stay["ongoing"], _last_id=stay["id"],
                            point_count=previous["point_count"] + stay["point_count"],
                            observed_minutes=round(previous["observed_minutes"] + gap_minutes + stay["observed_minutes"], 1),
                            internal_moves=previous["internal_moves"] + ([move] if move else []))
            if stay["kind"] == "area":
                previous.update(kind="area", activity=previous.get("activity") or stay.get("activity"),
                                distance_m=(previous.get("distance_m") or 0) + (stay.get("distance_m") or 0),
                                radius_m=max(previous.get("radius_m") or 0, stay.get("radius_m") or 0))
            continue
        item = {**stay, "_last_id": stay["id"], "internal_moves": []}
        merged.append(item)
        last_by_source[stay["source"]] = item
    for item in merged:
        item.pop("_last_id")
    return merged


_is_home_place = is_home_place


def _confirm_possible_stops(store, rows, places, labels, lookup, rejected=None):
    """Keep a sparse 'pause' only where there is a place to have stopped at.

    A saved place, a name you gave the spot, or a named place within
    POSSIBLE_STOP_PLACE_M makes it a quick stop; otherwise it was probably the
    road or a red light, and it goes to `rejected` (when given) so the day view
    can offer to keep it. lookup=False reads only the lookup cache.
    """
    kept = []
    for row in rows:
        if row["kind"] == "pause":
            place = (label_at(labels, row["lat"], row["lon"], row["source"], row["started_at"], row["ended_at"])
                     or zone_at(places, row["lat"], row["lon"])
                     or named_place_near(store, row["lat"], row["lon"], POSSIBLE_STOP_PLACE_M, lookup))
            if not place:
                if rejected is not None:
                    rejected.append(row)
                continue
            row = {**row, "kind": "stop", "confirmed_by_place": True}
        kept.append(row)
    return kept


def stays(store, start=None, end=None, source=None, limit=500, lookup_places=False, rejected=None):
    clauses, args = [], []
    if start:
        clauses.append("ended_at>=?"); args.append(parse_time(start))
    if end:
        clauses.append("started_at<=?"); args.append(parse_time(end))
    if source:
        clauses.append("source=?"); args.append(source)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    suffix = ""
    if limit is not None:
        args.append(min(max(int(limit), 1), 5000))
        suffix = " LIMIT ?"
    # previous_stay_id lets callers tell whether went_elsewhere refers to the stay they hold.
    rows = store._rows("""SELECT * FROM (SELECT s.*,LAG(s.id) OVER (PARTITION BY s.source ORDER BY s.started_at,s.id)
        AS previous_stay_id FROM observed_stays_v2 s""" + where + ") ordered ORDER BY started_at DESC" + suffix, args)
    places, labels, overrides = store.places(), store.place_labels(), store.mode_overrides()
    rows = _confirm_possible_stops(store, rows, places, labels, lookup_places, rejected)
    rows = _apply_stay_edits(rows, store.stay_edits(start, end, source))
    if limit is not None:
        rows = rows[:min(max(int(limit), 1), 5000)]
    for row in rows:
        row["observed_minutes"] = round((datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds() / 60, 1)
        # A saved place is a zone around the stay, not its name: a university
        # holds many buildings. Only Home names what is inside it, and a name you
        # pinned to this spot wins over that too.
        label = label_at(labels, row["lat"], row["lon"], row["source"], row["started_at"], row["ended_at"])
        zone = zone_at(places, row["lat"], row["lon"])
        home = zone if zone and not label and _is_home_place(zone) else None
        row["label_id"] = label["id"] if label else None
        row["zone_id"], row["zone_name"] = (zone["id"], zone["name"]) if zone else (None, None)
        row["place_id"] = home["id"] if home else None
        row["place_name"] = label["name"] if label else home["name"] if home else None
        if row.get("kind") == "area":
            override = mode_override_for(overrides, row["source"], "stay", row["started_at"], row["ended_at"])
            if override:
                row.update(detected_activity=row.get("activity"), activity=override["mode"], activity_corrected=True)
    return rows


def _apply_stay_edits(rows, edits):
    """Detected stays (newest first) with your edits applied, newest first.

    A removed window drops the stays it mostly covers. An added stop replaces
    any detected stay it overlaps and gets a negative id (-edit id) so it never
    collides with a detected one. Quick stops inside an area stay (a pause on
    a lake loop) are part of that visit, not a stop on the way.
    """
    areas = [row for row in rows if row["kind"] == "area"]
    removed = [edit for edit in edits if edit["action"] == "remove"]
    added = [edit for edit in edits if edit["action"] == "add"]

    def covered(row, windows, share=MODE_OVERRIDE_MIN_OVERLAP):
        return any(item["source"] == row["source"] and windows_overlap(item, row["started_at"], row["ended_at"], share)
                   for item in windows)

    kept = [row for row in rows
            if not covered(row, removed)
            and not covered(row, added, share=.01)
            and not (row["kind"] == "stop" and covered(row, areas, share=.01))]
    for edit in added:
        kept.append({"id": -edit["id"], "source": edit["source"], "started_at": edit["started_at"],
                     "ended_at": edit["ended_at"], "lat": edit["lat"], "lon": edit["lon"], "kind": "manual",
                     "point_count": 0, "ongoing": 0, "activity": None, "distance_m": None, "radius_m": None,
                     "went_elsewhere": None, "previous_stay_id": None, "start_observation_id": None,
                     "end_observation_id": None, "edit_id": edit["id"]})
    kept.sort(key=lambda row: (row["started_at"], row["id"]), reverse=True)
    return kept


def anchored_trips(store, start=None, end=None, source=None):
    """Return travel intervals bounded by consecutive stays (of the length set in Settings)."""
    context_start = (datetime.fromisoformat(parse_time(start)) - timedelta(days=1)).isoformat() if start else None
    context_end = (datetime.fromisoformat(parse_time(end)) + timedelta(days=1)).isoformat() if end else None
    anchors = stays(store, context_start, context_end, source=source, limit=None, lookup_places=True)
    points = list(reversed(store.observations(context_start, context_end, source=source, limit=None)))
    trips = trips_between_stays(points, anchors, start, end, store.settings())
    overrides = store.mode_overrides()
    for trip in trips:
        override = mode_override_for(overrides, trip["source"], "trip", trip["start"], trip["end"])
        if override:
            # Your correction beats the speed-based guess (kept as detected_mode).
            trip.update(detected_mode=trip["mode"], mode=override["mode"], mode_corrected=True,
                        modes={override["mode"]: sum(trip["modes"].values())})
    return trips


def _bucket(lat, lon):
    """75 m ECEF grid: nearby places only need 27 neighboring cells checked."""
    a, b = math.radians(lat), math.radians(lon)
    r = 6371000 / 75
    return (math.floor(r * math.cos(a) * math.cos(b)), math.floor(r * math.cos(a) * math.sin(b)), math.floor(r * math.sin(a)))


def place_patterns(store, limit=50, rows=None):
    rows = rows if rows is not None else stays(store, limit=None)
    places = {p["id"]: p for p in store.places()}
    labels = {label["id"]: label for label in store.place_labels()}
    clusters, bucket_index, mapping = [], {}, {}
    wide_clusters = []
    for stay in rows:
        key = _bucket(stay["lat"], stay["lon"])
        radius = stay.get("radius_m") or 0
        target = None
        if stay.get("label_id") is not None:
            target = next((i for i, c in enumerate(clusters) if c.get("label_id") == stay["label_id"]), None)
        elif stay["place_id"] is not None:
            target = next((i for i, c in enumerate(clusters) if c["place_id"] == stay["place_id"]), None)
        else:
            candidates = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        candidates.update(bucket_index.get((key[0]+dx, key[1]+dy, key[2]+dz), ()))
            target = next((i for i in candidates if clusters[i]["place_id"] is None and clusters[i].get("label_id") is None
                           and metres(stay["lat"], stay["lon"], clusters[i]["lat"], clusters[i]["lon"]) <= 75), None)
            if target is None and radius > 75:
                # Laps of one lake or park do not share a centre, so wide areas
                # join when their centres fall inside each other's extent.
                target = next((i for i in wide_clusters if clusters[i]["place_id"] is None and clusters[i].get("label_id") is None
                               and metres(stay["lat"], stay["lon"], clusters[i]["lat"], clusters[i]["lon"])
                               <= min(max(radius, clusters[i]["radius_m"]), 500)), None)
        if target is None:
            place = labels.get(stay.get("label_id")) or places.get(stay["place_id"])
            target = len(clusters)
            clusters.append({"cluster_id": target, "place_id": stay["place_id"], "label_id": stay.get("label_id"),
                             "name": place["name"] if place else None,
                             "lat": stay["lat"], "lon": stay["lon"], "visits": 0, "observed_minutes": 0,
                             "last_seen": stay["ended_at"], "radius_m": 0, "activities": {}, "_spans": [], "_members": []})
            bucket_index.setdefault(key, set()).add(target)
        cluster = clusters[target]
        if radius > 75 and cluster["radius_m"] <= 75 and target not in wide_clusters:
            wide_clusters.append(target)
        cluster["radius_m"] = max(cluster["radius_m"], round(radius))
        if stay.get("activity"):
            cluster["activities"][stay["activity"]] = cluster["activities"].get(stay["activity"], 0) + 1
        cluster["_spans"].append((stay["id"], stay.get("previous_stay_id"), stay.get("went_elsewhere")))
        cluster["_members"].append((stay["lat"], stay["lon"], radius, max(stay.get("point_count") or 1, 1)))
        cluster["observed_minutes"] += stay["observed_minutes"]
        if stay["ended_at"] > cluster["last_seen"]:
            cluster["last_seen"] = stay["ended_at"]
        mapping[stay["id"]] = target
    # Spans stay attached so resolve_place_patterns can recount after clusters merge.
    counts = _count_visits([(*span, index) for index, cluster in enumerate(clusters) for span in cluster["_spans"]])
    for index, cluster in enumerate(clusters):
        # Matching used each cluster's first stay; the place itself sits at the
        # sample-weighted average of its stays (each already the average of its
        # fixes), and extent_m covers their spread for the place-name search.
        members = cluster.pop("_members")
        weight = sum(member[3] for member in members)
        cluster["lat"] = sum(lat * count for lat, _lon, _radius, count in members) / weight
        cluster["lon"] = sum(lon * count for _lat, lon, _radius, count in members) / weight
        cluster["extent_m"] = round(max(metres(cluster["lat"], cluster["lon"], lat, lon) + radius
                                        for lat, lon, radius, _count in members))
        cluster["visits"] = counts.get(index, 0)
        cluster["observed_minutes"] = round(cluster["observed_minutes"], 1)
        cluster["display_name"] = cluster["name"] or f"{cluster['lat']:.5f}, {cluster['lon']:.5f}"
        zone = places.get(cluster["place_id"]) or zone_at(places.values(), cluster["lat"], cluster["lon"])
        cluster["zone_id"], cluster["zone"] = (zone["id"], zone["name"]) if zone else (None, None)
    ordered = sorted(clusters, key=lambda c: (c["observed_minutes"], c["visits"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)], mapping


def _normalized_place_name(value):
    """Normalize provider labels for matching without changing their display text."""
    normalized = " ".join(str(value or "").casefold().split())
    return "" if normalized in ("unknown", "unknown place") else normalized


def resolve_place_patterns(store, patterns):
    """Resolve coordinate clusters and merge clusters that describe one real place.

    Provider IDs are authoritative. Equal normalized names are also merged when
    their coordinates are within 2.5 km, which joins large parks and lakes while
    keeping distant branches of a chain separate. A place with no name, only a
    street address, joins Home or the nearest name correction on the same
    street within ADDRESS_MERGE_M (indoor GPS drifted out of Home's circle),
    else other address-only places there. Inside another saved place (a zone)
    a place is named after the building it is in, else after the zone.
    """
    clusters, stay_mapping = patterns
    saved = {place["id"]: place for place in store.places(enrich=True)}
    labels = {label["id"]: label for label in store.place_labels()}
    resolved = []
    for cluster in clusters:
        place = saved.get(cluster["place_id"])
        label = labels.get(cluster.get("label_id"))
        found = (_label_place(label) if label else place if place
                 else place_in_zone(store, cluster["lat"], cluster["lon"], saved.get(cluster.get("zone_id")),
                                    cluster.get("extent_m"), lookup=place_for_coordinate))
        name = cluster.get("name") or found.get("name") or found.get("address") or ""
        categories = found.get("categories") if isinstance(found.get("categories"), list) else []
        category = found.get("category") or (categories[0] if categories else "")
        resolved.append({**cluster, "name": name, "display_name": name or cluster["display_name"],
                         "address": found.get("address") or "", "category": category or "",
                         "categories": categories, "provider_place_id": found.get("provider_place_id") or "",
                         "saved": bool(place), "label_id": cluster.get("label_id") or found.get("label_id"),
                         "_street": street_key(found.get("address")),
                         "_anchor": bool(place or label), "_address_only": not (place or label or found.get("named", True))})

    parent = list(range(len(resolved)))
    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(a, b):
        a, b = root(a), root(b)
        if a != b:
            parent[b] = a

    saved_ids, provider_ids, names, streets = {}, {}, {}, {}
    for index, place in enumerate(resolved):
        if place.get("place_id") is not None:
            key = place["place_id"]
            if key in saved_ids:
                union(index, saved_ids[key])
            else:
                saved_ids[key] = index
        if place.get("provider_place_id"):
            key = place["provider_place_id"]
            if key in provider_ids:
                union(index, provider_ids[key])
            else:
                provider_ids[key] = index
        name = _normalized_place_name(place.get("name"))
        if name:
            for other in names.get(name, []):
                if metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]) <= 2500:
                    union(index, other)
            names.setdefault(name, []).append(index)
    # Address-only places: join the nearest saved place or correction on the
    # same street, else other address-only places there (never bridging two
    # saved places through an address).
    anchors = {}
    for index, place in enumerate(resolved):
        if place["_anchor"] and place["_street"]:
            anchors.setdefault(place["_street"], []).append(index)
    for index, place in enumerate(resolved):
        if not (place["_address_only"] and place["_street"]):
            continue
        nearby = [(metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]), other)
                  for other in anchors.get(place["_street"], [])]
        nearby = [item for item in nearby if item[0] <= ADDRESS_MERGE_M]
        if nearby:
            union(min(nearby)[1], index)
            continue
        for other in streets.get(place["_street"], []):
            if metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]) <= ADDRESS_MERGE_M:
                union(index, other)
        streets.setdefault(place["_street"], []).append(index)

    grouped, raw_to_merged = [], {}
    by_root = {}
    for index, place in enumerate(resolved):
        group_root = root(index)
        target = by_root.get(group_root)
        weight = max(float(place.get("observed_minutes") or 0), 1.0)
        if target is None:
            target = len(grouped)
            by_root[group_root] = target
            grouped.append({**place, "cluster_id": target, "_weight": weight})
        else:
            item = grouped[target]
            total_weight = item["_weight"] + weight
            item["lat"] = (item["lat"] * item["_weight"] + place["lat"] * weight) / total_weight
            item["lon"] = (item["lon"] * item["_weight"] + place["lon"] * weight) / total_weight
            item["_weight"] = total_weight
            item["visits"] += place["visits"]
            item["observed_minutes"] = round(item["observed_minutes"] + place["observed_minutes"], 1)
            item["last_seen"] = max(item.get("last_seen") or "", place.get("last_seen") or "") or None
            if "first_seen" in item or "first_seen" in place:
                values = [value for value in (item.get("first_seen"), place.get("first_seen")) if value]
                item["first_seen"] = min(values) if values else None
            if place.get("saved") and not item.get("saved"):
                for field in ("place_id", "name", "display_name", "address", "category", "provider_place_id"):
                    item[field] = place.get(field) or item.get(field)
            else:
                for field in ("address", "category", "provider_place_id"):
                    item[field] = item.get(field) or place.get(field) or ""
            item["saved"] = item.get("saved") or place.get("saved")
            item["label_id"] = item.get("label_id") or place.get("label_id")
            if not item.get("zone_id"):
                item["zone_id"], item["zone"] = place.get("zone_id"), place.get("zone")
            item["categories"] = list(dict.fromkeys((item.get("categories") or []) + (place.get("categories") or [])))
            item["radius_m"] = max(item.get("radius_m") or 0, place.get("radius_m") or 0)
            activities = dict(item.get("activities") or {})
            for activity, count in (place.get("activities") or {}).items():
                activities[activity] = activities.get(activity, 0) + count
            item["activities"] = activities
            if "_spans" in item or "_spans" in place:
                item["_spans"] = (item.get("_spans") or []) + (place.get("_spans") or [])
        raw_to_merged[place["cluster_id"]] = target
    counts = _count_visits([(*span, index) for index, place in enumerate(grouped) for span in place.get("_spans") or []])
    for index, place in enumerate(grouped):
        for key in ("_weight", "_street", "_anchor", "_address_only"):
            place.pop(key, None)
        if place.pop("_spans", None) is not None:
            place["visits"] = counts.get(index, 0)
    mapping = {stay_id: raw_to_merged[cluster_id] for stay_id, cluster_id in stay_mapping.items()}
    grouped.sort(key=lambda item: (item["observed_minutes"], item["visits"]), reverse=True)
    # Sorting changes public cluster IDs, so remap stays and IDs together.
    old_to_new = {item["cluster_id"]: index for index, item in enumerate(grouped)}
    for index, item in enumerate(grouped):
        item["cluster_id"] = index
    mapping = {stay_id: old_to_new[cluster_id] for stay_id, cluster_id in mapping.items()}
    return grouped, mapping


def visited_places(store, limit=500):
    """Observed places ordered by total sampled time, with cache-first labels."""
    return place_overview(store, limit)["visited"]


def place_overview(store, limit=100):
    """Visited places (without Home) and visit totals for every saved place (with Home), from one pass.

    A saved place is a zone, so its totals cover every place inside it: one
    visit to campus is one visit however many buildings it took in.
    """
    rows = stays(store, limit=None)
    clusters, mapping = resolve_place_patterns(store, place_patterns(store, limit=None, rows=rows))
    by_cluster = {item["cluster_id"]: item for item in clusters}
    # Home also owns indoor GPS drift that its place absorbed from down the street.
    zone_of = {stay["id"]: by_cluster[mapping[stay["id"]]].get("place_id") or stay.get("zone_id") for stay in rows}
    inside = [stay for stay in rows if zone_of[stay["id"]] is not None]
    visits = _count_visits([(stay["id"], stay.get("previous_stay_id"), stay.get("went_elsewhere"), zone_of[stay["id"]])
                            for stay in inside])
    saved = {}
    for stay in inside:
        zone_id = zone_of[stay["id"]]
        item = saved.setdefault(str(zone_id), {"visits": visits.get(zone_id, 0), "observed_minutes": 0, "last_seen": None})
        item["observed_minutes"] = round(item["observed_minutes"] + stay["observed_minutes"], 1)
        item["last_seen"] = max(item["last_seen"] or "", stay["ended_at"])
    return {"visited": [item for item in clusters if not _is_home_place(item)][:min(max(int(limit), 1), 5000)],
            "saved": saved}


def _search_text(value):
    return search_text(value)


def search_visits(store, query, start=None, end=None, limit=50, timezone_name=None):
    """Find when you were at a place by name, address, or category.

    Matching is exact on words, not semantic: "costco" never returns Sam's
    Club. An LLM caller supplies synonyms or categories when a query misses.
    """
    terms = _search_text(query).split()
    if not terms or len(query) > 200:
        raise ValueError("query is required (max 200 characters)")
    timezone_name = timezone_name or store.settings()["timezone"]
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown timezone") from exc
    rows = stays(store, start, end, limit=None)
    clusters, mapping = resolve_place_patterns(store, place_patterns(store, limit=None, rows=rows))
    matched = {}
    for cluster in clusters:
        words = " ".join(_search_text(value) for value in (
            cluster.get("name"), cluster.get("display_name"), cluster.get("address"), cluster.get("zone"),
            cluster.get("category"), *(cluster.get("categories") or []))).split()
        # Word prefixes: "gas" finds "Gasoline", "lake" does not find "Blake Street".
        if all(any(word.startswith(term) for word in words) for term in terms):
            matched[cluster["cluster_id"]] = cluster
    visits = []
    for stay in merge_split_visits(list(reversed(rows)), lambda stay: mapping.get(stay["id"])):
        place = matched.get(mapping.get(stay["id"]))
        if place is None:
            continue
        arrival = datetime.fromisoformat(stay["started_at"]).astimezone(zone)
        departure = datetime.fromisoformat(stay["ended_at"]).astimezone(zone)
        visits.append({"date": arrival.date().isoformat(), "weekday": arrival.strftime("%A"),
                       "arrival": arrival.isoformat(timespec="minutes"),
                       "departure": departure.isoformat(timespec="minutes"),
                       "observed_minutes": stay["observed_minutes"], "ongoing": bool(stay["ongoing"]),
                       "place": place["name"] or place["display_name"], "address": place.get("address") or "",
                       "zone": place.get("zone"), "kind": stay["kind"], "activity": stay.get("activity"),
                       "lat": stay["lat"], "lon": stay["lon"], "source": stay["source"],
                       "stay_id": stay["id"], "started_at": stay["started_at"], "ended_at": stay["ended_at"]})
    visits.sort(key=lambda visit: visit["arrival"], reverse=True)
    places = [{"name": place["name"] or place["display_name"], "address": place.get("address") or "",
               "zone": place.get("zone"), "category": place.get("category") or "", "visits": place["visits"],
               "observed_minutes": place["observed_minutes"], "last_seen": place.get("last_seen"),
               "saved": bool(place.get("saved"))}
              for place in sorted(matched.values(), key=lambda item: item.get("last_seen") or "", reverse=True)]
    return {"query": query, "timezone": timezone_name, "total_visits": len(visits), "places": places,
            "visits": visits[:min(max(int(limit), 1), 1000)],
            "note": f"Visits are observed stays of at least {store.settings()['stay_min_minutes']} minutes; sparse GPS can miss short stops"}


def transitions(store, limit=50, patterns=None, rows=None):
    rows = rows if rows is not None else stays(store, limit=None)
    clusters, mapping = patterns if patterns is not None else resolve_place_patterns(
        store, place_patterns(store, limit=None, rows=rows))
    cluster_by_id = {c["cluster_id"]: c for c in clusters}
    rows = list(reversed(rows))
    edges = {}
    by_source = {}
    now = datetime.now(timezone.utc)
    for stay in rows:
        previous = by_source.get(stay["source"])
        target = mapping.get(stay["id"])
        if previous and target is not None:
            origin = mapping.get(previous["id"])
            gap = (datetime.fromisoformat(stay["started_at"]) - datetime.fromisoformat(previous["ended_at"])).total_seconds()
            if origin is not None and origin != target and 0 <= gap <= 86400:
                key = (origin, target)
                edge = edges.setdefault(key, {"from_cluster_id": origin, "to_cluster_id": target, "count": 0, "weighted_score": 0.0, "last_seen": stay["started_at"]})
                edge["count"] += 1
                age_days = max(0.0, (now - datetime.fromisoformat(stay["started_at"])).total_seconds() / 86400)
                edge["weighted_score"] += 0.5 ** (age_days / 90)
                edge["last_seen"] = max(edge["last_seen"], stay["started_at"])
        by_source[stay["source"]] = stay
    result = list(edges.values())
    for edge in result:
        a, b = cluster_by_id[edge["from_cluster_id"]], cluster_by_id[edge["to_cluster_id"]]
        edge["from_name"] = a["display_name"]
        edge["to_name"] = b["display_name"]
        edge["to_lat"], edge["to_lon"] = b["lat"], b["lon"]
        edge["weighted_score"] = round(edge["weighted_score"], 4)
    ordered = sorted(result, key=lambda e: (e["weighted_score"], e["count"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)]


def predict_next(store, patterns=None, edges=None):
    current = store.latest()
    if not current:
        return {"prediction": None, "reason": "no location data"}
    clusters, _ = patterns if patterns is not None else resolve_place_patterns(store, place_patterns(store, limit=None))
    origin = next((c for c in clusters if metres(current["lat"], current["lon"], c["lat"], c["lon"])
                   <= max(75, c.get("radius_m") or 0)), None)
    if not origin:
        return {"prediction": None, "reason": "current point is not at an observed stay"}
    edges = [e for e in (edges if edges is not None else transitions(store, limit=None, patterns=patterns)) if e["from_cluster_id"] == origin["cluster_id"]]
    total = sum(e["count"] for e in edges)
    if total < 3:
        return {"prediction": None, "reason": "fewer than three observed departures", "observations": total}
    top = max(edges, key=lambda e: e["weighted_score"])
    newest = max(e["last_seen"] for e in edges)
    if (datetime.now(timezone.utc) - datetime.fromisoformat(newest)).total_seconds() > 180 * 86400:
        return {"prediction": None, "reason": "transition evidence is older than 180 days", "observations": total}
    total_score = sum(e["weighted_score"] for e in edges)
    return {"from": origin["display_name"],
            "prediction": top["to_name"], "lat": top["to_lat"], "lon": top["to_lon"],
            "observations": total, "matching_transitions": top["count"], "frequency": round(top["weighted_score"] / total_score, 2) if total_score else 0,
            "current_fix_stale": current["stale"],
            "note": "Recent transitions receive more weight; this is not a guaranteed destination"}


def insights(store, limit=50):
    rows = stays(store, limit=None)
    patterns = resolve_place_patterns(store, place_patterns(store, limit=None, rows=rows))
    edges = transitions(store, limit=None, patterns=patterns, rows=rows)
    unlabeled = [p for p in patterns[0] if p["place_id"] is None and not p.get("label_id") and p.get("zone_id") is None][:limit]
    return {"frequent_places": patterns[0][:limit],
            "unlabeled_places": unlabeled,
            "transitions": edges[:limit],
            "prediction": predict_next(store, patterns=patterns, edges=edges),
            "observed_stays": len(rows)}


def _fuse_sources(day_stays, trips, samples_by_source, priority=()):
    """Tell one story when several sources recorded the same day.

    One device can sit at the university while another drives. The source you
    ranked highest in Settings (else the one with the most samples) is taken
    whole; each next source fills only the times those before it have nothing
    to say, so the timeline never places you in two spots.
    """
    rank = {name: index for index, name in enumerate(priority)}
    sources = sorted({stay["source"] for stay in day_stays} | {trip["source"] for trip in trips},
                     key=lambda name: (rank.get(name, len(rank)), -samples_by_source.get(name, 0), name))
    if len(sources) < 2:
        return day_stays, trips, sources
    taken, kept_stays, kept_trips, used = [], [], [], []
    for name in sources:
        items = ([("stay", stay, stay["started_at"], stay["ended_at"]) for stay in day_stays if stay["source"] == name]
                 + [("trip", trip, trip["start"], trip["end"]) for trip in trips if trip["source"] == name])
        accepted = []
        for kind, item, begin, finish in items:
            begin, finish = datetime.fromisoformat(begin), datetime.fromisoformat(finish)
            if not any(begin < taken_end and taken_begin < finish for taken_begin, taken_end in taken):
                accepted.append((begin, finish))
                (kept_stays if kind == "stay" else kept_trips).append(item)
        taken.extend(accepted)
        if accepted:
            used.append(name)
    kept_stays.sort(key=lambda stay: (stay["started_at"], stay["id"]))
    kept_trips.sort(key=lambda trip: trip["start"], reverse=True)
    return kept_stays, kept_trips, used


def _join_source_handoffs(day_stays, trips):
    """One visit when the device recording it changed partway through.

    Switching trackers (Home Assistant's app to OwnTracks, say) ends one
    source's stay and starts another's at the same place minutes later. Stays
    from different sources at one place, no more than MAX_SAMPLE_GAP_SECONDS
    apart with no trip between them, are one visit. `day_stays` is chronological.
    """
    joined = []
    for stay in day_stays:
        previous = joined[-1] if joined else None
        if (previous is not None and previous["source"] != stay["source"]
                and previous.get("cluster_id") is not None and previous.get("cluster_id") == stay.get("cluster_id")):
            gap = (datetime.fromisoformat(stay["started_at"]) - datetime.fromisoformat(previous["ended_at"])).total_seconds()
            travelled = any(previous["ended_at"] <= trip["start"] and trip["end"] <= stay["started_at"] for trip in trips)
            if 0 <= gap <= MAX_SAMPLE_GAP_SECONDS and not travelled:
                previous.update(ended_at=stay["ended_at"], ongoing=stay["ongoing"],
                                point_count=previous["point_count"] + stay["point_count"],
                                observed_minutes=round(previous["observed_minutes"] + gap / 60 + stay["observed_minutes"], 1),
                                internal_moves=previous.get("internal_moves", []) + stay.get("internal_moves", []),
                                sources=sorted(set(previous.get("sources") or [previous["source"]]) | {stay["source"]}))
                continue
        joined.append(stay)
    return joined


def _coverage_gaps(points, trips, day_stays=()):
    """Unobserved stretches over 30 minutes, except inside a trip that already notes
    them or a stay (an app that reports only while moving is quiet while you stay)."""
    times = sorted({datetime.fromisoformat(point["timestamp"]) for point in points})
    spans = [(datetime.fromisoformat(trip["start"]), datetime.fromisoformat(trip["end"])) for trip in trips]
    spans += [(datetime.fromisoformat(stay["started_at"]), datetime.fromisoformat(stay["ended_at"])) for stay in day_stays]
    gaps = []
    for earlier, later in zip(times, times[1:]):
        if (later - earlier).total_seconds() <= MAX_SAMPLE_GAP_SECONDS:
            continue
        # Covered end to end by stays and trips together (a stay that ran until you left, then the trip).
        reached = earlier
        for start, end in sorted(spans):
            if start <= reached < end:
                reached = end
        if reached >= later:
            continue
        gaps.append({"start": earlier.isoformat(), "end": later.isoformat(),
                     "minutes": round((later - earlier).total_seconds() / 60, 1)})
    return gaps


def day_summary(store, date, timezone_name="UTC", source=None):
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    day_start = day.astimezone(timezone.utc)
    day_end = (day + timedelta(days=1)).astimezone(timezone.utc)
    start = day_start.isoformat()
    end = (day_end - timedelta(microseconds=1)).isoformat()
    # Switching the places provider in Settings re-resolves every day's names.
    provider = places_provider_identity(store)
    cache_identity = json.dumps(["day-v16", date, timezone_name, source, provider, store.settings()["source_priority"]],
                                separators=(",", ":"))
    cache_key = "day:" + hashlib.sha256(cache_identity.encode()).hexdigest()
    watermark = store.analysis_watermark(start, end, source)
    cached = store.cached_analysis(cache_key, watermark)
    if cached is not None:
        return cached
    points = store.observations(start, end, source=source, limit=None)
    samples_by_source = {}
    for point in points:
        samples_by_source[point["source"]] = samples_by_source.get(point["source"], 0) + 1
    unconfirmed = []
    day_stays, trips, timeline_sources = _fuse_sources(
        list(reversed(stays(store, start, end, source=source, limit=None, lookup_places=True, rejected=unconfirmed))),
        anchored_trips(store, start, end, source), samples_by_source, store.settings()["source_priority"])
    # Stops the detector found but no place confirmed, for you to keep or ignore;
    # none you already added or removed by hand.
    edits = store.stay_edits(start, end, source)
    shown_sources = set(timeline_sources) or set(samples_by_source)
    possible_stops = [{"source": row["source"], "started_at": row["started_at"], "ended_at": row["ended_at"],
                       "lat": row["lat"], "lon": row["lon"], "point_count": row["point_count"],
                       "minutes": round((datetime.fromisoformat(row["ended_at"])
                                         - datetime.fromisoformat(row["started_at"])).total_seconds() / 60, 1)}
                      for row in reversed(unconfirmed)
                      if row["source"] in shown_sources
                      and not any(edit["source"] == row["source"] and windows_overlap(edit, row["started_at"], row["ended_at"], .01)
                                  for edit in edits)]
    for stay in day_stays:
        first = max(datetime.fromisoformat(stay["started_at"]), day_start)
        last = min(datetime.fromisoformat(stay["ended_at"]), day_end)
        stay["observed_minutes"] = round(max(0, (last - first).total_seconds()) / 60, 1)
    raw_patterns = place_patterns(store, limit=None, rows=day_stays)
    raw_by_cluster = {cluster["cluster_id"]: cluster for cluster in raw_patterns[0]}
    for cluster in raw_patterns[0]:
        cluster["first_seen"] = None
        cluster["last_seen"] = None
    for stay in day_stays:
        cluster = raw_by_cluster[raw_patterns[1][stay["id"]]]
        first = max(datetime.fromisoformat(stay["started_at"]), day_start).isoformat()
        last = min(datetime.fromisoformat(stay["ended_at"]), day_end).isoformat()
        cluster["first_seen"] = min(cluster["first_seen"] or first, first)
        cluster["last_seen"] = max(cluster["last_seen"] or last, last)
    clusters, mapping = resolve_place_patterns(store, raw_patterns)
    by_cluster = {cluster["cluster_id"]: cluster for cluster in clusters}
    places_including_home = sorted(clusters, key=lambda item: item["first_seen"])
    visited_places = [place for place in places_including_home if not _is_home_place(place)]
    for stay in day_stays:
        place = by_cluster[mapping[stay["id"]]]
        stay["cluster_id"] = place["cluster_id"]
        stay["display_name"] = place["name"] or place["display_name"]
        stay["category"] = place.get("category") or ""
        stay["provider_place_id"] = place.get("provider_place_id") or ""
        stay["zone"] = place.get("zone")
    chronological = list(reversed(store.timeline(start, end, source=source, limit=None)))
    stay_labels = {stay["id"]: by_cluster[mapping[stay["id"]]]["name"] or by_cluster[mapping[stay["id"]]]["display_name"]
                   for stay in day_stays}
    # Speed cannot tell slow driving from cycling. A lone "ride" between two
    # drives (moving the car to another lot) is the car you arrived in.
    by_source = {}
    for trip in sorted(trips, key=lambda item: item["start"]):
        by_source.setdefault(trip["source"], []).append(trip)
    for sequence in by_source.values():
        for before, trip, after in zip(sequence, sequence[1:], sequence[2:]):
            if trip["mode"] == "cycling" and not trip.get("mode_corrected") and before["mode"] == after["mode"] == "driving":
                trip.update(mode="driving", modes={"driving": sum(trip["modes"].values())}, mode_from_context=True)
    kept_trips, internal_moves, saved_places = [], {}, store.places()
    for trip in trips:
        trip["from_name"] = stay_labels.get(trip["from_stay_id"], trip.get("from_name"))
        trip["to_name"] = stay_labels.get(trip["to_stay_id"], trip.get("to_name"))
        origin, destination = mapping.get(trip["from_stay_id"]), mapping.get(trip["to_stay_id"])
        if origin is not None and origin == destination:
            # Same place at both ends. Moving between two buildings of one
            # campus goes nowhere else: that is part of the visit, not a trip
            # "to" the place you were already at. A real round trip (lunch
            # off campus, a run from home) stays; drift is dropped.
            if trip.get("went_elsewhere") is False:
                move = {key: trip.get(key) for key in ("source", "start", "end", "mode", "distance_km", "duration_minutes",
                                                       "route_segments", "mode_corrected")}
                # Crossing a campus from sparse fixes looks fast: the phone held
                # its last position, then jumped. Under 1 km inside one place is
                # walking between buildings, not a ride or the car you arrived
                # in, unless the device itself reported the speed.
                if (move["distance_km"] < 1 and not trip.get("speed_reported") and not trip.get("mode_corrected")
                        and (move["mode"] in ("running", "cycling") or trip.get("mode_from_context"))):
                    move["mode"] = "walking"
                internal_moves[(trip["from_stay_id"], trip["to_stay_id"])] = move
                continue
            if not is_excursion(trip):
                continue
            trip["loop"] = True
        if trip.get("loop"):
            # Say where a round trip went, so Home -> Home reads as an errand, not a glitch.
            turnaround = trip.get("turnaround")
            if turnaround and turnaround["stop_minutes"]:
                turnaround["name"] = place_in_zone(store, turnaround["lat"], turnaround["lon"],
                                                   zone_at(saved_places, turnaround["lat"], turnaround["lon"]),
                                                   lookup=place_for_coordinate).get("name") or None
        kept_trips.append(trip)
    trips = kept_trips
    day_stays = merge_split_visits(day_stays, lambda stay: stay.get("cluster_id"), internal_moves)
    day_stays = _join_source_handoffs(day_stays, trips)
    for stay in day_stays:
        if stay["kind"] == "area":
            path = [point for point in chronological if point["source"] == stay["source"]
                    and stay["started_at"] <= point["timestamp"] <= stay["ended_at"]]
            stay["route_segments"] = [smooth_route_segment(segment) for segment in route_segments_from_points(path)]
    route_segments = [smooth_route_segment(segment) for segment in route_segments_from_points(chronological)]
    activity_totals = {mode: {"count": 0, "distance_km": 0.0, "minutes": 0.0} for mode in MOVEMENT_MODES}
    for trip in trips:
        totals = activity_totals[trip["mode"]]
        totals["count"] += 1
        totals["distance_km"] += trip["distance_km"]
        totals["minutes"] += trip["duration_minutes"]
    for stay in day_stays:
        if stay.get("activity") in activity_totals:
            totals = activity_totals[stay["activity"]]
            totals["count"] += 1
            totals["distance_km"] += (stay.get("distance_m") or 0) / 1000
            totals["minutes"] += stay["observed_minutes"]
        for move in stay["internal_moves"]:
            totals = activity_totals[move["mode"]]
            totals["count"] += 1
            totals["distance_km"] += move["distance_km"]
            totals["minutes"] += move["duration_minutes"]
    for totals in activity_totals.values():
        totals["distance_km"] = round(totals["distance_km"], 2)
        totals["minutes"] = round(totals["minutes"], 1)
    activity_counts = {mode: totals["count"] for mode, totals in activity_totals.items()}
    stats = stats_from_points(chronological)
    stats["points"] = len(points)
    stats["unique_points"] = len(chronological)
    stats["timeline_points"] = len(chronological)
    result = {"date": date, "timezone": timezone_name, "source": source, "start": start, "end": end,
            "points": len(points), "stays": day_stays,
            "unique_points": len(chronological), "timeline_points": len(chronological), "stats": stats, "trips": trips, "route_segments": route_segments,
            "activity_counts": activity_counts, "activity_totals": activity_totals,
            "movement_minutes": round(sum(totals["minutes"] for totals in activity_totals.values()), 1),
            "movement_km": round(sum(totals["distance_km"] for totals in activity_totals.values()), 2),
            "coverage_gaps": _coverage_gaps(points, trips, day_stays), "timeline_sources": timeline_sources,
            "samples_by_source": samples_by_source,
            "stay_edits": edits, "possible_stops": possible_stops,
            "visited_places": visited_places, "places_including_home": places_including_home,
            "first_fix": points[-1]["timestamp"] if points else None,
            "last_fix": points[0]["timestamp"] if points else None,
            "first_location": {"lat": points[-1]["lat"], "lon": points[-1]["lon"]} if points else None,
            "last_location": {"lat": points[0]["lat"], "lon": points[0]["lon"]} if points else None,
            "coverage_note": "Stays are observed from samples; time in unobserved gaps is unknown"}
    store.save_analysis(cache_key, watermark, result)
    return result


def _home_return_context(day_start, day_end):
    """The neighboring samples that can establish an early-morning Home return."""
    return (day_start - timedelta(days=1),
            day_end + timedelta(hours=HOME_RETURN_GRACE_HOURS) - timedelta(microseconds=1))


def _home_return(store, day_start, day_end, source=None):
    """An observed Home return just after midnight, for either adjoining day."""
    homes = {place["id"]: place["name"] for place in store.places() if _is_home_place(place)}
    if not homes:
        return None
    context_start, context_end = _home_return_context(day_start, day_end)
    rows = sorted(stays(store, context_start.isoformat(), context_end.isoformat(), source=source, limit=None),
                  key=lambda stay: (stay["started_at"], stay["id"]))
    previous_by_source, returns = {}, []
    for stay in rows:
        previous = previous_by_source.get(stay["source"])
        home_name = homes.get(stay.get("place_id"))
        is_home = home_name is not None
        if is_home and previous is not None and previous.get("place_id") not in homes:
            returned_at = datetime.fromisoformat(stay["started_at"])
            previous_started = datetime.fromisoformat(previous["started_at"])
            next_morning = day_end <= returned_at < day_end + timedelta(hours=HOME_RETURN_GRACE_HOURS)
            after_midnight = day_start <= returned_at < day_start + timedelta(hours=HOME_RETURN_GRACE_HOURS)
            if next_morning and previous_started < day_end:
                returns.append({"returned_at": returned_at.isoformat(), "home_name": home_name, "timing": "next_day"})
            elif after_midnight and previous_started < day_start:
                returns.append({"returned_at": returned_at.isoformat(), "home_name": home_name, "timing": "after_midnight"})
        previous_by_source[stay["source"]] = stay
    return min(returns, key=lambda item: item["returned_at"], default=None)


def _home_return_text(home_return, zone):
    returned = datetime.fromisoformat(home_return["returned_at"]).astimezone(zone).strftime("%H:%M")
    target = "home" if str(home_return["home_name"]).casefold() in ("home", "my home") else home_return["home_name"]
    action = f"Returned {target}" if target == "home" else f"Returned to {target}"
    suffix = "the next day" if home_return["timing"] == "next_day" else "after midnight"
    return f"{action} at {returned} {suffix}."


def daily_overview(store, date, timezone_name=None, source=None):
    """Materialize a compact, structured record intended for API and LLM use.

    The catalog holds one record per date: the configured timezone across all
    sources. Other timezones or a single source are computed on request but not
    stored, so an agent asking in another zone cannot add a second record.
    """
    configured = store.settings()["timezone"]
    timezone_name = timezone_name or configured
    canonical = timezone_name == configured and not source
    try:
        zone = ZoneInfo(timezone_name)
        local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    day_start = local_start.astimezone(timezone.utc)
    day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    start = day_start.isoformat()
    end = (day_end - timedelta(microseconds=1)).isoformat()
    home_context_start, home_context_end = _home_return_context(day_start, day_end)
    watermark = store.daily_overview_watermark(start, end, source, home_context_start.isoformat(), home_context_end.isoformat())
    cached = store.stored_daily_overview(date, timezone_name, source, watermark) if canonical else None
    if cached is not None:
        return cached
    day = day_summary(store, date, timezone_name, source)
    reminder_events = store.reminder_events(start, end, source)
    reminders_created = store.reminders_created(start, end)
    reminders_cancelled = store.reminders_cancelled(start, end)
    reminders_active_at_end = store.reminders_active_at(end)
    places = [{"name": place.get("name") or place.get("display_name"),
               "address": place.get("address") or "", "home": _is_home_place(place),
               "zone": place.get("zone"), "category": place.get("category") or "",
               "provider_place_id": place.get("provider_place_id") or "",
               "lat": place["lat"], "lon": place["lon"],
               "visits": place["visits"], "observed_minutes": place["observed_minutes"],
               "activities": place.get("activities") or {},
               "first_seen": place.get("first_seen"), "last_seen": place.get("last_seen")}
              for place in day["places_including_home"]]
    def endpoint_name(coordinate):
        if not coordinate:
            return None
        matches = [(metres(coordinate[1], coordinate[0], place["lat"], place["lon"]), place["name"])
                   for place in places]
        matches = [match for match in matches if match[0] <= 100]
        return min(matches, default=(0, None), key=lambda match: match[0])[1]
    trips = []
    for trip in day["trips"]:
        duration = max(0, (datetime.fromisoformat(trip["end"]) - datetime.fromisoformat(trip["start"])).total_seconds())
        trips.append({"start": trip["start"], "end": trip["end"], "duration_minutes": round(duration / 60, 1),
                      "mode": trip["mode"], "distance_km": trip["distance_km"],
                      "from": trip.get("from_name") or endpoint_name([trip["from_lon"], trip["from_lat"]]),
                      "to": trip.get("to_name") or endpoint_name([trip["to_lon"], trip["to_lat"]]),
                      "round_trip": bool(trip.get("loop")), "via": (trip.get("turnaround") or {}).get("name") if trip.get("loop") else None,
                      "mode_corrected": bool(trip.get("mode_corrected")),
                      "coverage_complete": trip["coverage_complete"]})
    visits = [{"place": stay.get("display_name") or stay.get("place_name"), "zone": stay.get("zone"),
               "arrival": stay["started_at"], "departure": stay["ended_at"],
               "observed_minutes": stay["observed_minutes"], "kind": stay["kind"],
               "activity": stay.get("activity"),
               "distance_km": round((stay.get("distance_m") or 0) / 1000, 2) if stay["kind"] == "area" else None,
               "moved_within": [{"mode": move["mode"], "distance_km": move["distance_km"]} for move in stay["internal_moves"]],
               "ongoing": bool(stay["ongoing"])}
              for stay in day["stays"]]
    # Saved places are zones: time inside each one, across the places in it.
    zones = {}
    for visit in visits:
        if visit["zone"]:
            item = zones.setdefault(visit["zone"], {"name": visit["zone"], "observed_minutes": 0.0, "places": []})
            item["observed_minutes"] += visit["observed_minutes"]
            if visit["place"] not in item["places"]:
                item["places"].append(visit["place"])
    zones = [{**item, "observed_minutes": round(item["observed_minutes"], 1)} for item in zones.values()
             if any(place != item["name"] for place in item["places"])]
    home_return = _home_return(store, day_start, day_end, source)
    if home_return:
        home_return["summary"] = _home_return_text(home_return, zone)
    trip_distance_km = round(sum(trip["distance_km"] for trip in trips), 2)
    totals = {"raw_fixes": day["points"], "timeline_fixes": day["timeline_points"],
              "distance_km": trip_distance_km, "sampled_distance_km": day["stats"]["distance_km"],
              "moving_hours": day["stats"]["moving_hours"],
              "trips": len(trips), "places": len(places),
              "reminders_created": len(reminders_created), "reminders_triggered": len(reminder_events),
              "reminders_cancelled": len(reminders_cancelled),
              "active_reminders_at_end": len(reminders_active_at_end)}
    activities = [f"{mode} {values['minutes']:.0f} min ({values['distance_km']:.1f} km)"
                  for mode, values in day["activity_totals"].items() if values["count"]]
    overview = (f"{len(trips)} trips covering {totals['distance_km']:.2f} km; "
                f"{len(places)} observed places; "
                + (f"activity: {', '.join(activities)}; " if activities else "")
                + f"{len(reminder_events)} reminders triggered; {day['points']} raw fixes.")
    if home_return:
        overview += " " + home_return["summary"]
    value = {"schema_version": 7, "date": date, "timezone": timezone_name, "source": source,
             "overview": overview, "totals": totals, "activity_counts": day["activity_counts"],
             "activity_totals": day["activity_totals"], "visits": visits,
             "places": places, "zones": zones, "trips": trips, "reminders_created": reminders_created,
             "reminders_triggered": reminder_events, "reminders_cancelled": reminders_cancelled,
             "active_reminders_at_end": reminders_active_at_end, "first_fix": day["first_fix"],
             "last_fix": day["last_fix"], "coverage_gaps": day["coverage_gaps"], "coverage_note": day["coverage_note"],
             "home_return": home_return,
             "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if canonical:
        # A day nothing was recorded on is not worth a record; drop one saved before data was removed.
        if totals["raw_fixes"]:
            store.save_daily_overview(value, watermark, daily_brief(value))
        else:
            store.delete_daily_overview(date, timezone_name)
    return value


BRIEF_MOVES = {"walking": "walked {}", "running": "ran {}", "cycling": "cycled {}",
               "driving": "drove {}", "transit": "rode {} by transit"}


def _brief_duration(minutes):
    hours, rest = divmod(round(minutes or 0), 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _brief_move(mode, distance_km):
    return BRIEF_MOVES.get(mode, mode + " {}").format(f"{distance_km or 0:.1f} km")


def _brief_count(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def daily_brief(overview):
    """The day in short plain-text lines, for an LLM to read cheaply.

    Derived from the full overview, so the two never disagree. Only what
    happened is stated: no false flags, empty lists, ids, or coordinates.
    Times are local HH:MM; a time on another day carries its date.
    """
    zone = ZoneInfo(overview["timezone"])
    day = datetime.strptime(overview["date"], "%Y-%m-%d").date()

    def local(value):
        return datetime.fromisoformat(value).astimezone(zone)

    def clock(moment):
        return moment.strftime("%H:%M") if moment.date() == day else moment.strftime("%Y-%m-%d %H:%M")

    def reminder(item, at):
        place = f"any place named {item['match_name']}" if item.get("match_name") else item.get("place_name")
        return f"{clock(local(item[at]))} '{item['text']}'" + (f" at {place}" if place else "")

    brief = {"date": f"{day.strftime('%A')}, {day.isoformat()} ({overview['timezone']})"}
    if overview.get("source"):
        brief["source"] = overview["source"]
    totals = overview["totals"]
    if not totals["raw_fixes"]:
        brief["summary"] = "No location recorded."
    else:
        trips = overview["trips"]
        summary = f"{_brief_count(len(overview['places']), 'place')}, {_brief_count(len(trips), 'trip')}"
        summary += f" ({totals['distance_km']:.1f} km)." if trips else "."
        moves = [f"{_brief_move(mode, values['distance_km'])} ({_brief_duration(values['minutes'])})"
                 for mode, values in overview["activity_totals"].items() if values["count"]]
        if moves:
            joined = ", ".join(moves)
            summary += " " + joined[0].upper() + joined[1:] + "."
        brief["summary"] = summary
    timeline = []
    for visit in overview["visits"]:
        arrival, departure = local(visit["arrival"]), local(visit["departure"])
        before, after = arrival.date() < day, departure.date() > day
        if before and (after or visit["ongoing"]):
            span = "all day"
        elif before:
            span = f"until {clock(departure)}"
        elif after or visit["ongoing"]:
            span = f"from {clock(arrival)}"
        else:
            span = f"{clock(arrival)}-{clock(departure)}"
        text = f"{span} at {visit['place'] or 'an unnamed place'}"
        if visit.get("zone") and visit["zone"] != visit["place"]:
            text += f" ({visit['zone']})"
        if visit.get("activity"):
            text += ", " + (_brief_move(visit["activity"], visit["distance_km"]) if visit.get("distance_km")
                            else visit["activity"])
        if visit.get("moved_within"):
            text += "; within it " + ", ".join(_brief_move(move["mode"], move["distance_km"]) for move in visit["moved_within"])
        if visit["ongoing"] and not after:
            # The latest stay: "still there" as of the last fix, not necessarily now.
            text += f", still there at {clock(departure)}"
        timeline.append((arrival, text))
    for trip in overview["trips"]:
        start = local(trip["start"])
        text = f"{clock(start)}-{clock(local(trip['end']))} {_brief_move(trip['mode'], trip['distance_km'])}"
        if trip["round_trip"]:
            text += f" round trip from {trip['from'] or 'an unnamed place'}" + (f" via {trip['via']}" if trip.get("via") else "")
        else:
            text += (f" from {trip['from']}" if trip["from"] else "") + (f" to {trip['to']}" if trip["to"] else "")
        if not trip["coverage_complete"]:
            text += ", partly unrecorded"
        timeline.append((start, text))
    for gap in overview["coverage_gaps"]:
        start = local(gap["start"])
        timeline.append((start, f"{clock(start)}-{clock(local(gap['end']))} no location data"))
    if timeline:
        brief["timeline"] = [text for _moment, text in sorted(timeline, key=lambda item: item[0])]
    places = []
    for place in overview["places"]:
        name = place["name"] or place["address"] or "Unnamed place"
        category = (place.get("category") or "").split(",")[0].rsplit(".", 1)[-1].replace("_", " ")
        if category.lower() in name.lower():
            category = ""
        zone_name = place.get("zone") if place.get("zone") != name else ""
        if category and category.lower() in (zone_name or "").lower():
            category = ""
        details = [item for item in (zone_name, category, place["address"] if place["address"] != name else "") if item]
        text = name + (f" ({', '.join(details)})" if details else "") + ": " + _brief_duration(place["observed_minutes"])
        if place["visits"] > 1:
            text += f" over {place['visits']} visits"
        activities = [mode if count == 1 else f"{mode} x{count}" for mode, count in (place.get("activities") or {}).items()]
        if activities:
            text += ", " + ", ".join(activities)
        if place["home"] and name.lower() != "home":
            text += ", home"
        places.append(text)
    if places:
        brief["places"] = places
    if overview.get("zones"):
        brief["zones"] = [f"{item['name']}: {_brief_duration(item['observed_minutes'])} at {', '.join(item['places'])}"
                          for item in overview["zones"]]
    if overview["reminders_created"]:
        brief["reminders_set"] = [reminder(item, "created_at") for item in overview["reminders_created"]]
    if overview["reminders_triggered"]:
        brief["reminders_triggered"] = [reminder(item, "triggered_at") for item in overview["reminders_triggered"]]
    if overview.get("home_return"):
        brief["home_return"] = overview["home_return"]["summary"]
    return brief


def daily_briefs(store, date, end_date=None):
    """Briefs for consecutive local days, oldest first: up to 31 per call."""
    try:
        first = datetime.strptime(date, "%Y-%m-%d").date()
        last = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else first
    except ValueError as exc:
        raise ValueError("date and end_date must be YYYY-MM-DD") from exc
    if not 0 <= (last - first).days <= 30:
        raise ValueError("end_date must be on or up to 30 days after date")
    return [daily_brief(daily_overview(store, (first + timedelta(days=offset)).isoformat()))
            for offset in range((last - first).days + 1)]


def stored_daily_record(store, date):
    """Exactly what the catalog holds for a date, and whether its inputs changed since.

    Reading never rebuilds the record; daily_overview() does that.
    """
    timezone_name = store.settings()["timezone"]
    try:
        local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(timezone_name))
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    row = store.db.execute("""SELECT summary_date,timezone,watermark,summary,brief,created_at,updated_at FROM daily_summaries
        WHERE summary_date=? AND timezone=? AND source='' AND points>0""", (date, timezone_name)).fetchone()
    if row is None:
        raise ValueError(f"no summary is stored for {date} yet")
    start = local_start.astimezone(timezone.utc).isoformat()
    day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    end = (day_end - timedelta(microseconds=1)).isoformat()
    home_context_start, home_context_end = _home_return_context(local_start.astimezone(timezone.utc), day_end)
    return {"date": row["summary_date"], "timezone": row["timezone"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "stale": row["watermark"] != store.daily_overview_watermark(
                start, end, None, home_context_start.isoformat(), home_context_end.isoformat()),
            "summary": json.loads(row["summary"]), "brief": json.loads(row["brief"]) if row["brief"] else None}


def finalize_previous_day(store, now=None):
    timezone_name = store.settings()["timezone"]
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown configured timezone") from exc
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    date = (local_now.date() - timedelta(days=1)).isoformat()
    return daily_overview(store, date, timezone_name)


def save_scheduled_summaries(store, now=None):
    """Save the day that ended and, on an interval schedule, today so far.

    Runs every minute. The completed day is always kept current; with the
    interval schedule today's record is rebuilt once it is that many hours old
    (a record nobody has asked for yet counts as old).
    """
    finalize_previous_day(store, now)
    settings = store.settings()
    if settings["summary_schedule"] != "interval":
        return
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(settings["timezone"]))
    today = local_now.date().isoformat()
    row = store.db.execute("""SELECT updated_at FROM daily_summaries
        WHERE summary_date=? AND timezone=? AND source=''""", (today, settings["timezone"])).fetchone()
    if row is None:
        # Nothing recorded yet today: there is no record to save, so skip the work.
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        due = store.observation_count(midnight.astimezone(timezone.utc).isoformat(), now.isoformat()) > 0
    else:
        due = now - datetime.fromisoformat(row["updated_at"]) >= timedelta(hours=settings["summary_interval_hours"])
    if due:
        daily_overview(store, today, settings["timezone"])


def delete_samples(store, ids, confirm=False):
    """Preview, or with confirm=True permanently delete, raw samples.

    The affected sources' stays are rebuilt and the stored summaries of the
    affected days refreshed, so history never refers to a deleted sample.
    """
    plan = store.sample_deletion(ids)
    timezone_name = store.settings()["timezone"]
    zone = ZoneInfo(timezone_name)
    dates = sorted({datetime.fromisoformat(value).astimezone(zone).date().isoformat() for value in plan["timestamps"]})
    preview = {"samples": plan["samples"], "sources": plan["sources"], "first": plan["first"], "last": plan["last"],
               "dates": dates, "reminder_triggers": [{"reminder": item["reminder"], "triggered_at": item["triggered_at"]}
                                                     for item in plan["reminder_triggers"]]}
    if not confirm:
        return {**preview, "deleted": False}
    store.delete_samples(plan)
    process_stays(store, plan["sources"], rebuild=True)
    store.db.commit()
    for date in dates:
        # Rebuild (or drop, when nothing is left) each stored record for those days.
        if store.stored_daily_overview(date, timezone_name) is not None:
            daily_overview(store, date, timezone_name)
    return {**preview, "deleted": True}
