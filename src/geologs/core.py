"""Location domain model with SQLite and PostgreSQL/PostGIS storage."""
from __future__ import annotations

import csv
import difflib
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fuel import EIA_AREAS

from .postgres import PostgresConnection


_reverse_lock = threading.Lock()
# How stays and travel modes are read from samples. Changing one rebuilds
# stays and makes stored daily summaries stale. Speeds are median km/h: below
# walking_max is a walk, below running_max a run, below cycling_max a ride, and
# faster is a drive; stretches at driving_min or more count as driving even
# when the median is slower (stop-and-go traffic). quick_stop_minutes finds
# shorter stops on the way (a fuel stop); 0 turns that off.
ANALYSIS_DEFAULTS = {"stay_min_minutes": 15, "quick_stop_minutes": 3, "walking_max_kmh": 6.5,
                     "running_max_kmh": 15.0, "cycling_max_kmh": 25.0, "driving_min_kmh": 35.0}
# Completed days are always saved after midnight; "interval" also saves today so far.
SUMMARY_SCHEDULES = ("nightly", "interval")
# An empty places_provider means the first configured provider (see places_provider()).
# source_priority lists trackers, most trusted first; when several report the
# same time, the first one with data wins. Unlisted sources follow, busiest first.
DEFAULT_SETTINGS = {"speed_strategy": "reported", "reported_speed_offset_kmh": 0.0, "timezone": "UTC",
                    "places_provider": "", **ANALYSIS_DEFAULTS,
                    "summary_schedule": "nightly", "summary_interval_hours": 3, "source_priority": [],
                    **{"units": "metric", "currency": "USD", "fuel_economy_l100km": None, "fuel_grade": "regular",
                       "fuel_price_source": "auto", "fuel_area": ""}}
# Display units; everything is stored metric. Fuel economy is kept in L/100 km and
# prices per litre, whatever you entered them in.
UNIT_SYSTEMS = ("metric", "imperial")
FUEL_GRADES = ("regular", "midgrade", "premium", "diesel")
# A higher-priority tracker's latest fix stands for where you are while it is
# this close to the newest fix from any tracker.
PRIORITY_FRESH_SECONDS = 900
MAX_CONNECTED_GAP_SECONDS = 900
# A trip's unrecorded leg shorter than this (its first fix inside the stay it left) is not drawn.
ROUTE_GAP_MIN_M = 25
MAX_PLAUSIBLE_SPEED_MPS = 55.6
# Transit (bus, train) is never guessed from speed; it only comes from a correction.
MOVEMENT_MODES = ("driving", "transit", "cycling", "running", "walking")
# A correction applies to a trip or area stay that overlaps its window by at
# least this share, so it survives trips being recalculated a minute either way.
MODE_OVERRIDE_MIN_OVERLAP = .5
# A stay may bridge this much time without samples; longer gaps split it.
MAX_SAMPLE_GAP_SECONDS = 1800
# Apps such as OwnTracks and Overland report only while you move, so silence
# usually means you stayed put. A quiet stretch up to this long that ends at
# the same spot is time spent there, and one that ends elsewhere is time there
# until you left (estimated from the distance to the next fix).
MAX_QUIET_STAY_SECONDS = 16 * 3600
QUIET_DEPARTURE_SPEED_MPS = 30 / 3.6
# Travel that leaves and returns to one place is kept only when it really went somewhere;
# smaller excursions are GPS drift between two halves of one stay.
ROUND_TRIP_MIN_EXCURSION_M = 150
ROUND_TRIP_MIN_DISTANCE_M = 300
# Leaving a place takes this many accurate fixes away from it, spanning this
# long. Indoor GPS jumps last a sample or two; a real errand lasts longer.
AWAY_MIN_FIXES = 3
AWAY_MIN_SECONDS = 240
# Short moves cannot be told from driving by speed alone; a drive needs this much distance.
DRIVING_MIN_DISTANCE_M = 500
# One delete request covers at most a page of raw samples.
MAX_SAMPLE_DELETE = 500


def database_path() -> str:
    if os.environ.get("GEOLOGS_DB"):
        return os.environ["GEOLOGS_DB"]
    path = Path.home() / ".local/share/geologs/geologs.db"
    # An archive created before the rename (as Geotracker) stays where it is.
    legacy = Path.home() / ".local/share/geotracker/geotracker.db"
    return str(legacy if legacy.exists() and not path.exists() else path)


def parse_time(value):
    if value is None or value == "":
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="auto")


