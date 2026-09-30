"""Location domain model with SQLite and PostgreSQL/PostGIS storage."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .postgres import PostgresConnection


_reverse_lock = threading.Lock()
DEFAULT_SETTINGS = {"speed_strategy": "reported", "reported_speed_offset_kmh": 0.0, "timezone": "UTC"}
MAX_CONNECTED_GAP_SECONDS = 900
MAX_PLAUSIBLE_SPEED_MPS = 55.6
MOVEMENT_MODES = ("driving", "cycling", "running", "walking")
# Travel that leaves and returns to one place is kept only when it really went somewhere;
# smaller excursions are GPS drift between two halves of one stay.
ROUND_TRIP_MIN_EXCURSION_M = 150
ROUND_TRIP_MIN_DISTANCE_M = 300


def database_path() -> str:
    return os.environ.get("GEOTRACKER_DB", str(Path.home() / ".local/share/geotracker/geotracker.db"))


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


def mode_for(speed_mps, accuracy_m=None):
    if speed_mps is None or (accuracy_m is not None and accuracy_m > 150):
        return "unknown"
    kmh = speed_mps * 3.6
    if kmh < 1.5:
        return "idle"
    if kmh < 7:
        return "walking"
    if kmh < 22:
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


def classify_motion(moves):
    """Estimate walking, running, cycling, or driving from connected moves.

    `moves` holds (distance_m, seconds, reported_speed_mps or None). Speeds are
    weighted by distance, so a single GPS jump cannot turn a walk into a drive.
    Running and cycling overlap between about 12 and 16 km/h; the median speed
    decides, which suits recreational runs and commuter cycling. A median of
    25 km/h or more is treated as driving: city traffic sampled once a minute
    averages about that, while few riders sustain it.
    """
    samples = []
    for distance, seconds, reported in moves:
        if seconds <= 0 or distance < 10:
            continue
        speed = max(distance / seconds, reported or 0)
        samples.append((speed * 3.6, distance))
    if not samples:
        return None
    total = sum(distance for _, distance in samples)
    fast = sum(distance for kmh, distance in samples if kmh >= 35)
    median = _weighted_percentile(samples, .5)
    if fast >= 1000 or fast >= .35 * total or median >= 25:
        mode = "driving"
    elif median >= 15:
        mode = "cycling"
    elif median >= 6.5:
        mode = "running"
    else:
        mode = "walking"
    moving_seconds = sum(seconds for distance, seconds, _ in moves if seconds > 0 and distance >= 10)
    return {"mode": mode, "median_kmh": round(median, 1), "distance_m": total, "moving_seconds": moving_seconds}


def is_excursion(trip):
    """True when a trip went far enough to matter even if it ended where it began."""
    return (trip.get("max_excursion_m", 0) >= ROUND_TRIP_MIN_EXCURSION_M
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


def trips_between_stays(points, stay_rows, start=None, end=None):
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
                    moves.append((distance, seconds, reported_speed(point)))
                if not _same_source_state(previous, point):
                    held_since = point
            motion = classify_motion(moves)
            if motion is None:
                continue
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
                "median_kmh": motion["median_kmh"], "max_excursion_m": max(0, round(excursion)),
                "mode": motion["mode"], "modes": {motion["mode"]: len(moves)}, "loop": loop,
                "from_stay_id": origin["id"], "to_stay_id": destination["id"],
                "from_name": origin.get("place_name"), "to_name": destination.get("place_name"),
                "from_lat": origin["lat"], "from_lon": origin["lon"],
                "to_lat": destination["lat"], "to_lon": destination["lon"],
                "recorded_points": len(trip_points),
            }
            # Two stays at one spot joined by GPS drift are one visit, not a trip.
            if loop and not is_excursion(trip):
                continue
            route_segments = [smooth_route_segment(segment) for segment in route_segments_from_points(trip_points)]
            trip.update({"route_segments": route_segments,
                         "coordinates": route_segments[0] if len(route_segments) == 1 else [],
                         "coverage_complete": len(route_segments) == 1})
            trips.append(trip)
    return sorted(trips, key=lambda trip: trip["start"], reverse=True)


def route_segments_from_points(points):
    """Connect plausible fixes per source and skip readings too inaccurate to route."""
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
                if len(current) > 1:
                    segments.append(current)
                current = [[point["lon"], point["lat"]]]
            else:
                current.append([point["lon"], point["lat"]])
            if previous is None or not _same_source_state(previous, point):
                held_since = point
            previous = point
        if len(current) > 1:
            segments.append(current)
    return segments


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


class Store:
    def __init__(self, path=None):
        url = os.environ.get("GEOTRACKER_DATABASE_URL") if path is None else None
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
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(summary_date,timezone,source)
        );
        CREATE INDEX IF NOT EXISTS daily_summaries_date ON daily_summaries(summary_date DESC,timezone);
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 150,
            address TEXT, category TEXT, provider_place_id TEXT, saved INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER NOT NULL REFERENCES places(id),
            active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
            cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
            created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT,
            destination_name TEXT, destination_address TEXT,
            destination_category TEXT, destination_provider_place_id TEXT,
            FOREIGN KEY(place_id) REFERENCES places(id)
        );
        CREATE INDEX IF NOT EXISTS reminders_active_created ON reminders(active,created_at);
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
            kind TEXT NOT NULL DEFAULT 'stationary', activity TEXT, distance_m REAL, radius_m REAL
        );
        CREATE INDEX IF NOT EXISTS observed_stays_v2_time ON observed_stays_v2(started_at);
        CREATE INDEX IF NOT EXISTS observed_stays_v2_source_time ON observed_stays_v2(source,started_at,ended_at);
        CREATE TABLE IF NOT EXISTS stay_observation_state (
            source TEXT PRIMARY KEY, last_observation_id INTEGER NOT NULL,
            last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL
        );
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
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            rebuild_analysis = version < 11
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
        # Stay algorithm versions: 5 and 8 changed stationary stays; 12 adds area
        # stays (moving around one place) and stray-fix tolerance. One rebuild
        # covers every missing version.
        rebuild_versions = (5, 8, 12)
        if self.postgres and self._missing_migrations(rebuild_versions):
            self.db.execute("SELECT pg_advisory_xact_lock(hashtext('geotracker:stays:rebuild'))")
            missing = self._missing_migrations(rebuild_versions)
            if missing:
                from .analysis import process_stays
                process_stays(self, rebuild=True)
                for version_number in missing:
                    self.db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version_number,))
        elif not self.postgres and rebuild_analysis:
            from .analysis import process_stays
            process_stays(self, rebuild=True)
            self.db.execute("PRAGMA user_version=11")
        elif not self.db.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone() and self.db.execute("SELECT 1 FROM point_observations LIMIT 1").fetchone():
            if self.postgres:
                self.db.execute("SELECT pg_advisory_xact_lock(hashtext('geotracker:stays:bootstrap'))")
            if not self.db.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone():
                from .analysis import process_stays
                process_stays(self)
        if not self.postgres and version < 8:
            self.db.execute("PRAGMA user_version=8")
        if not self.postgres and version < 9:
            self.db.execute("DELETE FROM analysis_cache")
            self.db.execute("PRAGMA user_version=9")
        if not self.postgres and version < 11:
            self.db.execute("PRAGMA user_version=11")
        self.db.commit()

    def close(self):
        self.db.close()

    def _missing_migrations(self, versions):
        applied = {row["version"] for row in self._rows("SELECT version FROM schema_migrations")}
        return [version for version in versions if version not in applied]

    def _rows(self, query, args=()):
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        if os.environ.get("GEOTRACKER_TIMEZONE"):
            values["timezone"] = os.environ["GEOTRACKER_TIMEZONE"]
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
        values = {"speed_strategy": strategy, "reported_speed_offset_kmh": round(offset, 2),
                  "timezone": timezone_name}
        for key, value in values.items():
            self.db.execute("""INSERT INTO app_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, json.dumps(value)))
        self.db.commit()
        return values

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

    def daily_overview_watermark(self, start, end, source=None):
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
        value = ["stay-trips-v3", self.analysis_watermark(start, end, source), event_row["count"], event_row["max_id"], places, reminders]
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def stored_daily_overview(self, date, timezone_name, source=None, watermark=None):
        args = [date, timezone_name, source or ""]
        where = "summary_date=? AND timezone=? AND source=?"
        if watermark is not None:
            where += " AND watermark=?"
            args.append(watermark)
        row = self.db.execute("SELECT summary FROM daily_summaries WHERE " + where, args).fetchone()
        return json.loads(row["summary"]) if row else None

    def save_daily_overview(self, value, watermark):
        now = parse_time(None)
        totals = value["totals"]
        self.db.execute("""INSERT INTO daily_summaries
            (summary_date,timezone,source,watermark,points,distance_km,trip_count,place_count,
             reminder_count,summary,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(summary_date,timezone,source) DO UPDATE SET
            watermark=excluded.watermark,points=excluded.points,distance_km=excluded.distance_km,
            trip_count=excluded.trip_count,place_count=excluded.place_count,
            reminder_count=excluded.reminder_count,summary=excluded.summary,updated_at=excluded.updated_at""",
            (value["date"], value["timezone"], value.get("source") or "", watermark,
             totals["raw_fixes"], totals["distance_km"], totals["trips"], totals["places"],
             totals["reminders_triggered"], json.dumps(value, separators=(",", ":")), now, now))
        self.db.commit()
        return value

    def daily_overviews(self, start=None, end=None, limit=90):
        clauses, args = [], []
        if start:
            clauses.append("summary_date>=?"); args.append(str(start))
        if end:
            clauses.append("summary_date<=?"); args.append(str(end))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
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
            mode = mode_for(speed, accuracy)
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
            from .analysis import process_stays
            process_stays(self, {str(item.get("source") or "manual")[:80] for item in items})
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
        for row in rows:
            row["mode"] = mode_for(row["speed_mps"], row["accuracy_m"])
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
        for row in rows:
            row["mode"] = mode_for(row["speed_mps"], row["accuracy_m"])
        return rows

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
        rows = self.observations(limit=1)
        if not rows:
            return None
        point = rows[0]
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

    def reminders(self):
        """Active one-shot reminders waiting for their next arrival."""
        return self._rows("""SELECT r.*,COALESCE(r.destination_name,p.name) AS place_name,
            p.lat AS place_lat,p.lon AS place_lon,p.radius_m,
            COALESCE(r.destination_address,p.address) AS place_address,
            COALESCE(r.destination_category,p.category) AS category
            FROM reminders r JOIN places p ON p.id=r.place_id
            WHERE r.active=1 ORDER BY r.created_at DESC,r.id DESC""")

    def reminder_history(self, limit=200):
        return self._rows("""SELECT r.*,COALESCE(r.destination_name,p.name) AS place_name,
            COALESCE(r.destination_category,p.category) AS category,e.id AS event_id,e.triggered_at,e.webhook_status,
            CASE WHEN r.last_triggered_at IS NULL THEN 'cancelled' ELSE 'triggered' END AS status
            FROM reminders r JOIN places p ON p.id=r.place_id
            LEFT JOIN events e ON e.id=(SELECT MAX(latest.id) FROM events latest WHERE latest.reminder_id=r.id)
            WHERE r.active=0 ORDER BY COALESCE(r.last_triggered_at,r.cancelled_at,r.created_at) DESC,r.id DESC LIMIT ?""",
            (min(max(int(limit), 1), 1000),))

    def create_reminder(self, raw):
        message = str(raw.get("text") or "").strip()
        if not message or len(message) > 500:
            raise ValueError("text is required (max 500 characters)")
        if raw.get("place_id") not in (None, ""):
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
                matches = geocode(self, destination_name)
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
             destination_category,destination_provider_place_id)
            VALUES (?,?,0,1440,?,?,?,?,?) RETURNING id""",
            (message, place_id, parse_time(None), destination["name"], destination["address"],
             destination["category"], destination["provider_place_id"])).fetchone()["id"]
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
            JOIN reminders r ON r.id=e.reminder_id JOIN places p ON p.id=r.place_id
            WHERE e.id>? ORDER BY e.id DESC LIMIT ?""", (int(since_id), min(int(limit), 500)))

    def reminder_events(self, start, end, source=None):
        clauses = ["e.triggered_at>=?", "e.triggered_at<=?"]
        args = [parse_time(start), parse_time(end)]
        if source:
            clauses.append("pnt.source=?")
            args.append(source)
        return self._rows("""SELECT e.id,e.reminder_id,e.triggered_at,e.webhook_status,
            r.text,COALESCE(r.destination_name,p.name) AS place_name FROM events e JOIN reminders r ON r.id=e.reminder_id
            JOIN places p ON p.id=r.place_id JOIN points pnt ON pnt.id=e.point_id WHERE """
            + " AND ".join(clauses) + " ORDER BY e.triggered_at", args)

    def reminders_created(self, start, end):
        return self._rows("""SELECT r.id,r.text,r.created_at,r.active,r.last_triggered_at,
            COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r JOIN places p ON p.id=r.place_id
            WHERE r.created_at>=? AND r.created_at<=? ORDER BY r.created_at""",
            (parse_time(start), parse_time(end)))

    def reminders_cancelled(self, start, end):
        return self._rows("""SELECT r.id,r.text,r.cancelled_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r JOIN places p ON p.id=r.place_id
            WHERE r.cancelled_at>=? AND r.cancelled_at<=? ORDER BY r.cancelled_at""",
            (parse_time(start), parse_time(end)))

    def reminders_active_at(self, timestamp):
        at = parse_time(timestamp)
        return self._rows("""SELECT r.id,r.text,r.created_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r JOIN places p ON p.id=r.place_id WHERE r.created_at<=?
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
        at_place = [s for s in reversed(stays(self, start, end, limit=None)) if s["place_id"] == int(place_id)]
        visits = [{"arrival": s["started_at"], "departure": s["ended_at"],
                   "observed_minutes": s["observed_minutes"], "points": s["point_count"],
                   "ongoing": bool(s["ongoing"]), "source": s["source"],
                   "kind": s["kind"], "activity": s["activity"]}
                  for s in reversed(merge_split_visits(at_place, lambda s: s["place_id"]))]
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
        days = max(1, min(int(os.environ.get("GEOTRACKER_REVERSE_CACHE_DAYS", "180")), 3650))
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
    if kind in ("building", "house", "amenity", "place"):
        return 15
    if kind == "street":
        return 30
    return 0


def json_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Geotracker/0.1 (personal self-hosted location archive)"})
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


def geoapify_place_details_result(data, lat, lon):
    """Extract a named place from a coordinate based Place Details response."""
    for feature in data.get("features", []) if isinstance(data, dict) else []:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict) or props.get("feature_type") not in (None, "details"):
            continue
        name = str(props.get("name") or "").strip()
        datasource = props.get("datasource") if isinstance(props.get("datasource"), dict) else {}
        raw = datasource.get("raw") if isinstance(datasource.get("raw"), dict) else {}
        if not name:
            name = str(raw.get("name") or "").strip()
        if not name:
            continue
        address = str(props.get("formatted") or "").strip()
        if not address:
            address = ", ".join(str(props.get(key)).strip() for key in ("address_line1", "address_line2") if props.get(key))
        categories = props.get("categories") if isinstance(props.get("categories"), list) else []
        return {
            "name": name, "place_name": name, "address": address,
            "lat": lat, "lon": lon,
            "result_type": "place", "category": categories[0] if categories else None,
            "categories": categories, "provider_place_id": props.get("place_id"),
            "source": "geoapify_place_details",
        }
    return None


def geocode(store, query, lat=None, lon=None):
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
    # Cache by nearby search area; a result from another city must not win later.
    key = "search:" + query.casefold() + (f":{bias[0]:.2f}:{bias[1]:.2f}" if bias else ":unbiased")
    photon = os.environ.get("GEOTRACKER_PHOTON_URL", "").rstrip("/")
    provider = "photon:" + photon if photon else "geoapify"
    cached = store.cached(key, provider)
    if cached is not None:
        return cached
    if photon:
        params = {"q": query, "limit": 5}
        if bias:
            params.update({"lat": bias[0], "lon": bias[1]})
        data = json_get(photon + "/api?" + urllib.parse.urlencode(params))
        value = [photon_result(r) for r in data.get("features", [])]
    else:
        params = {"text": query, "limit": 5, "format": "json"}
        if bias:
            params["bias"] = f"proximity:{bias[1]},{bias[0]}"
        data = provider_request("/v1/geocode/search", params)
        value = [{"name": r.get("name") or r.get("formatted") or query,
                  "place_name": r.get("name") or "", "lat": r["lat"], "lon": r["lon"],
                  "address": r.get("formatted", ""),
                  "category": r.get("category") or ((r.get("categories") or [None])[0]),
                  "categories": r.get("categories") if isinstance(r.get("categories"), list) else [],
                  "provider_place_id": r.get("place_id")} for r in data.get("results", [])]
    store.cache(key, provider, value)
    return value


def reverse_geocode(store, lat, lon):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    photon = os.environ.get("GEOTRACKER_PHOTON_URL", "").rstrip("/")
    # Version the reverse cache because older rows stored the formatted address
    # as `name`, even when Geoapify supplied a named venue or amenity.
    if photon:
        provider = "photon:v2:" + photon
    else:
        provider = "geoapify:place-details-v1"
    # A lock coalesces simultaneous browser/MCP requests within this process.
    with _reverse_lock:
        cached = store.reverse_region(provider, lat, lon)
        if cached is not None:
            approximate = metres(lat, lon, cached["lat"], cached["lon"]) > 0.5
            return {**cached, "lat": lat, "lon": lon, "cache": "region", "approximate": approximate}
        if photon:
            data = json_get(photon + "/reverse?" + urllib.parse.urlencode({"lat": lat, "lon": lon, "limit": 1}))
            feature = data["features"][0] if data.get("features") else None
            value = photon_result(feature) if feature else {"name": "Unknown place", "place_name": "", "address": ""}
            kind = (feature or {}).get("properties", {}).get("type") or (feature or {}).get("properties", {}).get("osm_key")
            result = {**value, "name": value.get("place_name") or value.get("address") or "Unknown place",
                      "lat": lat, "lon": lon}
        else:
            try:
                place_data = provider_request("/v2/place-details", {
                    "lat": lat, "lon": lon, "features": "details",
                })
                result = geoapify_place_details_result(place_data, lat, lon)
            except (OSError, TypeError, KeyError, ValueError):
                result = None
            if result is not None:
                kind = "place"
            else:
                data = provider_request("/v1/geocode/reverse", {"lat": lat, "lon": lon, "format": "json"})
                value = (data.get("results") or [{}])[0]
                kind = value.get("result_type")
                result = {**geoapify_reverse_result(value, lat, lon), "source": "geoapify_reverse"}
        store.save_reverse_region(provider, lat, lon, reverse_cache_radius(kind), result)
        return {**result, "cache": "provider", "approximate": False}


def place_for_coordinate(store, lat, lon):
    """Return the best cached/provider place label plus its full address."""
    try:
        value = reverse_geocode(store, lat, lon)
        return {"name": str(value.get("place_name") or value.get("name") or value.get("address") or "")[:300],
                "address": str(value.get("address") or "")[:300],
                "category": str(value.get("category") or "")[:160],
                "categories": value.get("categories") if isinstance(value.get("categories"), list) else [],
                "provider_place_id": str(value.get("provider_place_id") or "")[:300]}
    except (ValueError, OSError, KeyError):
        return {"name": "", "address": "", "category": "", "categories": [], "provider_place_id": ""}


def current_location(store):
    """Most recent fix enriched with a saved or reverse-geocoded place name."""
    point = store.latest()
    if not point:
        return None
    matches = [(metres(point["lat"], point["lon"], place["lat"], place["lon"]), place)
               for place in store.places()]
    matches = [(distance, place) for distance, place in matches if distance <= place["radius_m"]]
    if matches:
        place = min(matches, key=lambda item: item[0])[1]
        point.update({"place_id": place["id"], "place_name": place["name"], "address": place["address"] or "",
                      "category": place.get("category") or "", "provider_place_id": place.get("provider_place_id") or ""})
    else:
        found = place_for_coordinate(store, point["lat"], point["lon"])
        point.update({"place_id": None, "place_name": found["name"], "address": found["address"],
                      "category": found["category"], "provider_place_id": found["provider_place_id"]})
    return point


def address_for_coordinate(store, lat, lon):
    """Use the reverse cache first, then the configured provider when available."""
    return place_for_coordinate(store, lat, lon)["address"]


def deliver_webhook(store, events):
    url = os.environ.get("GEOTRACKER_WEBHOOK_URL")
    if not url or not events:
        return
    parsed_url = urllib.parse.urlparse(url)
    local_hosts = ("127.0.0.1", "::1", "localhost", "host.docker.internal")
    if parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and parsed_url.hostname in local_hosts):
        raise ValueError("webhook URL must use HTTPS or loopback HTTP")
    for event in events:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("GEOTRACKER_WEBHOOK_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(event, separators=(",", ":")).encode()
        secret = os.environ.get("GEOTRACKER_WEBHOOK_SECRET")
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