def metres(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


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


def _same_source_state(previous, point):
    """True when polling returned the same source event at the same position."""
    external_id = previous.get("external_id")
    return (previous["source"] == point["source"] and external_id is not None
            and external_id == point.get("external_id")
            and metres(previous["lat"], previous["lon"], point["lat"], point["lon"]) <= 50)


def _connected_elapsed(previous, point, held_since=None):
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
    if held_since is not None and held_since is not previous and _same_source_state(held_since, previous):
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


def _corridor_distance(point, origin, destination):
    """Metres from a fix to the straight corridor joining two stays."""
    x_scale = 111320 * math.cos(math.radians(origin["lat"]))
    bx, by = (destination["lon"] - origin["lon"]) * x_scale, (destination["lat"] - origin["lat"]) * 110574
    px, py = (point["lon"] - origin["lon"]) * x_scale, (point["lat"] - origin["lat"]) * 110574
    length = bx * bx + by * by
    t = 0.0 if length == 0 else max(0.0, min(1.0, (px * bx + py * by) / length))
    return math.hypot(px - t * bx, py - t * by)


def went_elsewhere_between(points, origin, destination):
    """Whether travel between two stays went somewhere else.

    `points` are the fixes strictly between the stays. A fix is away when,
    even after allowing for its reported accuracy, it lies more than
    ROUND_TRIP_MIN_EXCURSION_M outside the corridor joining the stays (and
    beyond an area stay's extent). Walking between two buildings stays in the
    corridor; an indoor GPS jump lasts a sample or two; an errand needs
    AWAY_MIN_FIXES away fixes spanning AWAY_MIN_SECONDS. Returns None when a
    coverage gap longer than a stay may bridge hides what happened.
    """
    times = ([datetime.fromisoformat(origin["ended_at"])]
             + [datetime.fromisoformat(point["timestamp"]) for point in points]
             + [datetime.fromisoformat(destination["started_at"])])
    if any((later - earlier).total_seconds() > MAX_SAMPLE_GAP_SECONDS for earlier, later in zip(times, times[1:])):
        return None
    reach = max(origin.get("radius_m") or 0, destination.get("radius_m") or 0)
    away = [point for point in points
            if (point["accuracy_m"] is None or point["accuracy_m"] <= 100)
            and _corridor_distance(point, origin, destination) - reach - (point["accuracy_m"] or 0)
            >= ROUND_TRIP_MIN_EXCURSION_M]
    if len(away) < AWAY_MIN_FIXES:
        return False
    span = datetime.fromisoformat(away[-1]["timestamp"]) - datetime.fromisoformat(away[0]["timestamp"])
    return span.total_seconds() >= AWAY_MIN_SECONDS


def trip_turnaround(points, origin):
    """Where a round trip went: its longest stop of two minutes or more, else its farthest fix."""
    usable = [point for point in points if point["accuracy_m"] is None or point["accuracy_m"] <= 100]
    best, index = None, 0
    while index < len(usable):
        end = index
        while end + 1 < len(usable) and metres(usable[index]["lat"], usable[index]["lon"],
                                               usable[end + 1]["lat"], usable[end + 1]["lon"]) <= 60:
            end += 1
        seconds = (datetime.fromisoformat(usable[end]["timestamp"]) - datetime.fromisoformat(usable[index]["timestamp"])).total_seconds()
        if seconds >= 120 and (best is None or seconds > best[0]):
            best = (seconds, index, end)
        index = end + 1
    if best:
        seconds, first, last = best
        stop = usable[first:last + 1]
        return {"lat": sum(point["lat"] for point in stop) / len(stop), "lon": sum(point["lon"] for point in stop) / len(stop),
                "stop_minutes": round(seconds / 60, 1), "arrived_at": stop[0]["timestamp"],
                "distance_m": round(metres(origin["lat"], origin["lon"], stop[0]["lat"], stop[0]["lon"]))}
    farthest = max(usable or points, key=lambda point: metres(origin["lat"], origin["lon"], point["lat"], point["lon"]))
    return {"lat": farthest["lat"], "lon": farthest["lon"], "stop_minutes": 0, "arrived_at": farthest["timestamp"],
            "distance_m": round(metres(origin["lat"], origin["lon"], farthest["lat"], farthest["lon"]))}


def is_excursion(trip):
    """True when a trip went far enough to matter even if it ended where it began.

    A trip known not to have left (see went_elsewhere_between) never qualifies; one
    whose coverage is unknown must at least reach far enough.
    """
    return (trip.get("went_elsewhere") is not False
            and trip.get("max_excursion_m", 0) >= ROUND_TRIP_MIN_EXCURSION_M
            and trip["distance_km"] * 1000 >= ROUND_TRIP_MIN_DISTANCE_M)


def stats_from_points(points):
    """Summarize chronological fixes without bridging source or coverage gaps."""
    distance, moving_seconds, by_mode = 0, 0, {}
    by_source = {}
    for point in points:
        by_mode[point["mode"]] = by_mode.get(point["mode"], 0) + 1
        by_source.setdefault(point["source"], []).append(point)
    for source_points in by_source.values():
        source_points.sort(key=lambda point: (point["timestamp"], point.get("id", 0)))
        held_since = source_points[0] if source_points else None
        for prev, point in zip(source_points, source_points[1:]):
            elapsed = _connected_elapsed(prev, point, held_since)
            segment = metres(prev["lat"], prev["lon"], point["lat"], point["lon"])
            if elapsed is not None:
                distance += segment
                if point["mode"] in ("walking", "running_cycling", "driving"):
                    moving_seconds += elapsed
            if not _same_source_state(prev, point):
                held_since = point
    return {"points": len(points), "distance_km": round(distance / 1000, 2), "moving_hours": round(moving_seconds / 3600, 2), "modes": by_mode,
            "first": points[0]["timestamp"] if points else None, "last": points[-1]["timestamp"] if points else None}


def trips_between_stays(points, stay_rows, start=None, end=None, speeds=None):
    """Build one trip between each pair of consecutive confirmed stays."""
    points_by_source, stays_by_source = {}, {}
    for point in select_best_timed_fixes(points):
        points_by_source.setdefault(point["source"], []).append(point)
    for stay in stay_rows:
        stays_by_source.setdefault(stay["source"], []).append(stay)
    start_at = datetime.fromisoformat(parse_time(start)) if start else None
    end_at = datetime.fromisoformat(parse_time(end)) if end else None
    trips = []
    for source, source_stays in stays_by_source.items():
        source_stays.sort(key=lambda stay: (stay["started_at"], stay["id"]))
        source_points = points_by_source.get(source, [])
        for origin, destination in zip(source_stays, source_stays[1:]):
            departure = datetime.fromisoformat(origin["ended_at"])
            arrival = datetime.fromisoformat(destination["started_at"])
            if arrival <= departure or (arrival - departure).total_seconds() > 86400:
                continue
            if start_at and arrival < start_at:
                continue
            if end_at and departure > end_at:
                continue
            trip_points = [point for point in source_points
                           if departure <= datetime.fromisoformat(point["timestamp"]) <= arrival]
            if len(trip_points) < 2:
                continue
            moves, held_since = [], trip_points[0]
            for previous, point in zip(trip_points, trip_points[1:]):
                seconds = _connected_elapsed(previous, point, held_since)
                distance = metres(previous["lat"], previous["lon"], point["lat"], point["lon"])
                if seconds is not None and distance >= 10:
                    moves.append((distance, seconds, reported_speed(point), move_uncertainty(previous, point)))
                if not _same_source_state(previous, point):
                    held_since = point
            motion = classify_motion(moves, speeds)
            if motion is None:
                continue
            # Stays carry this flag from analysis; compute it for hand-built rows.
            left = (destination["went_elsewhere"] if "went_elsewhere" in destination
                    else went_elsewhere_between(trip_points[1:-1], origin, destination))
            # Area stays (a lake loop) have an extent; distance inside it is not an excursion.
            origin_reach = origin.get("radius_m") or 0
            destination_reach = destination.get("radius_m") or 0
            excursion = max(metres(origin["lat"], origin["lon"], point["lat"], point["lon"]) for point in trip_points) - origin_reach
            loop = (metres(origin["lat"], origin["lon"], destination["lat"], destination["lon"])
                    <= max(100, origin_reach + 50, destination_reach + 50))
            trip = {
                "start": origin["ended_at"], "end": destination["started_at"],
                "source": source, "distance_km": round(motion["distance_m"] / 1000, 2),
                "duration_minutes": round((arrival - departure).total_seconds() / 60, 1),
                "moving_minutes": round(motion["moving_seconds"] / 60, 1),
                "median_kmh": motion["median_kmh"], "speed_reported": motion["speed_reported"],
                "max_excursion_m": max(0, round(excursion)),
                "mode": motion["mode"], "modes": {motion["mode"]: len(moves)}, "loop": loop,
                "went_elsewhere": None if left is None else bool(left),
                "from_stay_id": origin["id"], "to_stay_id": destination["id"],
                "from_name": origin.get("place_name"), "to_name": destination.get("place_name"),
                "from_lat": origin["lat"], "from_lon": origin["lon"],
                "to_lat": destination["lat"], "to_lon": destination["lon"],
                "recorded_points": len(trip_points),
            }
            # Two stays at one spot joined by drift or a GPS jump are one visit, not a trip.
            if loop and not is_excursion(trip):
                continue
            # Named only for round trips, where it says where you went.
            trip["turnaround"] = trip_turnaround(trip_points[1:-1] or trip_points, origin)
            pieces = [smooth_route_segment(segment) for segment in route_segments_from_points(trip_points, keep_lone=True)]
            route_segments = [piece for piece in pieces if len(piece) > 1]
            trip.update({"route_segments": route_segments, "route_gaps": route_gaps(pieces, origin, destination),
                         "coordinates": route_segments[0] if len(route_segments) == 1 else [],
                         "coverage_complete": len(route_segments) == 1})
            trips.append(trip)
    return sorted(trips, key=lambda trip: trip["start"], reverse=True)


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
            connected = previous is not None and _connected_elapsed(previous, point, held_since) is not None
            if not connected:
                if len(current) > 1 or current and keep_lone:
                    segments.append(current)
                current = [[point["lon"], point["lat"]]]
            else:
                current.append([point["lon"], point["lat"]])
            if previous is None or not _same_source_state(previous, point):
                held_since = point
            previous = point
        if len(current) > 1 or current and keep_lone:
            segments.append(current)
    return segments


def route_gaps(pieces, origin, destination):
    """Straight [lon, lat] legs over a trip's unrecorded stretches.

    A trip runs from one stay to the next, so where its recorded pieces (in
    time order) stop short of those stays or of each other, you still went
    that way; a sparse tracker leaves most of a trip like this.
    """
    ends = [[[origin["lon"], origin["lat"]]], *pieces, [[destination["lon"], destination["lat"]]]]
    return [[before[-1], after[0]] for before, after in zip(ends, ends[1:])
            if metres(before[-1][1], before[-1][0], after[0][1], after[0][0]) > ROUTE_GAP_MIN_M]


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


# A reminder has a place (geofence) or, since v13, only a name to match.
SQLITE_REMINDERS_TABLE = """CREATE TABLE {name} (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER REFERENCES places(id),
        active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
        cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
        created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT,
        destination_name TEXT, destination_address TEXT,
        destination_category TEXT, destination_provider_place_id TEXT, match_name TEXT
    )"""
REMINDER_COLUMNS = ("id,text,place_id,active,repeat,cooldown_minutes,created_at,last_triggered_at,cancelled_at,"
                    "destination_name,destination_address,destination_category,destination_provider_place_id")


class Store:
    def __init__(self, path=None):
        url = os.environ.get("GEOLOGS_DATABASE_URL") if path is None else None
        self.postgres = bool(url)
        self.path = str(url or path or database_path())
        if self.postgres:
            self.db = PostgresConnection(url)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT,
            timestamp TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
            accuracy_m REAL, speed_mps REAL, speed_origin TEXT, mode TEXT NOT NULL,
            UNIQUE(source, external_id), UNIQUE(source, timestamp, lat, lon)
        );
        CREATE INDEX IF NOT EXISTS points_time ON points(timestamp);
        CREATE INDEX IF NOT EXISTS points_source_time ON points(source,timestamp);
        CREATE TABLE IF NOT EXISTS point_observations (
            id INTEGER PRIMARY KEY, point_id INTEGER NOT NULL REFERENCES points(id),
            legacy_point_id INTEGER UNIQUE REFERENCES points(id),
            source TEXT NOT NULL, timestamp TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, accuracy_m REAL, speed_mps REAL,
            speed_origin TEXT,
            received_at TEXT NOT NULL, fingerprint TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS point_observations_time ON point_observations(timestamp);
        CREATE INDEX IF NOT EXISTS point_observations_source_time ON point_observations(source,timestamp);
        CREATE INDEX IF NOT EXISTS point_observations_source_id ON point_observations(source,id);
        CREATE TABLE IF NOT EXISTS timeline_fixes (
            observation_id INTEGER PRIMARY KEY REFERENCES point_observations(id) ON DELETE CASCADE,
            source TEXT NOT NULL, role TEXT NOT NULL,
            stay_id INTEGER REFERENCES observed_stays_v2(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS timeline_fixes_source ON timeline_fixes(source);
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_cache (
            cache_key TEXT PRIMARY KEY, watermark TEXT NOT NULL,
            value TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY, summary_date TEXT NOT NULL, timezone TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '', watermark TEXT NOT NULL,
            points INTEGER NOT NULL, distance_km REAL NOT NULL,
            trip_count INTEGER NOT NULL, place_count INTEGER NOT NULL,
            reminder_count INTEGER NOT NULL, summary TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, brief TEXT,
            UNIQUE(summary_date,timezone,source)
        );
        CREATE INDEX IF NOT EXISTS daily_summaries_date ON daily_summaries(summary_date DESC,timezone);
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 150,
            address TEXT, category TEXT, provider_place_id TEXT, saved INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER REFERENCES places(id),
            active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
            cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
            created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT,
            destination_name TEXT, destination_address TEXT,
            destination_category TEXT, destination_provider_place_id TEXT, match_name TEXT
        );
        CREATE INDEX IF NOT EXISTS reminders_active_created ON reminders(active,created_at);
        CREATE TABLE IF NOT EXISTS mode_overrides (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, mode TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fuel_prices (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, effective_date TEXT NOT NULL, price_per_l REAL NOT NULL,
            fetched_at TEXT NOT NULL, UNIQUE(source, effective_date)
        );
        CREATE TABLE IF NOT EXISTS ingest_tokens (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, hint TEXT NOT NULL,
            source TEXT, created_at TEXT NOT NULL, last_used_at TEXT, last_source TEXT
        );
        CREATE TABLE IF NOT EXISTS stay_edits (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, action TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, lat REAL, lon REAL,
            created_at TEXT NOT NULL
        );
        DROP TABLE IF EXISTS ingest_status;
        CREATE TABLE IF NOT EXISTS ingest_devices (
            kind TEXT NOT NULL, device TEXT NOT NULL DEFAULT '', last_attempt_at TEXT NOT NULL,
            last_status INTEGER NOT NULL, last_message TEXT, last_success_at TEXT,
            received INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, device)
        );
        CREATE TABLE IF NOT EXISTS place_labels (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
            radius_m REAL NOT NULL, address TEXT, category TEXT, provider_place_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            source TEXT, started_at TEXT, ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL REFERENCES reminders(id),
            point_id INTEGER NOT NULL REFERENCES points(id), triggered_at TEXT NOT NULL,
            webhook_status TEXT DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS events_triggered ON events(triggered_at);
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY, point_id INTEGER NOT NULL REFERENCES points(id)
        );
        CREATE TABLE IF NOT EXISTS observed_stays (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            start_point_id INTEGER NOT NULL UNIQUE REFERENCES points(id),
            end_point_id INTEGER NOT NULL REFERENCES points(id),
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            point_count INTEGER NOT NULL, ongoing INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS observed_stays_time ON observed_stays(started_at);
        CREATE TABLE IF NOT EXISTS stay_processing_state (
            source TEXT PRIMARY KEY, last_point_id INTEGER NOT NULL,
            last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observed_stays_v2 (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            start_observation_id INTEGER NOT NULL UNIQUE REFERENCES point_observations(id),
            end_observation_id INTEGER NOT NULL REFERENCES point_observations(id),
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            point_count INTEGER NOT NULL, ongoing INTEGER NOT NULL DEFAULT 1,
            kind TEXT NOT NULL DEFAULT 'stationary', activity TEXT, distance_m REAL, radius_m REAL,
            went_elsewhere INTEGER
        );
        CREATE INDEX IF NOT EXISTS observed_stays_v2_time ON observed_stays_v2(started_at);
        CREATE INDEX IF NOT EXISTS observed_stays_v2_source_time ON observed_stays_v2(source,started_at,ended_at);
        CREATE TABLE IF NOT EXISTS stay_observation_state (
            source TEXT PRIMARY KEY, last_observation_id INTEGER NOT NULL,
            last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS known_paths (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            mode TEXT NOT NULL, distance_m REAL NOT NULL,
            min_lat REAL NOT NULL, min_lon REAL NOT NULL, max_lat REAL NOT NULL, max_lon REAL NOT NULL,
            coordinates TEXT NOT NULL, UNIQUE(source, started_at)
        );
        CREATE INDEX IF NOT EXISTS known_paths_bounds ON known_paths(min_lat,max_lat);
        CREATE TABLE IF NOT EXISTS geocode_cache (
            cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, value TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reverse_regions (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
            center_lat REAL NOT NULL, center_lon REAL NOT NULL, radius_m REAL NOT NULL,
            value TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reverse_regions_provider_expiry
            ON reverse_regions(provider, expires_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS reverse_regions_spatial
            USING rtree(id, min_lat, max_lat, min_lon, max_lon);
        CREATE TRIGGER IF NOT EXISTS reverse_regions_delete AFTER DELETE ON reverse_regions
        BEGIN DELETE FROM reverse_regions_spatial WHERE id=old.id; END;
            """)
        if not self.postgres:
            label_columns = {row[1] for row in self.db.execute("PRAGMA table_info(place_labels)")}
            for column in ("source", "started_at", "ended_at"):
                if column not in label_columns:
                    self.db.execute(f"ALTER TABLE place_labels ADD COLUMN {column} TEXT")
            if "brief" not in {row[1] for row in self.db.execute("PRAGMA table_info(daily_summaries)")}:
                self.db.execute("ALTER TABLE daily_summaries ADD COLUMN brief TEXT")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            rebuild_analysis = version < 18
            if version < 1:
                self.db.execute("UPDATE points SET mode='running_cycling' WHERE mode='cycling'")
            if version < 2:
                self.db.execute("""INSERT OR IGNORE INTO point_observations
                    (point_id,legacy_point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,received_at)
                    SELECT id,id,source,timestamp,lat,lon,accuracy_m,speed_mps,timestamp FROM points""")
                self.db.execute("PRAGMA user_version=2")
            if version < 3:
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(point_observations)")}
                if "speed_origin" not in columns:
                    self.db.execute("ALTER TABLE point_observations ADD COLUMN speed_origin TEXT")
                self.db.execute("""UPDATE point_observations SET speed_mps=(SELECT p.speed_mps FROM points p WHERE p.id=point_id),
                    speed_origin=(SELECT p.speed_origin FROM points p WHERE p.id=point_id)
                    WHERE legacy_point_id IS NOT NULL OR
                    (speed_mps IS NULL AND EXISTS (SELECT 1 FROM points p WHERE p.id=point_id
                        AND p.timestamp=point_observations.timestamp AND p.lat=point_observations.lat
                        AND p.lon=point_observations.lon AND p.accuracy_m IS point_observations.accuracy_m))""")
                self.db.execute("UPDATE point_observations SET speed_origin='reported' WHERE speed_mps IS NOT NULL AND speed_origin IS NULL")
                self.db.execute("""WITH prior AS (
                    SELECT id,lat,lon,timestamp,
                        LAG(lat) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lat,
                        LAG(lon) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lon,
                        LAG(timestamp) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_timestamp
                    FROM point_observations)
                    UPDATE point_observations SET speed_mps=0,speed_origin='estimated'
                    WHERE speed_mps IS NULL AND id IN (
                        SELECT id FROM prior WHERE lat=prev_lat AND lon=prev_lon
                        AND (julianday(timestamp)-julianday(prev_timestamp))*86400 > 0
                        AND (julianday(timestamp)-julianday(prev_timestamp))*86400 <= 900)""")
                self.db.execute("PRAGMA user_version=3")
            if version < 6:
                # Location reminders are one-shot. Preserve already-triggered
                # repeating reminders as history instead of rearming them.
                self.db.execute("UPDATE reminders SET active=0 WHERE repeat=1 AND last_triggered_at IS NOT NULL")
                self.db.execute("UPDATE reminders SET repeat=0")
            if version < 7:
                reminder_columns = {row[1] for row in self.db.execute("PRAGMA table_info(reminders)")}
                if "cancelled_at" not in reminder_columns:
                    self.db.execute("ALTER TABLE reminders ADD COLUMN cancelled_at TEXT")
                self.db.execute("UPDATE reminders SET cancelled_at=created_at WHERE active=0 AND last_triggered_at IS NULL AND cancelled_at IS NULL")
            if version < 9:
                place_columns = {row[1] for row in self.db.execute("PRAGMA table_info(places)")}
                if "category" not in place_columns:
                    self.db.execute("ALTER TABLE places ADD COLUMN category TEXT")
                if "provider_place_id" not in place_columns:
                    self.db.execute("ALTER TABLE places ADD COLUMN provider_place_id TEXT")
            if version < 10:
                place_columns = {row[1] for row in self.db.execute("PRAGMA table_info(places)")}
                if "saved" not in place_columns:
                    self.db.execute("ALTER TABLE places ADD COLUMN saved INTEGER DEFAULT 1")
                reminder_columns = {row[1] for row in self.db.execute("PRAGMA table_info(reminders)")}
                for column in ("destination_name", "destination_address", "destination_category", "destination_provider_place_id"):
                    if column not in reminder_columns:
                        self.db.execute(f"ALTER TABLE reminders ADD COLUMN {column} TEXT")
                self.db.execute("""UPDATE reminders SET destination_name=(SELECT name FROM places WHERE id=place_id),
                    destination_address=(SELECT address FROM places WHERE id=place_id),
                    destination_category=(SELECT category FROM places WHERE id=place_id),
                    destination_provider_place_id=(SELECT provider_place_id FROM places WHERE id=place_id)
                    WHERE destination_name IS NULL""")
                event_columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
                if "acknowledged_at" in event_columns:
                    self.db.execute("ALTER TABLE events DROP COLUMN acknowledged_at")
            if version < 11:
                stay_columns = {row[1] for row in self.db.execute("PRAGMA table_info(observed_stays_v2)")}
                for column, definition in (("kind", "TEXT NOT NULL DEFAULT 'stationary'"), ("activity", "TEXT"),
                                           ("distance_m", "REAL"), ("radius_m", "REAL")):
                    if column not in stay_columns:
                        self.db.execute(f"ALTER TABLE observed_stays_v2 ADD COLUMN {column} {definition}")
            if version < 12:
                stay_columns = {row[1] for row in self.db.execute("PRAGMA table_info(observed_stays_v2)")}
                if "went_elsewhere" not in stay_columns:
                    self.db.execute("ALTER TABLE observed_stays_v2 ADD COLUMN went_elsewhere INTEGER")
            if version < 13:
                reminder_columns = {row[1]: row for row in self.db.execute("PRAGMA table_info(reminders)")}
                if reminder_columns["place_id"][3]:
                    self._rebuild_sqlite_reminders()
                elif "match_name" not in reminder_columns:
                    self.db.execute("ALTER TABLE reminders ADD COLUMN match_name TEXT")
                self._keep_canonical_daily_summaries(self.settings()["timezone"])
        # Stay algorithm versions: 5 and 8 changed stationary stays; 12 adds area
        # stays (moving around one place) and stray-fix tolerance; 14 records
        # whether travel between stays went somewhere else; 17 adds quick stops
        # on the way (SQLite user_version 14); 18 reads quiet stretches from apps
        # that report only while moving as time spent still (SQLite 15); 19 ends
        # a stop at a sharp fix far away instead of holding it as a stray (SQLite
        # 16); 20 measures a quick stop's spread from its centre and sets creeping
        # traffic apart (SQLite 17); 21 allows for accuracy at a stay's first fix,
        # so rolling in does not split it (SQLite 18). One rebuild covers every
        # missing version.
        rebuild_versions = (5, 8, 12, 14, 17, 18, 19, 20, 21)
        if self.postgres and self._missing_migrations(rebuild_versions):
            self.db.execute("SELECT pg_advisory_xact_lock(hashtext('geologs:stays:rebuild'))")
            missing = self._missing_migrations(rebuild_versions)
            if missing:
                from .analysis import process_stays
                process_stays(self, rebuild=True)
                for version_number in missing:
                    self.db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version_number,))
        elif not self.postgres and rebuild_analysis:
            from .analysis import process_stays
            process_stays(self, rebuild=True)
            self.db.execute("PRAGMA user_version=12")
        elif not self.db.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone() and self.db.execute("SELECT 1 FROM point_observations LIMIT 1").fetchone():
            if self.postgres:
                self.db.execute("SELECT pg_advisory_xact_lock(hashtext('geologs:stays:bootstrap'))")
            if not self.db.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone():
                from .analysis import process_stays
                process_stays(self)
        if not self.postgres and version < 8:
            self.db.execute("PRAGMA user_version=8")
        if not self.postgres and version < 9:
            self.db.execute("DELETE FROM analysis_cache")
            self.db.execute("PRAGMA user_version=9")
        if not self.postgres and version < 18:
            self.db.execute("PRAGMA user_version=18")
        if self.postgres and self._missing_migrations((16,)):
            # Keep one stored daily summary per date: the configured timezone, all sources.
            self._keep_canonical_daily_summaries(self.settings()["timezone"])
            self.db.execute("INSERT INTO schema_migrations(version) VALUES(16) ON CONFLICT DO NOTHING")
        self.db.commit()

    def _rebuild_sqlite_reminders(self):
        """Let place_id be empty (name-only reminders): SQLite needs a table rebuild for that."""
        self.db.commit()
        # Compare against violations that already existed, so an old orphan row
        # elsewhere cannot block the upgrade; only new breakage aborts it.
        existing = {tuple(row) for row in self.db.execute("PRAGMA foreign_key_check").fetchall()}
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.execute("BEGIN")
            self.db.execute(SQLITE_REMINDERS_TABLE.format(name="reminders_v13"))
            self.db.execute(f"INSERT INTO reminders_v13 ({REMINDER_COLUMNS}) SELECT {REMINDER_COLUMNS} FROM reminders")
            self.db.execute("DROP TABLE reminders")
            self.db.execute("ALTER TABLE reminders_v13 RENAME TO reminders")
            self.db.execute("CREATE INDEX IF NOT EXISTS reminders_active_created ON reminders(active,created_at)")
            if {tuple(row) for row in self.db.execute("PRAGMA foreign_key_check").fetchall()} - existing:
                raise RuntimeError("reminder rebuild would break foreign keys")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.db.close()

    def _missing_migrations(self, versions):
        applied = {row["version"] for row in self._rows("SELECT version FROM schema_migrations")}
        return [version for version in versions if version not in applied]

    def _rows(self, query, args=()):
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        if os.environ.get("GEOLOGS_TIMEZONE"):
            values["timezone"] = os.environ["GEOLOGS_TIMEZONE"]
        for row in self._rows("SELECT key,value FROM app_settings"):
            if row["key"] in values:
                try:
                    values[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    pass
        return values

    def save_settings(self, raw):
        current = self.settings()
        strategy = str(raw.get("speed_strategy", current["speed_strategy"]))
        if strategy not in ("reported", "calculated"):
            raise ValueError("speed_strategy must be reported or calculated")
        offset = float(raw.get("reported_speed_offset_kmh", current["reported_speed_offset_kmh"]))
        if not math.isfinite(offset) or not -100 <= offset <= 100:
            raise ValueError("reported_speed_offset_kmh must be between -100 and 100")
        timezone_name = str(raw.get("timezone", current["timezone"])).strip()
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone, such as America/Chicago") from exc
        provider = str(raw.get("places_provider", current["places_provider"]) or "").strip()
        if provider and provider not in PLACES_PROVIDERS:
            raise ValueError("places_provider must be one of: " + ", ".join(PLACES_PROVIDERS))
        if provider and provider != current["places_provider"] and not configured_places_providers()[provider]:
            raise ValueError(f"Set {PLACES_PROVIDERS[provider]['env']} to use {PLACES_PROVIDERS[provider]['label']}")
        stay_minutes = float(raw.get("stay_min_minutes", current["stay_min_minutes"]))
        if not stay_minutes.is_integer() or not 5 <= stay_minutes <= 120:
            raise ValueError("stay_min_minutes must be a whole number from 5 to 120")
        quick_minutes = float(raw.get("quick_stop_minutes", current["quick_stop_minutes"]))
        if not quick_minutes.is_integer() or not (quick_minutes == 0 or 2 <= quick_minutes < stay_minutes):
            raise ValueError("quick_stop_minutes must be 0 (off) or a whole number from 2 to below the minimum stay")
        speeds = {}
        for key in ("walking_max_kmh", "running_max_kmh", "cycling_max_kmh", "driving_min_kmh"):
            speeds[key] = float(raw.get(key, current[key]))
            if not math.isfinite(speeds[key]) or not 1 <= speeds[key] <= 200:
                raise ValueError(f"{key} must be between 1 and 200 km/h")
        if not speeds["walking_max_kmh"] < speeds["running_max_kmh"] < speeds["cycling_max_kmh"] <= speeds["driving_min_kmh"]:
            raise ValueError("speeds must rise from walking to running to cycling, and driving must be at least the cycling limit")
        schedule = str(raw.get("summary_schedule", current["summary_schedule"]))
        if schedule not in SUMMARY_SCHEDULES:
            raise ValueError("summary_schedule must be nightly or interval")
        interval = float(raw.get("summary_interval_hours", current["summary_interval_hours"]))
        if not interval.is_integer() or not 1 <= interval <= 12:
            raise ValueError("summary_interval_hours must be a whole number from 1 to 12")
        priority = raw.get("source_priority", current["source_priority"])
        if not isinstance(priority, list) or len(priority) > 100:
            raise ValueError("source_priority must be a list of at most 100 source names")
        priority = list(dict.fromkeys(str(name).strip()[:80] for name in priority if str(name).strip()))
        units = str(raw.get("units", current["units"]))
        if units not in UNIT_SYSTEMS:
            raise ValueError("units must be metric or imperial")
        currency = str(raw.get("currency", current["currency"]) or "").strip()
        if not currency or len(currency) > 5:
            raise ValueError("currency must be a symbol or code of 1 to 5 characters, such as $ or EUR")
        economy = raw.get("fuel_economy_l100km", current["fuel_economy_l100km"])
        if economy not in (None, ""):
            economy = float(economy)
            if not math.isfinite(economy) or not 1 <= economy <= 50:
                raise ValueError("fuel economy must be between 1 and 50 L/100 km (about 5 to 235 mpg)")
            economy = round(economy, 3)
        else:
            economy = None
        grade = str(raw.get("fuel_grade", current["fuel_grade"]))
        if grade not in FUEL_GRADES:
            raise ValueError("fuel_grade must be one of: " + ", ".join(FUEL_GRADES))
        price_source = str(raw.get("fuel_price_source", current["fuel_price_source"]))
        if price_source not in ("auto", "manual"):
            raise ValueError("fuel_price_source must be auto or manual")
        area = str(raw.get("fuel_area", current["fuel_area"]) or "").strip().upper()
        if area and area not in EIA_AREAS:
            raise ValueError("fuel_area must be one of: " + ", ".join(EIA_AREAS))
        values = {"speed_strategy": strategy, "reported_speed_offset_kmh": round(offset, 2),
                  "timezone": timezone_name, "places_provider": provider, "stay_min_minutes": int(stay_minutes),
                  "quick_stop_minutes": int(quick_minutes),
                  **{key: round(value, 1) for key, value in speeds.items()},
                  "summary_schedule": schedule, "summary_interval_hours": int(interval),
                  "source_priority": priority, "units": units, "currency": currency,
                  "fuel_economy_l100km": economy, "fuel_grade": grade, "fuel_price_source": price_source, "fuel_area": area}
        for key, value in values.items():
            self.db.execute("""INSERT INTO app_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, json.dumps(value)))
        if timezone_name != current["timezone"]:
            # Summaries are derived; days are rebuilt in the new zone when requested.
            self._keep_canonical_daily_summaries(timezone_name)
        if priority != current["source_priority"]:
            # Days combine sources at read time; drop what was combined in the old order.
            self.clear_analysis_cache(commit=False)
        if any(values[key] != current[key] for key in ANALYSIS_DEFAULTS):
            # Stays and their activities are stored; recalculate them under the new rules.
            # Stored summaries go stale through their watermark and are rebuilt when requested.
            from .analysis import process_stays
            process_stays(self, rebuild=True)
        self.db.commit()
        return values

    def _keep_canonical_daily_summaries(self, timezone_name):
        """Drop stored summaries for another timezone or a single source."""
        self.db.execute("DELETE FROM daily_summaries WHERE timezone<>? OR source<>''", (timezone_name,))

    def analysis_watermark(self, start=None, end=None, source=None):
        clauses, args = [], []
        if start:
            clauses.append("timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.db.execute("SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS max_id FROM point_observations" + where, args).fetchone()
        return f"{row['count']}:{row['max_id']}"

    def cached_analysis(self, cache_key, watermark):
        row = self.db.execute("SELECT value FROM analysis_cache WHERE cache_key=? AND watermark=?", (cache_key, watermark)).fetchone()
        return json.loads(row["value"]) if row else None

    def save_analysis(self, cache_key, watermark, value):
        self.db.execute("""INSERT INTO analysis_cache(cache_key,watermark,value,created_at) VALUES (?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET watermark=excluded.watermark,value=excluded.value,created_at=excluded.created_at""",
            (cache_key, watermark, json.dumps(value, separators=(",", ":")), parse_time(None)))
        self.db.commit()

    def clear_analysis_cache(self, commit=True):
        self.db.execute("DELETE FROM analysis_cache")
        if commit:
            self.db.commit()

    def daily_overview_watermark(self, start, end, source=None, home_context_start=None, home_context_end=None):
        clauses = ["e.triggered_at>=?", "e.triggered_at<=?"]
        args = [parse_time(start), parse_time(end)]
        if source:
            clauses.append("p.source=?")
            args.append(source)
        event_row = self.db.execute("""SELECT COUNT(*) AS count,COALESCE(MAX(e.id),0) AS max_id
            FROM events e JOIN points p ON p.id=e.point_id WHERE """ + " AND ".join(clauses), args).fetchone()
        places = self._rows("SELECT id,name,lat,lon,radius_m,address,category,provider_place_id FROM places ORDER BY id")
        reminders = self._rows("""SELECT id,text,place_id,active,created_at,last_triggered_at,cancelled_at
            FROM reminders WHERE (created_at>=? AND created_at<=?)
            OR (last_triggered_at>=? AND last_triggered_at<=?)
            OR (cancelled_at>=? AND cancelled_at<=?)
            OR (created_at<=? AND (last_triggered_at IS NULL OR last_triggered_at>?)
                AND (cancelled_at IS NULL OR cancelled_at>?)) ORDER BY id""",
            (parse_time(start), parse_time(end), parse_time(start), parse_time(end),
             parse_time(start), parse_time(end), parse_time(end), parse_time(end), parse_time(end)))
        labels = self._rows("SELECT id,name,lat,lon,radius_m,source,started_at,ended_at,updated_at FROM place_labels ORDER BY id")
        modes = self._rows("SELECT id,source,target,started_at,ended_at,mode FROM mode_overrides ORDER BY id")
        stay_edits = self._rows("SELECT id,source,action,started_at,ended_at FROM stay_edits ORDER BY id")
        # A daily record can also report an early-morning return Home. Include
        # that small cross-midnight context so a prior day's stored record goes
        # stale when the return is recorded after midnight.
        context_start = home_context_start or start
        context_end = home_context_end or end
        settings = self.settings()
        # Stay length and mode speeds change what a day contains.
        analysis = {key: settings[key] for key in ANALYSIS_DEFAULTS}
        from .fuel import fuel_signature
        value = ["stay-trips-v12", modes, stay_edits, analysis, settings["source_priority"], fuel_signature(self), self.analysis_watermark(start, end, source),
                 self.analysis_watermark(context_start, context_end, source),
                 event_row["count"], event_row["max_id"], places, reminders, labels]
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def stored_daily_overview(self, date, timezone_name, source=None, watermark=None):
        args = [date, timezone_name, source or ""]
        where = "summary_date=? AND timezone=? AND source=?"
        if watermark is not None:
            where += " AND watermark=?"
            args.append(watermark)
        row = self.db.execute("SELECT summary FROM daily_summaries WHERE " + where, args).fetchone()
        return json.loads(row["summary"]) if row else None

    def save_daily_overview(self, value, watermark, brief=None):
        now = parse_time(None)
        totals = value["totals"]
        self.db.execute("""INSERT INTO daily_summaries
            (summary_date,timezone,source,watermark,points,distance_km,trip_count,place_count,
             reminder_count,summary,brief,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(summary_date,timezone,source) DO UPDATE SET
            watermark=excluded.watermark,points=excluded.points,distance_km=excluded.distance_km,
            trip_count=excluded.trip_count,place_count=excluded.place_count,
            reminder_count=excluded.reminder_count,summary=excluded.summary,brief=excluded.brief,
            updated_at=excluded.updated_at""",
            (value["date"], value["timezone"], value.get("source") or "", watermark,
             totals["raw_fixes"], totals["distance_km"], totals["trips"], totals["places"],
             totals["reminders_triggered"], json.dumps(value, separators=(",", ":")),
             json.dumps(brief, separators=(",", ":"), ensure_ascii=False) if brief is not None else None, now, now))
        self.db.commit()
        return value

    def delete_daily_overview(self, date, timezone_name):
        self.db.execute("DELETE FROM daily_summaries WHERE summary_date=? AND timezone=? AND source=''", (date, timezone_name))
        self.db.commit()

    def daily_overviews(self, start=None, end=None, limit=90):
        # One record per date with data: the configured timezone across all sources.
        clauses, args = ["timezone=?", "source=''", "points>0"], [self.settings()["timezone"]]
        if start:
            clauses.append("summary_date>=?"); args.append(str(start))
        if end:
            clauses.append("summary_date<=?"); args.append(str(end))
        where = " WHERE " + " AND ".join(clauses)
        args.append(min(max(int(limit), 1), 1000))
        return self._rows("""SELECT summary_date AS date,timezone,NULLIF(source,'') AS source,
            points,distance_km,trip_count,place_count,reminder_count,created_at,updated_at
            FROM daily_summaries""" + where + " ORDER BY summary_date DESC,source LIMIT ?", args)

    def ingest(self, payload, trigger_reminders=True, process_analysis=True):
        items = payload if isinstance(payload, list) else [payload]
        if len(items) > 1000:
            raise ValueError("maximum 1000 points per request")
        if self.postgres:
            # Serialize each source's previous-fix and reminder decisions across
            # concurrent HTTP workers. Sorting prevents opposing batch lock order.
            for source in sorted({str(item.get("source") or "manual")[:80] for item in items}):
                self.db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (source,))
        speed_settings = self.settings()
        inserted, duplicate, recorded, events = 0, 0, 0, []
        for raw in items:
            lat = float(raw.get("lat", raw.get("latitude")))
            lon = float(raw.get("lon", raw.get("longitude")))
            if not math.isfinite(lat) or not -90 <= lat <= 90 or not math.isfinite(lon) or not -180 <= lon <= 180:
                raise ValueError("invalid latitude or longitude")
            source = str(raw.get("source") or "manual")[:80]
            timestamp = parse_time(raw.get("timestamp"))
            accuracy = raw.get("accuracy_m", raw.get("gps_accuracy"))
            accuracy = float(accuracy) if accuracy is not None else None
            if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
                raise ValueError("accuracy_m must be nonnegative")
            reported_speed = raw.get("speed_mps")
            reported_speed = float(reported_speed) if reported_speed is not None else None
            if reported_speed is not None and (not math.isfinite(reported_speed) or reported_speed < 0):
                raise ValueError("speed_mps must be nonnegative")
            use_reported = speed_settings["speed_strategy"] == "reported" and reported_speed is not None
            speed = max(0.0, reported_speed + speed_settings["reported_speed_offset_kmh"] / 3.6) if use_reported else None
            previous = self.db.execute("""SELECT p.* FROM source_state s JOIN points p ON p.id=s.point_id WHERE s.source=?""", (source,)).fetchone()
            origin = "reported_adjusted" if use_reported and speed_settings["reported_speed_offset_kmh"] else "reported" if use_reported else None
            if speed is None:
                prior_fix = self.db.execute("""SELECT timestamp,lat,lon,accuracy_m FROM point_observations
                    WHERE source=? AND timestamp<? ORDER BY timestamp DESC,id DESC LIMIT 1""", (source, timestamp)).fetchone()
                if prior_fix:
                    elapsed = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(prior_fix["timestamp"])).total_seconds()
                    if 0 < elapsed <= 900 and (accuracy is None or accuracy <= 150) and (prior_fix["accuracy_m"] is None or prior_fix["accuracy_m"] <= 150):
                        distance = metres(lat, lon, prior_fix["lat"], prior_fix["lon"])
                        if distance <= 1:
                            speed, origin = 0.0, "estimated"
                        elif elapsed >= 5 and distance / elapsed <= 55.6:
                            speed, origin = distance / elapsed, "estimated"
            mode = mode_for(speed, accuracy, speed_settings)
            external_id = raw.get("external_id")
            cursor = self.db.execute("""INSERT INTO points
                (source, external_id, timestamp, lat, lon, accuracy_m, speed_mps, speed_origin, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id""",
                (source, str(external_id) if external_id is not None else None, timestamp, lat, lon, accuracy, speed, origin, mode))
            inserted_row = cursor.fetchone()
            point_id = inserted_row["id"] if inserted_row else None
            if not inserted_row:
                existing = None
                if external_id is not None:
                    existing = self.db.execute("SELECT id FROM points WHERE source=? AND external_id=?", (source, str(external_id))).fetchone()
                if existing is None:
                    existing = self.db.execute("SELECT id FROM points WHERE source=? AND timestamp=? AND lat=? AND lon=?", (source, timestamp, lat, lon)).fetchone()
                if existing is None:
                    raise RuntimeError("conflicting point could not be found")
                point_id = existing["id"]
            legacy_retry = False
            if not inserted_row:
                legacy = self.db.execute("""SELECT timestamp,lat,lon,accuracy_m,speed_mps,speed_origin
                    FROM point_observations WHERE legacy_point_id=?""", (point_id,)).fetchone()
                legacy_retry = bool(legacy and legacy["timestamp"] == timestamp and legacy["lat"] == lat
                    and legacy["lon"] == lon and legacy["accuracy_m"] == accuracy
                    and (legacy["speed_mps"] == speed or
                         (speed is None and legacy["speed_origin"] in (None, "estimated"))))
            if not legacy_retry:
                observation = self.db.execute("""INSERT INTO point_observations
                    (point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,received_at,fingerprint)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO NOTHING""",
                    (point_id, source, timestamp, lat, lon, accuracy, speed, origin,
                     parse_time(None), hashlib.sha256(json.dumps(
                         [source, str(external_id) if external_id is not None else None, timestamp, lat, lon, accuracy, reported_speed],
                         separators=(",", ":")).encode()).hexdigest()))
                recorded += observation.rowcount
            if not inserted_row:
                duplicate += 1
                continue
            inserted += 1
            # Late imports are stored but do not rewind the live geofence state.
            if previous and timestamp <= previous["timestamp"]:
                continue
            self.db.execute("INSERT INTO source_state(source,point_id) VALUES (?,?) ON CONFLICT(source) DO UPDATE SET point_id=excluded.point_id", (source, point_id))
            if not trigger_reminders or abs((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()) > 1800:
                continue
            for reminder in self._rows("""SELECT r.*, p.lat AS place_lat, p.lon AS place_lon, p.radius_m,
                COALESCE(r.destination_name,p.name) AS place_name
                FROM reminders r JOIN places p ON p.id=r.place_id WHERE r.active=1"""):
                if accuracy is not None and accuracy > max(150, reminder["radius_m"]):
                    continue
                inside = metres(lat, lon, reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
                # A stale previous fix cannot prove the user remained inside.
                recent_previous = previous and (datetime.fromisoformat(timestamp) - datetime.fromisoformat(previous["timestamp"])).total_seconds() <= 1800
                was_inside = recent_previous and metres(previous["lat"], previous["lon"], reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
                if not inside or was_inside:
                    continue
                event_id = self.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?) RETURNING id", (reminder["id"], point_id, timestamp)).fetchone()["id"]
                self.db.execute("UPDATE reminders SET last_triggered_at=?,cancelled_at=NULL,active=0,repeat=0 WHERE id=?", (timestamp, reminder["id"]))
                events.append({"id": event_id, "reminder_id": reminder["id"], "text": reminder["text"], "place": reminder["place_name"], "triggered_at": timestamp})
        if recorded and process_analysis:
            from .analysis import check_name_reminders, process_stays
            ingested_sources = {str(item.get("source") or "manual")[:80] for item in items}
            process_stays(self, ingested_sources)
            if trigger_reminders:
                events += check_name_reminders(self, ingested_sources)
        self.db.commit()
        return {"inserted": inserted, "duplicates": duplicate, "recorded": recorded, "events": events}

    def points(self, start=None, end=None, source=None, limit=5000):
        clauses, args = [], []
        if start:
            clauses.append("timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        suffix = ""
        if limit is not None:
            args.append(min(max(int(limit), 1), 20000))
            suffix = " LIMIT ?"
        return self._rows("SELECT id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode FROM points" + where + " ORDER BY timestamp DESC" + suffix, args)

    def observations(self, start=None, end=None, source=None, limit=5000, offset=0):
        clauses, args = [], []
        if start:
            clauses.append("o.timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("o.timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("o.source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        suffix = ""
        if limit is not None:
            args.extend((min(max(int(limit), 1), 20000), min(max(int(offset), 0), 10_000_000)))
            suffix = " LIMIT ? OFFSET ?"
        rows = self._rows("""SELECT o.id,o.point_id,p.external_id,o.source,o.timestamp,o.lat,o.lon,o.accuracy_m,
            o.speed_mps,o.speed_origin,
            o.received_at FROM point_observations o JOIN points p ON p.id=o.point_id""" + where + " ORDER BY o.timestamp DESC,COALESCE(o.accuracy_m,1000000000) ASC,o.id DESC" + suffix, args)
        speeds = self.settings()
        for row in rows:
            row["mode"] = mode_for(row["speed_mps"], row["accuracy_m"], speeds)
        return rows

    def timeline(self, start=None, end=None, source=None, limit=5000):
        """Compact informational fixes: movement plus stop arrival/departure edges."""
        clauses, args = [], []
        if start:
            clauses.append("o.timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("o.timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("o.source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        suffix = ""
        if limit is not None:
            args.append(min(max(int(limit), 1), 20000))
            suffix = " LIMIT ?"
        rows = self._rows("""SELECT o.id,o.point_id,p.external_id,o.source,o.timestamp,o.lat,o.lon,
            o.accuracy_m,o.speed_mps,o.speed_origin,o.received_at,t.role,t.stay_id
            FROM timeline_fixes t JOIN point_observations o ON o.id=t.observation_id
            JOIN points p ON p.id=o.point_id""" + where + " ORDER BY o.timestamp DESC,o.id DESC" + suffix, args)
        speeds = self.settings()
        for row in rows:
            row["mode"] = mode_for(row["speed_mps"], row["accuracy_m"], speeds)
        return rows

    def sample_deletion(self, ids):
        """What deleting these raw samples (point_observations ids) would remove.

        A canonical point goes too once none of its samples are left, and with
        it any reminder trigger it caused.
        """
        try:
            ids = sorted({int(value) for value in ids})
        except (TypeError, ValueError) as exc:
            raise ValueError("ids must be a list of sample ids") from exc
        if not 1 <= len(ids) <= MAX_SAMPLE_DELETE:
            raise ValueError(f"choose between 1 and {MAX_SAMPLE_DELETE} samples")
        marks = ",".join("?" * len(ids))
        rows = self._rows(f"SELECT id,point_id,source,timestamp FROM point_observations WHERE id IN ({marks})", ids)
        if not rows:
            raise ValueError("those samples no longer exist")
        point_ids = sorted({row["point_id"] for row in rows})
        point_marks = ",".join("?" * len(point_ids))
        # Points that keep another sample stay; the rest are removed with their samples.
        kept = {row["point_id"] for row in self._rows(f"""SELECT DISTINCT point_id FROM point_observations
            WHERE point_id IN ({point_marks}) AND id NOT IN ({marks})""", point_ids + ids)}
        orphans = [point_id for point_id in point_ids if point_id not in kept]
        triggers = self._rows(f"""SELECT e.id,e.triggered_at,r.text AS reminder FROM events e
            JOIN reminders r ON r.id=e.reminder_id WHERE e.point_id IN ({",".join("?" * len(orphans))})
            ORDER BY e.triggered_at""", orphans) if orphans else []
        timestamps = sorted(row["timestamp"] for row in rows)
        return {"ids": [row["id"] for row in rows], "samples": len(rows), "points": orphans,
                "sources": sorted({row["source"] for row in rows}), "first": timestamps[0], "last": timestamps[-1],
                "timestamps": timestamps, "reminder_triggers": triggers}

    def source_deletion(self, source):
        """What removing a tracker would remove: all its samples, and the reminder triggers they caused."""
        row = self.db.execute("""SELECT COUNT(*) AS samples,MIN(timestamp) AS first,MAX(timestamp) AS last
            FROM point_observations WHERE source=?""", (source,)).fetchone()
        if not row["samples"]:
            raise ValueError("that tracker has no samples")
        triggers = self._rows("""SELECT e.triggered_at,r.text AS reminder FROM events e JOIN reminders r ON r.id=e.reminder_id
            JOIN points p ON p.id=e.point_id WHERE p.source=? ORDER BY e.triggered_at""", (source,))
        return {"source": source, "samples": row["samples"], "first": row["first"], "last": row["last"],
                "reminder_triggers": triggers}

    def delete_source(self, source):
        """Remove a tracker and everything derived from it, without committing."""
        for table in ("timeline_fixes", "observed_stays_v2", "stay_observation_state", "stay_edits", "mode_overrides"):
            self.db.execute(f"DELETE FROM {table} WHERE source=?", (source,))
        self.db.execute("DELETE FROM place_labels WHERE source=? AND started_at IS NOT NULL", (source,))
        self.db.execute("DELETE FROM events WHERE point_id IN (SELECT id FROM points WHERE source=?)", (source,))
        self.db.execute("DELETE FROM observed_stays WHERE source=?", (source,))
        self.db.execute("DELETE FROM point_observations WHERE source=?", (source,))
        self.db.execute("DELETE FROM source_state WHERE source=?", (source,))
        self.db.execute("DELETE FROM points WHERE source=?", (source,))
        priority = self.settings()["source_priority"]
        if source in priority:
            self.db.execute("UPDATE app_settings SET value=? WHERE key='source_priority'",
                            (json.dumps([name for name in priority if name != source]),))
        self.clear_analysis_cache(commit=False)

    def ingest_tokens(self):
        """Device tokens, newest first. The tokens themselves are never stored, only their hashes."""
        return self._rows("""SELECT id,name,hint,source,created_at,last_used_at,last_source
            FROM ingest_tokens ORDER BY created_at DESC,id DESC""")

    def create_ingest_token(self, raw):
        """A token one device sends locations with, shown once.

        `source` locks it to one tracker name, or to every tracker of a kind
        when it ends with ":" (such as "home_assistant:").
        """
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 60:
            raise ValueError("name is required (max 60 characters)")
        source = str(raw.get("source") or "").strip() or None
        if source and len(source) > 80:
            raise ValueError("source must be at most 80 characters")
        token = "glp_" + secrets.token_urlsafe(24)
        row = self.db.execute("""INSERT INTO ingest_tokens(name,token_hash,hint,source,created_at)
            VALUES (?,?,?,?,?) RETURNING id""", (name, ingest_token_hash(token), token[:8], source, parse_time(None))).fetchone()
        self.db.commit()
        return {**next(item for item in self.ingest_tokens() if item["id"] == row["id"]), "token": token}

    def delete_ingest_token(self, id_):
        result = self.db.execute("DELETE FROM ingest_tokens WHERE id=?", (int(id_),))
        if not result.rowcount:
            raise ValueError("that token was already revoked")
        self.db.commit()
        return {"deleted": int(id_)}

    def ingest_token_for(self, token):
        """The stored device token a request presented, or None."""
        if not token or not token.startswith("glp_"):
            return None
        row = self.db.execute("SELECT id,name,source FROM ingest_tokens WHERE token_hash=?",
                              (ingest_token_hash(token),)).fetchone()
        return dict(row) if row else None

    def has_ingest_tokens(self):
        return self.db.execute("SELECT 1 FROM ingest_tokens LIMIT 1").fetchone() is not None

    def note_ingest_token_use(self, id_, source):
        self.db.execute("UPDATE ingest_tokens SET last_used_at=?,last_source=? WHERE id=?", (parse_time(None), source, int(id_)))
        self.db.commit()

    def delete_ingest_device(self, kind, device):
        """Forget a tracker app's device status row (a failed or retired phone); its samples stay."""
        result = self.db.execute("DELETE FROM ingest_devices WHERE kind=? AND device=?", (str(kind), str(device or "")))
        if not result.rowcount:
            raise ValueError("that device is already gone")
        self.db.commit()
        return {"deleted": True}

    def delete_samples(self, plan):
        """Remove the samples in a sample_deletion() plan without committing.

        Stays of the affected sources reference raw samples, so they are
        dropped here and must be rebuilt (process_stays) before committing.
        """
        ids, orphans, sources = plan["ids"], plan["points"], plan["sources"]
        source_marks = ",".join("?" * len(sources))
        self.db.execute(f"DELETE FROM timeline_fixes WHERE source IN ({source_marks})", sources)
        self.db.execute(f"DELETE FROM observed_stays_v2 WHERE source IN ({source_marks})", sources)
        self.db.execute(f"DELETE FROM stay_observation_state WHERE source IN ({source_marks})", sources)
        self.db.execute(f"DELETE FROM point_observations WHERE id IN ({','.join('?' * len(ids))})", ids)
        if orphans:
            marks = ",".join("?" * len(orphans))
            self.db.execute(f"DELETE FROM events WHERE point_id IN ({marks})", orphans)
            self.db.execute(f"DELETE FROM observed_stays WHERE start_point_id IN ({marks}) OR end_point_id IN ({marks})",
                            orphans + orphans)
            # A source's latest point moves back to the newest one left.
            for row in self._rows(f"SELECT source FROM source_state WHERE point_id IN ({marks})", orphans):
                latest = self.db.execute(f"""SELECT id FROM points WHERE source=? AND id NOT IN ({marks})
                    ORDER BY timestamp DESC,id DESC LIMIT 1""", [row["source"], *orphans]).fetchone()
                if latest:
                    self.db.execute("UPDATE source_state SET point_id=? WHERE source=?", (latest["id"], row["source"]))
                else:
                    self.db.execute("DELETE FROM source_state WHERE source=?", (row["source"],))
            self.db.execute(f"DELETE FROM points WHERE id IN ({marks})", orphans)

    def observation_count(self, start=None, end=None, source=None):
        clauses, args = [], []
        if start:
            clauses.append("timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.db.execute("SELECT COUNT(*) AS n FROM point_observations" + where, args).fetchone()["n"]

    def latest(self):
        """Your latest position: the newest fix, unless a tracker ranked above it
        (Settings) reported within PRIORITY_FRESH_SECONDS of it."""
        rows = self.observations(limit=1)
        if not rows:
            return None
        point = rows[0]
        newest = datetime.fromisoformat(point["timestamp"])
        for source in self.settings()["source_priority"]:
            if source == point["source"]:
                break
            preferred = self.observations(source=source, limit=1)
            if preferred and (newest - datetime.fromisoformat(preferred[0]["timestamp"])).total_seconds() <= PRIORITY_FRESH_SECONDS:
                point = preferred[0]
                break
        point["id"] = point["point_id"]
        point["age_seconds"] = max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(point["timestamp"])).total_seconds()))
        point["stale"] = point["age_seconds"] > 900
        return point

    def at_time(self, timestamp):
        at = parse_time(timestamp)
        columns = "id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode"
        if self.postgres:
            rows = self._rows(f"SELECT {columns}, ABS(EXTRACT(EPOCH FROM timestamp::timestamptz - ?::timestamptz))::double precision AS seconds_away FROM points ORDER BY seconds_away LIMIT 1", (at,))
        else:
            rows = self._rows(f"SELECT {columns}, ABS(strftime('%s',timestamp)-strftime('%s',?)) AS seconds_away FROM points ORDER BY seconds_away LIMIT 1", (at,))
        return rows[0] if rows else None

    def places(self, enrich=False):
        rows = self._rows("""SELECT id,name,lat,lon,radius_m,address,category,provider_place_id
            FROM places WHERE COALESCE(saved,1)=1 ORDER BY name""")
        if enrich:
            changed = False
            for place in rows:
                if place["address"] and place["category"] and place["provider_place_id"]:
                    continue
                found = place_for_coordinate(self, place["lat"], place["lon"])
                address = place["address"] or found.get("address") or ""
                category = place["category"] or found.get("category") or ""
                provider_place_id = place["provider_place_id"] or found.get("provider_place_id") or ""
                if (address, category, provider_place_id) != (place["address"] or "", place["category"] or "", place["provider_place_id"] or ""):
                    place.update({"address": address, "category": category, "provider_place_id": provider_place_id})
                    self.db.execute("UPDATE places SET address=?,category=?,provider_place_id=? WHERE id=?",
                                    (address, category, provider_place_id, place["id"]))
                    changed = True
            if changed:
                self.db.commit()
        return rows

    def save_place(self, raw):
        name = str(raw.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("name is required (max 120 characters)")
        lat, lon = float(raw["lat"]), float(raw["lon"])
        radius = float(raw.get("radius_m", 150))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 5000):
            raise ValueError("invalid place coordinates or radius (20–5000 m)")
        existing = self.db.execute("SELECT address,category,provider_place_id FROM places WHERE id=?", (int(raw["id"]),)).fetchone() if raw.get("id") else None
        address = str(raw.get("address") or (existing["address"] if existing else "") or "")[:300]
        category = str(raw.get("category") or (existing["category"] if existing else "") or "")[:160]
        provider_place_id = str(raw.get("provider_place_id") or (existing["provider_place_id"] if existing else "") or "")[:300]
        if not address or not category or not provider_place_id:
            found = place_for_coordinate(self, lat, lon)
            address = address or found.get("address") or ""
            category = category or found.get("category") or ""
            provider_place_id = provider_place_id or found.get("provider_place_id") or ""
        if raw.get("id"):
            updated = self.db.execute("UPDATE places SET name=?,lat=?,lon=?,radius_m=?,address=?,category=?,provider_place_id=?,saved=1 WHERE id=?",
                                      (name, lat, lon, radius, address, category, provider_place_id, int(raw["id"])))
            if not updated.rowcount:
                raise ValueError("place not found")
            id_ = int(raw["id"])
        else:
            id_ = self.db.execute("INSERT INTO places(name,lat,lon,radius_m,address,category,provider_place_id,saved) VALUES (?,?,?,?,?,?,?,1) RETURNING id",
                                  (name, lat, lon, radius, address, category, provider_place_id)).fetchone()["id"]
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return self._rows("SELECT id,name,lat,lon,radius_m,address,category,provider_place_id FROM places WHERE id=?", (id_,))[0]

    def delete_place(self, id_):
        """Remove a saved place from the list and from naming visits.

        A place that a reminder points at is hidden instead, so the reminder
        keeps its destination and its history stays readable.
        """
        place = self.db.execute("SELECT id FROM places WHERE id=? AND COALESCE(saved,1)=1", (int(id_),)).fetchone()
        if not place:
            raise ValueError("saved place not found")
        if self.db.execute("SELECT 1 FROM reminders WHERE place_id=? LIMIT 1", (int(id_),)).fetchone():
            # Names are unique; the hidden row must not block reusing this one.
            self.db.execute("UPDATE places SET saved=0,name=? WHERE id=?", ("Deleted place " + os.urandom(8).hex(), int(id_)))
        else:
            self.db.execute("DELETE FROM places WHERE id=?", (int(id_),))
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return {"deleted": int(id_)}

    def place_labels(self):
        """Name corrections. Without a time window one names every visit to its spot;
        with one (and usually a source) it names only the visit it was made for."""
        return self._rows("""SELECT id,name,lat,lon,radius_m,address,category,provider_place_id,source,started_at,ended_at,
            created_at,updated_at FROM place_labels ORDER BY name,id""")

    def save_place_label(self, raw):
        """Pin a name to a position, overriding saved places and provider lookups there.

        With started_at and ended_at it applies only to stays overlapping that
        window (from `source`, when given): one visit, untouched by later ones.
        Without them it names every visit to the spot, past and future.
        Correcting the same visit, or the same spot, again replaces the label.
        """
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ValueError("name is required (max 120 characters)")
        lat, lon = float(raw["lat"]), float(raw["lon"])
        radius = float(raw.get("radius_m") or 50)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 1000):
            raise ValueError("invalid label coordinates or radius (20–1000 m)")
        if bool(raw.get("started_at")) != bool(raw.get("ended_at")):
            raise ValueError("started_at and ended_at must be given together")
        started_at = parse_time(raw["started_at"]) if raw.get("started_at") else None
        ended_at = parse_time(raw["ended_at"]) if raw.get("ended_at") else None
        if started_at and ended_at < started_at:
            raise ValueError("ended_at must not be before started_at")
        source = str(raw.get("source") or "").strip()[:80] or None if started_at else None
        values = (name, lat, lon, radius, str(raw.get("address") or "")[:300], str(raw.get("category") or "")[:160],
                  str(raw.get("provider_place_id") or "")[:300], source, started_at, ended_at, parse_time(None))
        def same_correction(label):
            if metres(lat, lon, label["lat"], label["lon"]) > 25 or bool(label["started_at"]) != bool(started_at):
                return False
            return not started_at or (label["source"] == source and label["started_at"] <= ended_at and label["ended_at"] >= started_at)
        target = int(raw["id"]) if raw.get("id") else next(
            (label["id"] for label in self.place_labels() if same_correction(label)), None)
        if target is not None:
            updated = self.db.execute("""UPDATE place_labels SET name=?,lat=?,lon=?,radius_m=?,address=?,category=?,
                provider_place_id=?,source=?,started_at=?,ended_at=?,updated_at=? WHERE id=?""", (*values, target))
            if not updated.rowcount:
                raise ValueError("label not found")
        else:
            target = self.db.execute("""INSERT INTO place_labels
                (name,lat,lon,radius_m,address,category,provider_place_id,source,started_at,ended_at,updated_at,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""", (*values, values[-1])).fetchone()["id"]
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return next(label for label in self.place_labels() if label["id"] == target)

    def record_ingest(self, kind, status, message=None, device=None, accepted=0):
        """Remember the latest request from each device of each tracker app, so Settings
        can say whether a phone is getting through and, when it is not, why. `device` is
        the source the request names, or empty when it cannot be told apart."""
        now = parse_time(None)
        ok = 200 <= status < 300
        self.db.execute("""INSERT INTO ingest_devices
            (kind,device,last_attempt_at,last_status,last_message,last_success_at,received)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,device) DO UPDATE SET
            last_attempt_at=excluded.last_attempt_at,last_status=excluded.last_status,
            last_message=excluded.last_message,
            last_success_at=COALESCE(excluded.last_success_at,ingest_devices.last_success_at),
            received=ingest_devices.received+excluded.received""",
            (kind, (device or "")[:80], now, int(status), (message or "")[:300] or None, now if ok else None, int(accepted)))
        self.db.commit()

    def source_activity(self, now=None):
        """Every source that has sent locations: how much, the newest fix, and when it last arrived."""
        day_ago = parse_time((now or datetime.now(timezone.utc)).timestamp() - 86400)
        sources = self._rows("""SELECT o.source,o.fixes,o.first_fix,o.last_fix,r.received_at AS last_received,
            (SELECT COUNT(*) FROM point_observations x WHERE x.source=o.source AND x.timestamp>=?) AS fixes_24h
            FROM (SELECT source,COUNT(*) AS fixes,MIN(timestamp) AS first_fix,MAX(timestamp) AS last_fix,MAX(id) AS last_id
                  FROM point_observations GROUP BY source) o
            JOIN point_observations r ON r.id=o.last_id ORDER BY r.received_at DESC""", (day_ago,))
        return {"sources": sources, "devices": self._rows("SELECT * FROM ingest_devices ORDER BY last_attempt_at DESC")}

    def mode_overrides(self):
        """Travel mode corrections, each for one trip or area stay identified by source and time."""
        return self._rows("SELECT id,source,target,started_at,ended_at,mode,created_at,updated_at FROM mode_overrides ORDER BY started_at,id")

    def save_mode_override(self, raw):
        """Set (or with mode "auto", clear) the travel mode of one trip or area stay.

        Trips are recalculated from raw points, so a correction is keyed by
        source and time window, not an ID. Setting it again for the same trip
        replaces it.
        """
        source = str(raw.get("source") or "").strip()[:80]
        target = str(raw.get("target") or "trip")
        mode = str(raw.get("mode") or "").strip()
        if not source:
            raise ValueError("source is required")
        if target not in ("trip", "stay"):
            raise ValueError("target must be trip or stay")
        if mode != "auto" and mode not in MOVEMENT_MODES:
            raise ValueError("mode must be one of: auto, " + ", ".join(MOVEMENT_MODES))
        started_at, ended_at = parse_time(raw["started_at"]), parse_time(raw["ended_at"])
        if ended_at <= started_at:
            raise ValueError("ended_at must be after started_at")
        existing = mode_override_for(self.mode_overrides(), source, target, started_at, ended_at)
        if mode == "auto":
            if existing:
                self.db.execute("DELETE FROM mode_overrides WHERE id=?", (existing["id"],))
            result = None
        elif existing:
            self.db.execute("UPDATE mode_overrides SET started_at=?,ended_at=?,mode=?,updated_at=? WHERE id=?",
                            (started_at, ended_at, mode, parse_time(None), existing["id"]))
            result = existing["id"]
        else:
            result = self.db.execute("""INSERT INTO mode_overrides(source,target,started_at,ended_at,mode,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?) RETURNING id""", (source, target, started_at, ended_at, mode, parse_time(None), parse_time(None))).fetchone()["id"]
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return next((item for item in self.mode_overrides() if item["id"] == result), {"source": source, "target": target, "mode": "auto"})

    def stay_edits(self, start=None, end=None, source=None):
        """Stays you added or removed, each kept by source and time window.

        Detected stays are rebuilt from raw samples, so an edit names a window
        rather than a stay id and is applied whenever stays are read.
        """
        clauses, args = [], []
        if start:
            clauses.append("ended_at>=?"); args.append(parse_time(start))
        if end:
            clauses.append("started_at<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self._rows("SELECT id,source,action,started_at,ended_at,lat,lon,created_at FROM stay_edits"
                          + where + " ORDER BY started_at,id", args)

    def save_stay_edit(self, raw):
        """Add a stop the detector missed, or remove a stay it should not have found.

        An added stop sits at the median position of its source's accurate
        samples in the window. Editing the same window again replaces the edit.
        """
        source = str(raw.get("source") or "").strip()[:80]
        action = str(raw.get("action") or "")
        if not source:
            raise ValueError("source is required")
        if action not in ("add", "remove"):
            raise ValueError("action must be add or remove")
        started_at, ended_at = parse_time(raw.get("started_at")), parse_time(raw.get("ended_at"))
        seconds = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds()
        if not 0 < seconds <= 86400:
            raise ValueError("a stop must end after it starts, within a day")
        lat = lon = None
        if action == "add":
            fixes = self._rows("""SELECT lat,lon FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND (accuracy_m IS NULL OR accuracy_m<=100)""", (source, started_at, ended_at))
            if not fixes:
                raise ValueError("no accurate samples from that device in that time; pick a window with samples")
            lat = sorted(fix["lat"] for fix in fixes)[len(fixes) // 2]
            lon = sorted(fix["lon"] for fix in fixes)[len(fixes) // 2]
        for edit in self.stay_edits(started_at, ended_at, source):
            if edit["action"] == action and windows_overlap(edit, started_at, ended_at):
                self.db.execute("DELETE FROM stay_edits WHERE id=?", (edit["id"],))
        result = self.db.execute("""INSERT INTO stay_edits(source,action,started_at,ended_at,lat,lon,created_at)
            VALUES (?,?,?,?,?,?,?) RETURNING id""", (source, action, started_at, ended_at, lat, lon, parse_time(None))).fetchone()["id"]
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return next(edit for edit in self.stay_edits(source=source) if edit["id"] == result)

    def delete_stay_edit(self, id_):
        """Undo an added or removed stop."""
        result = self.db.execute("DELETE FROM stay_edits WHERE id=?", (int(id_),))
        if not result.rowcount:
            raise ValueError("that change was already undone")
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return {"deleted": int(id_)}

    def delete_place_label(self, id_):
        result = self.db.execute("DELETE FROM place_labels WHERE id=?", (int(id_),))
        if not result.rowcount:
            raise ValueError("label not found")
        self.db.execute("DELETE FROM analysis_cache")
        self.db.commit()
        return {"deleted": int(id_)}

    def reminders(self):
        """Active one-shot reminders waiting for their next arrival."""
        return self._rows("""SELECT r.*,COALESCE(r.destination_name,p.name) AS place_name,
            p.lat AS place_lat,p.lon AS place_lon,p.radius_m,
            COALESCE(r.destination_address,p.address) AS place_address,
            COALESCE(r.destination_category,p.category) AS category
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.active=1 ORDER BY r.created_at DESC,r.id DESC""")

    def reminder_history(self, limit=200):
        return self._rows("""SELECT r.*,COALESCE(r.destination_name,p.name) AS place_name,
            COALESCE(r.destination_category,p.category) AS category,e.id AS event_id,e.triggered_at,e.webhook_status,
            CASE WHEN r.last_triggered_at IS NULL THEN 'cancelled' ELSE 'triggered' END AS status
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            LEFT JOIN events e ON e.id=(SELECT MAX(latest.id) FROM events latest WHERE latest.reminder_id=r.id)
            WHERE r.active=0 ORDER BY COALESCE(r.last_triggered_at,r.cancelled_at,r.created_at) DESC,r.id DESC LIMIT ?""",
            (min(max(int(limit), 1), 1000),))

    def create_reminder(self, raw):
        message = str(raw.get("text") or "").strip()
        if not message or len(message) > 500:
            raise ValueError("text is required (max 500 characters)")
        match_name = str(raw.get("match_name") or "").strip()
        if match_name:
            # Name-only: fires at the first stop near any place with a matching name.
            if len(match_name) > 120 or not search_text(match_name):
                raise ValueError("match_name must contain letters or digits (max 120 characters)")
            place_id = None
            destination = {"name": match_name, "address": "", "category": "", "provider_place_id": ""}
        elif raw.get("place_id") not in (None, ""):
            place_id = int(raw["place_id"])
            place = self.db.execute("SELECT * FROM places WHERE id=?", (place_id,)).fetchone()
            if not place:
                raise ValueError("place not found")
            destination = {"name": place["name"], "address": place["address"] or "",
                           "category": place["category"] or "", "provider_place_id": place["provider_place_id"] or ""}
        else:
            destination_name = str(raw.get("place_name") or "").strip()
            lat, lon = raw.get("lat"), raw.get("lon")
            if (lat is None or lon is None) and destination_name:
                matches = geocode(self, destination_name, resolve=1)
                if not matches:
                    raise ValueError("place name was not found; provide coordinates")
                match = matches[0]
                lat, lon = match["lat"], match["lon"]
                raw = {**match, **raw, "lat": lat, "lon": lon}
            if lat is None or lon is None:
                raise ValueError("place_id or destination coordinates are required")
            lat, lon = float(lat), float(lon)
            radius = float(raw.get("radius_m", 150))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 5000):
                raise ValueError("invalid destination coordinates or radius (20–5000 m)")
            found = place_for_coordinate(self, lat, lon)
            destination = {
                "name": destination_name or str(raw.get("name") or found.get("name") or f"{lat:.5f}, {lon:.5f}").strip(),
                "address": str(raw.get("address") or found.get("address") or "")[:300],
                "category": str(raw.get("category") or found.get("category") or "")[:160],
                "provider_place_id": str(raw.get("provider_place_id") or found.get("provider_place_id") or "")[:300],
            }
            if not destination["name"] or len(destination["name"]) > 120:
                raise ValueError("place_name is required (max 120 characters)")
            internal_name = "Reminder destination " + os.urandom(8).hex()
            place_id = self.db.execute("""INSERT INTO places
                (name,lat,lon,radius_m,address,category,provider_place_id,saved)
                VALUES (?,?,?,?,?,?,?,0) RETURNING id""",
                (internal_name, lat, lon, radius, destination["address"], destination["category"],
                 destination["provider_place_id"])).fetchone()["id"]
        id_ = self.db.execute("""INSERT INTO reminders
            (text,place_id,repeat,cooldown_minutes,created_at,destination_name,destination_address,
             destination_category,destination_provider_place_id,match_name)
            VALUES (?,?,0,1440,?,?,?,?,?,?) RETURNING id""",
            (message, place_id, parse_time(None), destination["name"], destination["address"],
             destination["category"], destination["provider_place_id"], match_name or None)).fetchone()["id"]
        self.db.commit()
        return next(item for item in self.reminders() if item["id"] == id_)

    def cancel_reminder(self, id_):
        result = self.db.execute("UPDATE reminders SET active=0,cancelled_at=? WHERE id=? AND active=1", (parse_time(None), int(id_)))
        self.db.commit()
        if not result.rowcount:
            raise ValueError("active reminder not found")
        return {"updated": result.rowcount}

    def events(self, since_id=0, limit=100):
        return self._rows("""SELECT e.*,r.text,COALESCE(r.destination_name,p.name) AS place_name FROM events e
            JOIN reminders r ON r.id=e.reminder_id LEFT JOIN places p ON p.id=r.place_id
            WHERE e.id>? ORDER BY e.id DESC LIMIT ?""", (int(since_id), min(int(limit), 500)))

    def reminder_events(self, start, end, source=None):
        clauses = ["e.triggered_at>=?", "e.triggered_at<=?"]
        args = [parse_time(start), parse_time(end)]
        if source:
            clauses.append("pnt.source=?")
            args.append(source)
        return self._rows("""SELECT e.id,e.reminder_id,e.triggered_at,e.webhook_status,
            r.text,COALESCE(r.destination_name,p.name) AS place_name,r.match_name FROM events e JOIN reminders r ON r.id=e.reminder_id
            LEFT JOIN places p ON p.id=r.place_id JOIN points pnt ON pnt.id=e.point_id WHERE """
            + " AND ".join(clauses) + " ORDER BY e.triggered_at", args)

    def reminders_created(self, start, end):
        return self._rows("""SELECT r.id,r.text,r.created_at,r.active,r.last_triggered_at,
            COALESCE(r.destination_name,p.name) AS place_name,r.match_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.created_at>=? AND r.created_at<=? ORDER BY r.created_at""",
            (parse_time(start), parse_time(end)))

    def reminders_cancelled(self, start, end):
        return self._rows("""SELECT r.id,r.text,r.cancelled_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.cancelled_at>=? AND r.cancelled_at<=? ORDER BY r.cancelled_at""",
            (parse_time(start), parse_time(end)))

    def reminders_active_at(self, timestamp):
        at = parse_time(timestamp)
        return self._rows("""SELECT r.id,r.text,r.created_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id WHERE r.created_at<=?
            AND (r.last_triggered_at IS NULL OR r.last_triggered_at>?)
            AND (r.cancelled_at IS NULL OR r.cancelled_at>?) ORDER BY r.created_at""", (at, at, at))

    def stats(self, start=None, end=None, source=None):
        raw = list(reversed(self.observations(start, end, source=source, limit=None)))
        selected = list(reversed(self.timeline(start, end, source=source, limit=None)))
        result = stats_from_points(selected)
        result["points"] = len(raw)
        result["unique_points"] = len(selected)
        result["timeline_points"] = len(selected)
        return result

    def trips(self, start=None, end=None, source=None):
        """Travel between consecutive confirmed stays. Estimates are based on recorded samples."""
        from .analysis import anchored_trips
        return anchored_trips(self, start, end, source)

    def export_geojson(self, start=None, end=None):
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": {k: v for k, v in p.items() if k not in ("lat", "lon")}} for p in self.observations(start, end, limit=None)]}

    def import_csv(self, content, source="csv"):
        reader = csv.DictReader(io.StringIO(content) if isinstance(content, str) else content)
        batch = []
        totals = {"inserted": 0, "duplicates": 0, "events": []}
        for row in reader:
            row["source"] = source
            for optional in ("accuracy_m", "speed_mps", "external_id"):
                if row.get(optional) == "":
                    row.pop(optional)
            batch.append(row)
            if len(batch) == 1000:
                result = self.ingest(batch, trigger_reminders=False, process_analysis=False)
                totals["inserted"] += result["inserted"]
                totals["duplicates"] += result["duplicates"]
                batch = []
        if batch:
            result = self.ingest(batch, trigger_reminders=False, process_analysis=False)
            totals["inserted"] += result["inserted"]
            totals["duplicates"] += result["duplicates"]
        if totals["inserted"]:
            from .analysis import process_stays
            process_stays(self, {source}, rebuild=True)
            self.db.commit()
        return totals

    def place_visits(self, place_id, start=None, end=None):
        place = self.db.execute("SELECT id,name,lat,lon,radius_m,address FROM places WHERE id=?", (int(place_id),)).fetchone()
        if not place:
            raise ValueError("place not found")
        from .analysis import merge_split_visits, stays
        # Merge over every stay so each went_elsewhere flag meets the stay it refers to.
        # A saved place is a zone: a visit is time anywhere inside it, across its buildings.
        merged = merge_split_visits(list(reversed(stays(self, start, end, limit=None))), lambda s: s["zone_id"])
        visits = [{"arrival": s["started_at"], "departure": s["ended_at"],
                   "observed_minutes": s["observed_minutes"], "points": s["point_count"],
                   "ongoing": bool(s["ongoing"]), "source": s["source"],
                   "kind": s["kind"], "activity": s["activity"]}
                  for s in reversed(merged) if s["zone_id"] == int(place_id)]
        return {"place": dict(place), "visits": visits, "total_observed_minutes": round(sum(v["observed_minutes"] for v in visits), 1)}

    def near(self, lat, lon, radius_m=250, start=None, end=None, limit=100, source=None):
        lat, lon, radius_m = float(lat), float(lon), float(radius_m)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 1 <= radius_m <= 100000):
            raise ValueError("invalid coordinates or radius")
        if self.postgres:
            clauses = ["ST_DWithin(position, ST_SetSRID(ST_MakePoint(?,?),4326)::geography, ?)"]
            where_args = [lon, lat, radius_m]
            if start:
                clauses.append("timestamp>=?"); where_args.append(parse_time(start))
            if end:
                clauses.append("timestamp<=?"); where_args.append(parse_time(end))
            if source:
                clauses.append("source=?"); where_args.append(source)
            return self._rows("SELECT id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode, "
                              "ROUND(ST_Distance(position,ST_SetSRID(ST_MakePoint(?,?),4326)::geography)) AS distance_m "
                              "FROM points WHERE " + " AND ".join(clauses) + " ORDER BY timestamp DESC LIMIT ?",
                              [lon, lat] + where_args + [min(int(limit), 500)])
        found = []
        for point in self.points(start, end, source=source, limit=None):
            distance = metres(lat, lon, point["lat"], point["lon"])
            if distance <= radius_m:
                found.append({**point, "distance_m": round(distance)})
                if len(found) >= min(int(limit), 500):
                    break
        return found

    def coverage(self):
        sources = self._rows("SELECT source,COUNT(*) AS points,MIN(timestamp) AS first,MAX(timestamp) AS last FROM point_observations GROUP BY source ORDER BY source")
        if self.postgres:
            gaps = self._rows("""SELECT source, previous AS start, timestamp AS end,
                ROUND(((EXTRACT(EPOCH FROM timestamp::timestamptz)-EXTRACT(EPOCH FROM previous::timestamptz))/3600)::numeric,1)::double precision AS hours
                FROM (SELECT source,timestamp,LAG(timestamp) OVER
                      (PARTITION BY source ORDER BY timestamp) AS previous FROM point_observations) p
                WHERE previous IS NOT NULL AND timestamp::timestamptz-previous::timestamptz>interval '24 hours'
                ORDER BY timestamp DESC LIMIT 200""")
        else:
            gaps = self._rows("""SELECT source, previous AS start, timestamp AS end,
            ROUND((strftime('%s',timestamp)-strftime('%s',previous))/3600.0,1) AS hours
            FROM (SELECT source,timestamp,LAG(timestamp) OVER
                  (PARTITION BY source ORDER BY timestamp) AS previous FROM point_observations)
            WHERE previous IS NOT NULL AND strftime('%s',timestamp)-strftime('%s',previous)>86400
            ORDER BY timestamp DESC LIMIT 200""")
        return {"sources": sources, "gaps_over_24h": gaps}

    def cached(self, key, provider):
        row = self.db.execute("SELECT value FROM geocode_cache WHERE cache_key=? AND provider=?", (key, provider)).fetchone()
        return json.loads(row["value"]) if row else None

    def cache(self, key, provider, value):
        self.db.execute("""INSERT INTO geocode_cache(cache_key,provider,value,created_at) VALUES (?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,value=excluded.value,created_at=excluded.created_at""",
            (key, provider, json.dumps(value), parse_time(None)))
        self.db.commit()

    def reverse_region(self, provider, lat, lon):
        """Use an indexed bounding box, then an exact distance check for a safe cache hit."""
        now = parse_time(None)
        if self.postgres:
            candidates = self._rows("""SELECT id,provider,center_lat,center_lon,radius_m,value,created_at,expires_at
                FROM reverse_regions WHERE provider=? AND expires_at>?
                  AND ST_DWithin(center,ST_SetSRID(ST_MakePoint(?,?),4326)::geography,30)
                  AND ST_DWithin(center,ST_SetSRID(ST_MakePoint(?,?),4326)::geography,radius_m)
                ORDER BY id DESC LIMIT 64""", (provider, now, lon, lat, lon, lat))
        else:
            candidates = self._rows("""SELECT r.* FROM reverse_regions r
            JOIN reverse_regions_spatial s ON s.id=r.id
            WHERE r.provider=? AND r.expires_at>?
              AND s.min_lat<=? AND s.max_lat>=? AND s.min_lon<=? AND s.max_lon>=?
            ORDER BY r.id DESC LIMIT 64""", (provider, now, lat, lat, lon, lon))
        for row in candidates:
            if metres(lat, lon, row["center_lat"], row["center_lon"]) <= row["radius_m"]:
                return json.loads(row["value"])
        return None

    def save_reverse_region(self, provider, lat, lon, radius_m, value):
        radius = max(0.0, min(float(radius_m), 30.0))
        now = datetime.now(timezone.utc)
        days = max(1, min(int(os.environ.get("GEOLOGS_REVERSE_CACHE_DAYS", "180")), 3650))
        lat_delta = radius / 111320
        lon_delta = min(180, radius / max(1, 111320 * abs(math.cos(math.radians(lat)))))
        cursor = self.db.execute("""INSERT INTO reverse_regions
            (provider,center_lat,center_lon,radius_m,value,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?) RETURNING id""", (provider, lat, lon, radius, json.dumps(value), now.isoformat(timespec="seconds"), (now + timedelta(days=days)).isoformat(timespec="seconds")))
        region_id = cursor.fetchone()["id"]
        if not self.postgres:
            self.db.execute("INSERT INTO reverse_regions_spatial VALUES (?,?,?,?,?)", (region_id, lat-lat_delta, lat+lat_delta, lon-lon_delta, lon+lon_delta))
        self.db.commit()


def reverse_cache_radius(result_type):
    """Only reuse precise addresses nearby; a city bounding box is not a safe boundary."""
    kind = str(result_type or "").lower()
    if kind in ("building", "house", "amenity", "place", "address"):
        return 15
    if kind == "street":
        return 30
    return 0


def json_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "GeoLogs/0.1 (personal self-hosted location archive)"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def provider_request(path, params):
    key = os.environ.get("GEOAPIFY_API_KEY")
    if not key:
        raise ValueError("Set GEOAPIFY_API_KEY for geocoding; saved places and recorded paths work without it")
    params = {**params, "apiKey": key}
    url = "https://api.geoapify.com" + path + "?" + urllib.parse.urlencode(params)
    return json_get(url)


def photon_result(feature):
    props = feature.get("properties", {})
    lon, lat = feature["geometry"]["coordinates"][:2]
    label = ", ".join(str(props[k]) for k in ("name", "street", "city", "state", "country") if props.get(k))
    place_name = str(props.get("name") or "").strip()
    category = ".".join(str(props[key]) for key in ("osm_key", "osm_value") if props.get(key))
    provider_place_id = ":".join(str(props[key]) for key in ("osm_type", "osm_id") if props.get(key))
    return {"name": label or "Unknown place", "place_name": place_name,
            "lat": lat, "lon": lon, "address": label, "category": category,
            "categories": [category] if category else [], "provider_place_id": provider_place_id}


def geoapify_reverse_result(value, lat, lon):
    """Keep Geoapify's place name separate from its formatted address."""
    address = str(value.get("formatted") or "").strip()
    if not address:
        address = ", ".join(str(value.get(key)).strip() for key in ("address_line1", "address_line2") if value.get(key))
    place_name = str(value.get("name") or "").strip()
    datasource = value.get("datasource") if isinstance(value.get("datasource"), dict) else {}
    raw = datasource.get("raw") if isinstance(datasource.get("raw"), dict) else {}
    if not place_name:
        place_name = str(raw.get("name") or "").strip()
    # Geoapify documents address_line1 as the amenity name for amenity results.
    if not place_name and value.get("result_type") == "amenity":
        place_name = str(value.get("address_line1") or "").strip()
    categories = value.get("categories") if isinstance(value.get("categories"), list) else []
    category = value.get("category") or (categories[0] if categories else None)
    return {"name": place_name or address or "Unknown place",
            "place_name": place_name, "address": address, "lat": lat, "lon": lon,
            "result_type": value.get("result_type"), "category": category,
            "categories": categories, "provider_place_id": value.get("place_id"),
            "distance_m": value.get("distance")}


# Place types a person visits. Roads, parking, transit stops, boundaries, and
# street furniture would otherwise be "nearest" to almost every stay.
#
# Geoapify cannot mix these two groups in one request: adding any category of
# the second group made it drop the first entirely (a Kroger 558 m away and the
# park the point was in both vanished, even inside a circle filter). Each group
# is therefore its own request, and the nearest named place of both wins.
NEAREST_PLACE_CATEGORY_GROUPS = (
    ",".join(("activity", "adult", "building", "camping", "catering", "childcare", "commercial", "education",
              "emergency", "entertainment", "healthcare", "heritage", "leisure", "memorial", "natural", "office",
              "pet", "production", "religion", "rental", "service", "ski")),
    ",".join(("accommodation", "airport", "beach", "national_park", "sport", "tourism")),
)
SKIPPED_PLACE_SUFFIXES = (".atm", ".vending_machine", ".charging_station", ".bicycle_parking", ".post_box")
# Search radii, smallest first. A stay's averaged position is searched within
# the smallest radius that covers its extent; 150 m covers a big-box store
# seen from its parking lot. Buckets keep the reverse cache reusable.
NEAREST_PLACE_RADII_M = (150, 300, 600, 1000)
# Inside a saved zone, a named place this close (or within the stay's extent) names the stay.
ZONE_PLACE_REACH_M = 60
PHOTON_UNNAMED_KEYS = ("highway", "boundary", "place", "railway", "landuse", "waterway", "power")


def nearest_place_radius(extent_m=None):
    """Search radius for a position whose samples spread over `extent_m`."""
    extent = float(extent_m or 0)
    return next((radius for radius in NEAREST_PLACE_RADII_M if radius >= extent), NEAREST_PLACE_RADII_M[-1])


def geoapify_candidates(data):
    """Named places in a Places API response, in its order, each with its own position."""
    candidates = []
    for feature in data.get("features", []) if isinstance(data, dict) else []:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        name = str(props.get("name") or "").strip()
        categories = props.get("categories") if isinstance(props.get("categories"), list) else []
        if not name or any(str(category).endswith(SKIPPED_PLACE_SUFFIXES) for category in categories):
            continue
        address = str(props.get("formatted") or "").strip()
        if not address:
            address = ", ".join(str(props.get(key)).strip() for key in ("address_line1", "address_line2") if props.get(key))
        candidates.append({
            "name": name, "place_name": name, "address": address,
            # The deepest category ("commercial.supermarket") says the most.
            "category": max(categories, key=lambda category: str(category).count("."), default=None),
            "categories": categories, "provider_place_id": props.get("place_id"),
            "distance_m": props.get("distance"), "place_lat": props.get("lat"), "place_lon": props.get("lon"),
            "source": "geoapify_places",
        })
    return candidates


def geoapify_nearest_result(data, lat, lon):
    """The first named place in a distance-ordered Places API response."""
    candidates = geoapify_candidates(data)
    return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"} if candidates else None


PLACES_PROVIDERS = {
    "geoapify": {"label": "Geoapify", "env": "GEOAPIFY_API_KEY"},
    "mapbox": {"label": "Mapbox", "env": "MAPBOX_ACCESS_TOKEN"},
    "photon": {"label": "Photon (self-hosted)", "env": "GEOLOGS_PHOTON_URL"},
}
# Without a choice in Settings, the first configured provider is used; a
# self-hosted Photon keeps the precedence it always had.
PLACES_PROVIDER_ORDER = ("photon", "geoapify", "mapbox")
# Mapbox POI categories that would otherwise name almost every stay.
MAPBOX_SKIPPED_CATEGORIES = {"atm", "parking", "parking_lot", "parking_garage", "bus_stop", "bus_station",
                             "ev_charging_station", "charging_station", "post_box", "vending_machine"}
# Reverse lookup only returns POIs associated with the exact coordinate. Nearby
# browsing uses Search Box category searches instead. Mapbox's category catalog
# has no parent/child relationship, so cover its broad visit-relevant groups and
# the separate grocery/wholesale groups that do not reliably appear in Shopping.
MAPBOX_NEARBY_CATEGORIES = (
    "food_and_drink", "services", "shopping", "office", "education", "grocery", "outdoors", "lodging",
    "wholesale_store", "place_of_worship", "sports", "tourist_attraction", "entertainment",
)
MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS = .12


def configured_places_providers():
    return {name: bool(os.environ.get(info["env"], "").strip()) for name, info in PLACES_PROVIDERS.items()}


def places_provider(store):
    """The provider chosen in Settings when it is configured, else the first configured one."""
    configured = configured_places_providers()
    chosen = store.settings().get("places_provider")
    if chosen and configured.get(chosen):
        return chosen
    return next((name for name in PLACES_PROVIDER_ORDER if configured[name]), None)


def places_provider_identity(store):
    """Changes whenever place names could change: the provider, and Photon's server."""
    name = places_provider(store)
    return f"photon:{os.environ.get('GEOLOGS_PHOTON_URL', '').rstrip('/')}" if name == "photon" else str(name)


def _require_provider(store):
    name = places_provider(store)
    if name is None:
        raise ValueError("Set GEOAPIFY_API_KEY, MAPBOX_ACCESS_TOKEN, or GEOLOGS_PHOTON_URL for place names and "
                         "search; saved places and recorded paths work without one")
    return name


def mapbox_request(path, params):
    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError("Set MAPBOX_ACCESS_TOKEN to use Mapbox")
    return json_get("https://api.mapbox.com" + path + "?" + urllib.parse.urlencode({**params, "access_token": token}))


def _mapbox_position(feature):
    props = feature.get("properties") or {}
    coordinates = props.get("coordinates") if isinstance(props.get("coordinates"), dict) else {}
    if coordinates.get("latitude") is not None and coordinates.get("longitude") is not None:
        return float(coordinates["latitude"]), float(coordinates["longitude"])
    lon, lat = (feature.get("geometry") or {}).get("coordinates", [None, None])[:2]
    return (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)


def mapbox_place(feature):
    """A Search Box feature in the shape the rest of GeoLogs uses."""
    props = feature.get("properties") or {}
    lat, lon = _mapbox_position(feature)
    name = str(props.get("name") or "").strip()
    address = str(props.get("full_address") or "").strip() or ", ".join(
        part for part in (name, str(props.get("place_formatted") or "").strip()) if part)
    category_ids = [str(item) for item in props.get("poi_category_ids") or []]
    categories = list(dict.fromkeys(category_ids + [str(item) for item in props.get("poi_category") or []]))
    is_poi = props.get("feature_type") == "poi"
    return {"name": name or address or "Unknown place", "place_name": name if is_poi else "",
            "lat": lat, "lon": lon, "address": address,
            "category": category_ids[0] if category_ids else (props.get("feature_type") or None),
            "categories": categories, "provider_place_id": props.get("mapbox_id"),
            "result_type": props.get("feature_type")}


def mapbox_candidates(data, lat, lon, radius):
    """Named POIs within `radius`, nearest first; Search Box /reverse takes no radius and states no order."""
    candidates = []
    for feature in data.get("features", []) if isinstance(data, dict) else []:
        place = mapbox_place(feature)
        if not place["place_name"] or place["lat"] is None:
            continue
        if MAPBOX_SKIPPED_CATEGORIES & set(place["categories"]):
            continue
        distance = metres(lat, lon, place["lat"], place["lon"])
        if distance <= radius:
            candidates.append({**place, "place_lat": place["lat"], "place_lon": place["lon"],
                               "distance_m": round(distance), "source": "mapbox_searchbox"})
    return sorted(candidates, key=lambda place: place["distance_m"])


def _mapbox_bbox(lat, lon, radius):
    """Bounding box for a radius in metres; None when it would cross the antimeridian."""
    lat_delta = radius / 111320
    lon_delta = radius / (111320 * max(.01, math.cos(math.radians(lat))))
    west, east = lon - lon_delta, lon + lon_delta
    if west < -180 or east > 180:
        return None
    return ",".join(str(value) for value in (west, max(-90, lat - lat_delta), east, min(90, lat + lat_delta)))


def _mapbox_nearby_candidates(lat, lon, radius):
    """POIs around a position from category search, merged with exact reverse hits."""
    bbox = _mapbox_bbox(lat, lon, radius)
    request_lock, last_request = threading.Lock(), [0.0]

    def limited_request(path, params):
        # Space request starts below Mapbox's default ten-per-second limit while
        # still allowing their network waits to overlap.
        with request_lock:
            delay = last_request[0] + MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            last_request[0] = time.monotonic()
        return mapbox_request(path, params)

    def fetch(category):
        try:
            if category is None:
                data = limited_request("/search/searchbox/v1/reverse", {
                    "longitude": lon, "latitude": lat, "types": "poi", "limit": 10,
                })
            else:
                params = {"proximity": f"{lon},{lat}", "limit": 25, "exclude_fields": "photos,reviews"}
                if bbox:
                    params["bbox"] = bbox
                data = limited_request(f"/search/searchbox/v1/category/{category}", params)
            return mapbox_candidates(data, lat, lon, radius), None
        except (OSError, TypeError, KeyError, ValueError) as exc:
            return [], exc

    # Four workers avoid a long serial wait across the category requests.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, (None, *MAPBOX_NEARBY_CATEGORIES)))
    if all(error is not None for _group, error in results):
        raise results[0][1]
    unique = {}
    for place in (place for group, _error in results for place in group):
        key = place.get("provider_place_id") or (
            place["name"].casefold(), round(place["place_lat"], 6), round(place["place_lon"], 6))
        previous = unique.get(key)
        if previous is None or place["distance_m"] < previous["distance_m"]:
            unique[key] = place
    return sorted(unique.values(), key=lambda place: place["distance_m"])


def mapbox_nearest_result(data, lat, lon, radius):
    candidates = mapbox_candidates(data, lat, lon, radius)
    return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"} if candidates else None


# Place search looks near you first: within this distance a match is "nearby".
SEARCH_NEARBY_M = 100_000


def _geoapify_results(params):
    data = provider_request("/v1/geocode/search", {"limit": 5, "format": "json", **params})
    return [{"name": r.get("name") or r.get("formatted") or params["text"],
             "place_name": r.get("name") or "", "lat": r["lat"], "lon": r["lon"],
             "address": r.get("formatted", ""),
             "category": r.get("category") or ((r.get("categories") or [None])[0]),
             "categories": r.get("categories") if isinstance(r.get("categories"), list) else [],
             "provider_place_id": r.get("place_id")} for r in data.get("results", [])]


def _geoapify_search(query, bias):
    """Matches near you, then the best matches anywhere.

    A proximity bias alone lets a strong text match on another continent win
    (an exact "Tea Capital" in India over a tea shop across town), so the
    nearby area is searched on its own first.
    """
    if not bias:
        return _geoapify_results({"text": query})
    near = {"bias": f"proximity:{bias[1]},{bias[0]}"}
    return (_geoapify_results({"text": query, **near, "filter": f"circle:{bias[1]},{bias[0]},{SEARCH_NEARBY_M}"})
            + _geoapify_results({"text": query, **near}))


def _photon_search(query, bias):
    photon = os.environ.get("GEOLOGS_PHOTON_URL", "").rstrip("/")
    params = {"q": query, "limit": 5}
    if bias:
        params.update({"lat": bias[0], "lon": bias[1]})
    data = json_get(photon + "/api?" + urllib.parse.urlencode(params))
    return [photon_result(r) for r in data.get("features", [])]


def _mapbox_search(query, bias):
    """Mapbox's /suggest, made for finding places by name near you.

    Its /forward endpoint reads words such as "capital" as a region, so
    "Tea Capital" returned tea shops in Kuwait City; /suggest returns the shop
    across town first. Suggestions carry no coordinates: place_details()
    fetches them, in the same billing session, for the one that is chosen.
    """
    session = str(uuid.uuid4())
    params = {"q": query[:256], "limit": 8, "session_token": session}
    if bias:
        params["proximity"] = f"{bias[1]},{bias[0]}"
        params["origin"] = f"{bias[1]},{bias[0]}"
    data = mapbox_request("/search/searchbox/v1/suggest", params)
    results = []
    for item in data.get("suggestions", []):
        if not item.get("mapbox_id"):
            continue
        name = str(item.get("name") or "").strip()
        address = str(item.get("full_address") or "").strip() or ", ".join(
            part for part in (name, str(item.get("place_formatted") or "").strip()) if part)
        category_ids = [str(value) for value in item.get("poi_category_ids") or []]
        results.append({"name": name or address or "Unknown place",
                        "place_name": name if item.get("feature_type") == "poi" else "",
                        "lat": None, "lon": None, "address": address,
                        "category": category_ids[0] if category_ids else item.get("feature_type"),
                        "categories": category_ids, "provider_place_id": item["mapbox_id"],
                        "distance_m": round(item["distance"]) if item.get("distance") is not None else None,
                        "session": session})
    return results


def place_details(store, provider_place_id, session=None):
    """Coordinates for a search result that arrived without them (Mapbox suggestions)."""
    if _require_provider(store) != "mapbox":
        raise ValueError("place details are only needed for Mapbox search results")
    provider_place_id = str(provider_place_id or "").strip()
    if not provider_place_id or len(provider_place_id) > 300:
        raise ValueError("id is required")
    key = "place:" + provider_place_id
    cached = store.cached(key, "mapbox:searchbox-v1")
    if cached is not None:
        return cached
    data = mapbox_request("/search/searchbox/v1/retrieve/" + urllib.parse.quote(provider_place_id, safe=""),
                          {"session_token": session or str(uuid.uuid4())})
    feature = (data.get("features") or [None])[0]
    if feature is None:
        raise ValueError("place not found")
    place = mapbox_place(feature)
    if place["lat"] is None:
        raise ValueError("place has no coordinates")
    store.cache(key, "mapbox:searchbox-v1", place)
    return place


def geocode(store, query, lat=None, lon=None, resolve=5):
    """Places matching `query`, nearby ones first, each with its distance from you.

    `resolve` is how many of the leading results must carry coordinates:
    Mapbox suggestions don't, and each lookup costs, so the browser asks for
    none and fetches only the place you pick (place_details).
    """
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError("query is required (max 200 characters)")
    if (lat is None) != (lon is None):
        raise ValueError("lat and lon must be provided together")
    if lat is not None:
        bias = (float(lat), float(lon))
        if not (-90 <= bias[0] <= 90 and -180 <= bias[1] <= 180):
            raise ValueError("invalid search bias coordinates")
    else:
        latest = store.latest()
        bias = (float(latest["lat"]), float(latest["lon"])) if latest else None
    name = _require_provider(store)
    # Cache by nearby search area; a result from another city must not win later.
    key = "search-v2:" + query.casefold() + (f":{bias[0]:.2f}:{bias[1]:.2f}" if bias else ":unbiased")
    provider = {"photon": "photon:" + os.environ.get("GEOLOGS_PHOTON_URL", "").rstrip("/"),
                "geoapify": "geoapify", "mapbox": "mapbox:searchbox-v1"}[name]
    results = store.cached(key, provider)
    if results is None:
        results = {"photon": _photon_search, "geoapify": _geoapify_search, "mapbox": _mapbox_search}[name](query, bias)
        store.cache(key, provider, results)
    unique, seen = [], set()
    for place in results:
        identity = place.get("provider_place_id") or (place["name"], round(place["lat"] or 0, 4), round(place["lon"] or 0, 4))
        if identity in seen:
            continue
        seen.add(identity)
        if bias and place.get("lat") is not None:
            place = {**place, "distance_m": round(metres(bias[0], bias[1], place["lat"], place["lon"]))}
        unique.append(place)
    # Nearby matches first, in the provider's order; then the rest, nearest first.
    def rank(item):
        index, place = item
        distance = place.get("distance_m")
        if distance is None:
            return (1, 0, index)
        return (0, 0, index) if distance <= SEARCH_NEARBY_M else (0, 1, distance)
    ordered = [place for _index, place in sorted(enumerate(unique), key=rank)][:8]
    for index, place in enumerate(ordered[:resolve]):
        if place.get("lat") is None:
            details = place_details(store, place["provider_place_id"], place.get("session"))
            ordered[index] = {**place, "lat": details["lat"], "lon": details["lon"],
                              "address": place["address"] or details["address"]}
    # Callers that need coordinates get only the results that have them.
    return [place for place in ordered if place.get("lat") is not None] if resolve else ordered


def _photon_features(lat, lon, radius, limit=10):
    photon = os.environ.get("GEOLOGS_PHOTON_URL", "").rstrip("/")
    data = json_get(photon + "/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "radius": radius / 1000, "limit": limit}))
    features = data.get("features") or []
    named = [feature for feature in features
             if (feature.get("properties") or {}).get("name")
             and (feature.get("properties") or {}).get("osm_key") not in PHOTON_UNNAMED_KEYS]
    return features, named


def _photon_candidates(lat, lon, radius):
    candidates = []
    for feature in _photon_features(lat, lon, radius, limit=15)[1]:
        place = photon_result(feature)
        candidates.append({**place, "name": place["place_name"], "place_lat": place["lat"], "place_lon": place["lon"],
                           "distance_m": round(metres(lat, lon, place["lat"], place["lon"])), "source": "photon"})
    return sorted(candidates, key=lambda place: place["distance_m"])


def _photon_lookup(lat, lon, radius):
    features, named = _photon_features(lat, lon, radius)
    feature = named[0] if named else (features[0] if features else None)
    value = photon_result(feature) if feature else {"name": "Unknown place", "place_name": "", "address": ""}
    if named:
        value["place_lat"], value["place_lon"] = value["lat"], value["lon"]
    else:
        value["place_name"] = ""
    kind = "place" if named else (feature or {}).get("properties", {}).get("type")
    return {**value, "name": value.get("place_name") or value.get("address") or "Unknown place",
            "lat": lat, "lon": lon}, kind


def _geoapify_candidates(lat, lon, radius):
    features = []
    for categories in NEAREST_PLACE_CATEGORY_GROUPS:
        try:
            data = provider_request("/v2/places", {
                "categories": categories, "conditions": "named",
                "filter": f"circle:{lon},{lat},{radius}", "bias": f"proximity:{lon},{lat}", "limit": 20,
            })
        except (OSError, TypeError, KeyError, ValueError):
            continue  # the other group, or the address fallback, still names the point
        if isinstance(data, dict):
            features.extend(data.get("features") or [])
    # Each group arrives ordered by distance; merge so the nearest of both comes first.
    features.sort(key=lambda feature: (((feature or {}).get("properties") or {}).get("distance") is None,
                                       ((feature or {}).get("properties") or {}).get("distance") or 0))
    return geoapify_candidates({"features": features})


def _geoapify_lookup(lat, lon, radius):
    candidates = _geoapify_candidates(lat, lon, radius)
    if candidates:
        return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"}, "place"
    data = provider_request("/v1/geocode/reverse", {"lat": lat, "lon": lon, "format": "json"})
    value = (data.get("results") or [{}])[0]
    return {**geoapify_reverse_result(value, lat, lon), "source": "geoapify_reverse"}, value.get("result_type")


def _mapbox_candidates(lat, lon, radius):
    return mapbox_candidates(mapbox_request("/search/searchbox/v1/reverse", {
        "longitude": lon, "latitude": lat, "types": "poi", "limit": 10}), lat, lon, radius)


def _mapbox_lookup(lat, lon, radius):
    try:
        candidates = _mapbox_candidates(lat, lon, radius)
    except (OSError, TypeError, KeyError, ValueError):
        candidates = []
    if candidates:
        return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"}, "place"
    data = mapbox_request("/search/searchbox/v1/reverse", {
        "longitude": lon, "latitude": lat, "types": "address,street", "limit": 1})
    feature = (data.get("features") or [None])[0]
    if feature is None:
        return {"name": "Unknown place", "place_name": "", "address": "", "lat": lat, "lon": lon,
                "source": "mapbox_searchbox"}, None
    place = mapbox_place(feature)
    return {**place, "name": place["address"] or place["name"], "place_name": "", "lat": lat, "lon": lon,
            "source": "mapbox_searchbox"}, place["result_type"]


def _provider_cache_key(name):
    # Versions the reverse cache and keeps providers apart. Geoapify v2 drops
    # lookups made with the single mixed-category request, which cached
    # street addresses where named places existed.
    return {"photon": "photon:nearest-v1:" + os.environ.get("GEOLOGS_PHOTON_URL", "").rstrip("/"),
            "geoapify": "geoapify:nearest-v2", "mapbox": "mapbox:nearest-v1"}[name]


def nearby_places(store, lat, lon, radius_m=None, limit=15):
    """Named places around a position, nearest first, from the provider chosen in Settings.

    Used to correct a visit that was named after the wrong neighbour in a busy
    place, and to match name-only reminders against every place close by.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    radius = nearest_place_radius(radius_m)
    name = _require_provider(store)
    # Version Mapbox's category-backed list separately from its reverse lookup,
    # invalidating the older cache that contained only reverse results.
    provider = "mapbox:categories-v1" if name == "mapbox" else _provider_cache_key(name)
    key = provider + f":candidates:r{radius}"
    with _reverse_lock:
        candidates = store.reverse_region(key, lat, lon)
        if candidates is None:
            candidates = {"photon": _photon_candidates, "geoapify": _geoapify_candidates,
                          "mapbox": _mapbox_nearby_candidates}[name](lat, lon, radius)
            store.save_reverse_region(key, lat, lon, 15, candidates)
    places = []
    for place in candidates:
        if place.get("place_lat") is not None and place.get("place_lon") is not None:
            place = {**place, "distance_m": round(metres(lat, lon, place["place_lat"], place["place_lon"]))}
        places.append(place)
    places.sort(key=lambda place: (place.get("distance_m") is None, place.get("distance_m") or 0))
    return places[:max(1, min(int(limit), 50))]


def search_text(value):
    """Lowercase words; apostrophes vanish so "Sam's" matches "sams" and "McDonald's" "mcdonalds"."""
    text = re.sub(r"['’`]", "", str(value or "").casefold())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def street_key(address):
    """The street and town of a house address: "9004 Longacre Drive, Little Rock, ..." -> "longacre drive|little rock".

    None when the first part does not start with a house number.
    """
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    match = re.match(r"^\d+[\w/-]*\s+(.+)$", parts[0]) if len(parts) >= 2 else None
    return search_text(match.group(1)) + "|" + search_text(parts[1]) if match else None


def name_matches(pattern, name):
    """Whether a place name matches what a reminder asked for.

    Every word of the pattern must start a word of the name ("star" finds
    "Starbucks Coffee") or, for words of four letters or more, be a close
    spelling of one ("starbuks", "walgreens" for "Walgreen Co").
    """
    wanted, words = search_text(pattern).split(), search_text(name).split()
    if not wanted or not words:
        return False
    def close(term):
        return any(word.startswith(term)
                   or (len(term) >= 4 and difflib.SequenceMatcher(None, term, word).ratio() >= 0.8)
                   for word in words)
    return all(close(term) for term in wanted)


def reverse_geocode(store, lat, lon, radius_m=None):
    """Name a position after the nearest named place, else its street address.

    Callers pass the averaged position of a stay and, for an area stay, its
    extent as `radius_m`. The provider chosen in Settings is asked for named
    places within the search radius: Geoapify's Places API and Photon's reverse
    endpoint take the radius directly; Mapbox's Search Box reverse lookup does
    not, so its POIs are filtered by distance here. Only when nothing named is
    nearby does the result fall back to the street address.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    radius = nearest_place_radius(radius_m)
    name = _require_provider(store)
    provider = _provider_cache_key(name) + f":r{radius}"
    # A lock coalesces simultaneous browser/MCP requests within this process.
    with _reverse_lock:
        cached = store.reverse_region(provider, lat, lon)
        if cached is not None:
            approximate = metres(lat, lon, cached["lat"], cached["lon"]) > 0.5
            return {**cached, "lat": lat, "lon": lon, "cache": "region", "approximate": approximate}
        result, kind = {"photon": _photon_lookup, "geoapify": _geoapify_lookup, "mapbox": _mapbox_lookup}[name](lat, lon, radius)
        store.save_reverse_region(provider, lat, lon, reverse_cache_radius(kind), result)
        return {**result, "cache": "provider", "approximate": False}


def label_at(labels, lat, lon, source=None, started_at=None, ended_at=None):
    """The name correction for a position, or for a stay when its source and times are given.

    A visit-only correction applies to a stay from its source that overlaps its
    window at that spot, and beats an every-visit one; among equals the nearest
    centre wins. Without a stay's times only every-visit corrections apply.
    """
    covering = []
    for label in labels:
        distance = metres(lat, lon, label["lat"], label["lon"])
        if distance > label["radius_m"]:
            continue
        if label.get("started_at"):
            if not started_at or (label.get("source") and label["source"] != source):
                continue
            if started_at > label["ended_at"] or ended_at < label["started_at"]:
                continue
        covering.append((not label.get("started_at"), distance, label))
    return min(covering, key=lambda item: item[:2])[2] if covering else None


def windows_overlap(item, started_at, ended_at, share=MODE_OVERRIDE_MIN_OVERLAP):
    """Whether item's started_at..ended_at covers `share` of the shorter of it and the window."""
    start, end = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
    item_start, item_end = datetime.fromisoformat(item["started_at"]), datetime.fromisoformat(item["ended_at"])
    overlap = (min(end, item_end) - max(start, item_start)).total_seconds()
    shorter = max(1.0, min((end - start).total_seconds(), (item_end - item_start).total_seconds()))
    return overlap >= share * shorter


def mode_override_for(overrides, source, target, started_at, ended_at):
    """The correction for a trip or area stay: same source and target, overlapping most of either span."""
    start, end = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
    best = None
    for item in overrides:
        if item["source"] != source or item["target"] != target:
            continue
        item_start, item_end = datetime.fromisoformat(item["started_at"]), datetime.fromisoformat(item["ended_at"])
        overlap = (min(end, item_end) - max(start, item_start)).total_seconds()
        shorter = max(1.0, min((end - start).total_seconds(), (item_end - item_start).total_seconds()))
        if overlap >= MODE_OVERRIDE_MIN_OVERLAP * shorter and (best is None or overlap > best[0]):
            best = (overlap, item)
    return best[1] if best else None


def labels_by_id(labels):
    return {label["id"]: label for label in labels}


def _label_place(label):
    return {"name": label["name"], "named": True, "address": label.get("address") or "", "category": label.get("category") or "",
            "categories": [label["category"]] if label.get("category") else [],
            "provider_place_id": label.get("provider_place_id") or "", "label_id": label["id"]}


def ingest_token_hash(token):
    """Device tokens are long and random, so a plain SHA-256 is enough to keep them out of the database."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_allows_source(locked, source):
    """Whether a device token locked to `locked` may send as `source`."""
    if not locked:
        return True
    return source == locked or (locked.endswith(":") and str(source or "").startswith(locked))


def named_place_near(store, lat, lon, within_m, lookup=True):
    """The named place within `within_m` of a position, or None.

    With lookup=False only the reverse-lookup cache is read, so a sweep over
    the whole history spends no provider quota.
    """
    try:
        if lookup:
            value = reverse_geocode(store, lat, lon)
        else:
            radius = nearest_place_radius(None)
            value = store.reverse_region(_provider_cache_key(_require_provider(store)) + f":r{radius}", float(lat), float(lon))
    except (ValueError, OSError, KeyError):
        return None
    if not value or not value.get("place_name"):
        return None
    if value.get("place_lat") is not None and value.get("place_lon") is not None:
        distance = metres(lat, lon, value["place_lat"], value["place_lon"])
    elif value.get("distance_m") is not None:
        # Results cached before places kept their own position give only their
        # distance from where that lookup was made.
        distance = float(value["distance_m"]) + metres(lat, lon, value.get("lat", lat), value.get("lon", lon))
    else:
        return None
    return value if distance <= within_m else None


def place_for_coordinate(store, lat, lon, radius_m=None):
    """Return the nearest named place (or address) plus its full address.

    A name you pinned to this spot wins over the provider.
    """
    label = label_at(store.place_labels(), lat, lon)
    if label:
        return _label_place(label)
    try:
        value = reverse_geocode(store, lat, lon, radius_m)
        # `named` is false when only a street address was found nearby.
        return {"name": str(value.get("place_name") or value.get("name") or value.get("address") or "")[:300],
                "named": bool(value.get("place_name")),
                "address": str(value.get("address") or "")[:300],
                "category": str(value.get("category") or "")[:160],
                "categories": value.get("categories") if isinstance(value.get("categories"), list) else [],
                "provider_place_id": str(value.get("provider_place_id") or "")[:300],
                "place_lat": value.get("place_lat"), "place_lon": value.get("place_lon")}
    except (ValueError, OSError, KeyError):
        return {"name": "", "named": False, "address": "", "category": "", "categories": [], "provider_place_id": ""}


def save_place_label(store, raw):
    """Save a name correction, looking up its address when none is given.

    A label's address is the only one its visits get, so a rename without one
    (an MCP call that passes just a name) would leave visit search with only
    coordinates. A named place's address is used only when that place lies
    inside the label: a friend's house must not take the café down the street's.
    """
    if not str(raw.get("address") or "").strip():
        try:
            lat, lon = float(raw["lat"]), float(raw["lon"])
            value = reverse_geocode(store, lat, lon)
            place_lat, place_lon = value.get("place_lat"), value.get("place_lon")
            inside = not value.get("place_name") or (
                place_lat is not None and place_lon is not None
                and metres(lat, lon, place_lat, place_lon) <= float(raw.get("radius_m") or 50))
            if inside and value.get("address"):
                raw = {**raw, "address": str(value["address"])[:300]}
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return store.save_place_label(raw)


def is_home_place(place):
    return str(place.get("name") or place.get("display_name") or "").strip().casefold() in ("home", "my home")


def zone_at(places, lat, lon):
    """The saved place whose circle covers a position, nearest centre first."""
    matches = [(metres(lat, lon, place["lat"], place["lon"]), place) for place in places]
    matches = [(distance, place) for distance, place in matches if distance <= place["radius_m"]]
    return min(matches, key=lambda item: item[0])[1] if matches else None


def saved_place_as_found(place):
    return {"name": place["name"], "named": True, "address": place.get("address") or "",
            "category": place.get("category") or "", "categories": [place["category"]] if place.get("category") else [],
            "provider_place_id": place.get("provider_place_id") or ""}


def place_in_zone(store, lat, lon, zone, radius_m=None, lookup=None):
    """Name a position that may lie inside a saved place.

    Saved places are zones, not names: a university holds many buildings, and
    each is a place of its own. Inside a zone, a named place counts only when it
    is right there (within ZONE_PLACE_REACH_M, or the spot's own extent), so a
    house is not named after a shop down the street; otherwise the zone's name
    is used. Home is the one zone that names everything inside it. A name you
    pinned to the spot still wins.
    """
    if zone is not None and is_home_place(zone) and not label_at(store.place_labels(), lat, lon):
        return saved_place_as_found(zone)
    found = (lookup or place_for_coordinate)(store, lat, lon, radius_m)
    if zone is None or found.get("label_id"):
        return found
    reach = max(ZONE_PLACE_REACH_M, float(radius_m or 0))
    if found.get("named", True) and found.get("name") and (found.get("place_lat") is None
                           or metres(lat, lon, found["place_lat"], found["place_lon"]) <= reach):
        return found
    return {**saved_place_as_found(zone), "address": found.get("address") or zone.get("address") or ""}


def current_location(store):
    """Most recent fix enriched with a saved or reverse-geocoded place name."""
    point = store.latest()
    if not point:
        return None
    from .analysis import stays
    ongoing = next((stay for stay in stays(store, start=point["timestamp"], source=point["source"], limit=5)
                    if stay["ongoing"] and stay["started_at"] <= point["timestamp"]), None)
    labels = store.place_labels()
    label = (labels_by_id(labels).get(ongoing["label_id"]) if ongoing and ongoing.get("label_id") else None) \
        or label_at(labels, point["lat"], point["lon"])
    zone = zone_at(store.places(), point["lat"], point["lon"])
    point.update({"zone_id": zone["id"] if zone else None, "zone": zone["name"] if zone else None})
    if label:
        point.update({"place_id": None, **_label_place(label), "place_name": label["name"]})
        return point
    found = place_in_zone(store, point["lat"], point["lon"], zone)
    point.update({"place_id": zone["id"] if zone and is_home_place(zone) else None, "place_name": found["name"],
                  "address": found["address"], "category": found["category"],
                  "provider_place_id": found["provider_place_id"]})
    return point


def address_for_coordinate(store, lat, lon):
    """Use the reverse cache first, then the configured provider when available."""
    return place_for_coordinate(store, lat, lon)["address"]


def deliver_webhook(store, events):
    url = os.environ.get("GEOLOGS_WEBHOOK_URL")
    if not url or not events:
        return
    parsed_url = urllib.parse.urlparse(url)
    local_hosts = ("127.0.0.1", "::1", "localhost", "host.docker.internal")
    if parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and parsed_url.hostname in local_hosts):
        raise ValueError("webhook URL must use HTTPS or loopback HTTP")
    for event in events:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("GEOLOGS_WEBHOOK_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(event, separators=(",", ":")).encode()
        secret = os.environ.get("GEOLOGS_WEBHOOK_SECRET")
        if secret:
            timestamp = str(int(datetime.now(timezone.utc).timestamp()))
            headers["X-Webhook-Timestamp"] = timestamp
            headers["X-Webhook-Signature-V2"] = hmac.new(
                secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(url, body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=8):
                status = "sent"
        except Exception as exc:
            status = "failed: " + str(exc)[:150]
        store.db.execute("UPDATE events SET webhook_status=? WHERE id=?", (status, event["id"]))
    store.db.commit()
