"""Location domain model with SQLite and PostgreSQL/PostGIS storage."""
from __future__ import annotations

import csv
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

from .postgres import PostgresConnection


_reverse_lock = threading.Lock()


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
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


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
        return "cycling"
    return "driving" if kmh < 200 else "unknown"


def stats_from_points(points):
    """Summarize chronological fixes without bridging source or coverage gaps."""
    distance, moving_seconds, by_mode = 0, 0, {}
    for i, point in enumerate(points):
        by_mode[point["mode"]] = by_mode.get(point["mode"], 0) + 1
        if i == 0:
            continue
        prev = points[i - 1]
        elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(prev["timestamp"])).total_seconds()
        segment = metres(prev["lat"], prev["lon"], point["lat"], point["lon"])
        if point["source"] == prev["source"] and 0 < elapsed <= 900 and segment / elapsed <= 55.6 and (point["accuracy_m"] is None or point["accuracy_m"] <= 150):
            distance += segment
            if point["mode"] in ("walking", "cycling", "driving"):
                moving_seconds += elapsed
    return {"points": len(points), "distance_km": round(distance / 1000, 2), "moving_hours": round(moving_seconds / 3600, 2), "modes": by_mode,
            "first": points[0]["timestamp"] if points else None, "last": points[-1]["timestamp"] if points else None}


def trips_from_points(points):
    """Build movement runs from chronological fixes, with no lines across gaps."""
    trips, current = [], None
    for previous, point in zip(points, points[1:]):
        elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(previous["timestamp"])).total_seconds()
        distance = metres(previous["lat"], previous["lon"], point["lat"], point["lon"])
        moving = previous["source"] == point["source"] and 0 < elapsed <= 900 and point["mode"] in ("walking", "cycling", "driving") and distance / elapsed <= 55.6
        if moving and current and point["mode"] not in current["modes"]:
            current["distance_km"] = round(current["distance_km"], 2)
            current["mode"] = max(current["modes"], key=current["modes"].get)
            trips.append(current)
            current = None
        if moving:
            if current is None:
                current = {"start": previous["timestamp"], "end": point["timestamp"], "distance_km": 0, "modes": {}, "coordinates": [[previous["lon"], previous["lat"]]]}
            current["end"] = point["timestamp"]
            current["distance_km"] += distance / 1000
            current["modes"][point["mode"]] = current["modes"].get(point["mode"], 0) + 1
            current["coordinates"].append([point["lon"], point["lat"]])
        elif current:
            current["distance_km"] = round(current["distance_km"], 2)
            current["mode"] = max(current["modes"], key=current["modes"].get)
            trips.append(current)
            current = None
    if current:
        current["distance_km"] = round(current["distance_km"], 2)
        current["mode"] = max(current["modes"], key=current["modes"].get)
        trips.append(current)
    return list(reversed(trips))


def route_segments_from_points(points):
    """Connect consecutive plausible fixes, regardless of movement classification."""
    segments, current = [], []
    previous = None
    for point in points:
        connected = False
        if previous:
            elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(previous["timestamp"])).total_seconds()
            distance = metres(previous["lat"], previous["lon"], point["lat"], point["lon"])
            accurate = all(p["accuracy_m"] is None or p["accuracy_m"] <= 150 for p in (previous, point))
            connected = point["source"] == previous["source"] and 0 < elapsed <= 900 and distance / elapsed <= 55.6 and accurate
        if not connected:
            if len(current) > 1:
                segments.append(current)
            current = [[point["lon"], point["lat"]]]
        else:
            current.append([point["lon"], point["lat"]])
        previous = point
    if len(current) > 1:
        segments.append(current)
    return segments


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
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 150,
            address TEXT
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER NOT NULL REFERENCES places(id),
            active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
            cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
            created_at TEXT NOT NULL, last_triggered_at TEXT,
            FOREIGN KEY(place_id) REFERENCES places(id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL REFERENCES reminders(id),
            point_id INTEGER NOT NULL REFERENCES points(id), triggered_at TEXT NOT NULL,
            acknowledged_at TEXT, webhook_status TEXT DEFAULT 'pending'
        );
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
        if not self.db.execute("SELECT 1 FROM stay_processing_state LIMIT 1").fetchone() and self.db.execute("SELECT 1 FROM points LIMIT 1").fetchone():
            from .analysis import process_stays
            process_stays(self)
        self.db.commit()

    def close(self):
        self.db.close()

    def _rows(self, query, args=()):
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def ingest(self, payload, trigger_reminders=True, process_analysis=True):
        items = payload if isinstance(payload, list) else [payload]
        if len(items) > 1000:
            raise ValueError("maximum 1000 points per request")
        if self.postgres:
            # Serialize each source's previous-fix and reminder decisions across
            # concurrent HTTP workers. Sorting prevents opposing batch lock order.
            for source in sorted({str(item.get("source") or "manual")[:80] for item in items}):
                self.db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (source,))
        inserted, duplicate, events = 0, 0, []
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
            speed = raw.get("speed_mps")
            speed = float(speed) if speed is not None else None
            if speed is not None and (not math.isfinite(speed) or speed < 0):
                raise ValueError("speed_mps must be nonnegative")
            previous = self.db.execute("""SELECT p.* FROM source_state s JOIN points p ON p.id=s.point_id WHERE s.source=?""", (source,)).fetchone()
            origin = "reported" if speed is not None else None
            if speed is None and previous:
                elapsed = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(previous["timestamp"])).total_seconds()
                if 5 <= elapsed <= 900 and (accuracy is None or accuracy <= 150) and (previous["accuracy_m"] is None or previous["accuracy_m"] <= 150):
                    estimated = metres(lat, lon, previous["lat"], previous["lon"]) / elapsed
                    if estimated <= 55.6:
                        speed, origin = estimated, "estimated"
            mode = mode_for(speed, accuracy)
            external_id = raw.get("external_id")
            cursor = self.db.execute("""INSERT INTO points
                (source, external_id, timestamp, lat, lon, accuracy_m, speed_mps, speed_origin, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id""",
                (source, str(external_id) if external_id is not None else None, timestamp, lat, lon, accuracy, speed, origin, mode))
            inserted_row = cursor.fetchone()
            if not inserted_row:
                duplicate += 1
                continue
            inserted += 1
            point_id = inserted_row["id"]
            # Late imports are stored but do not rewind the live geofence state.
            if previous and timestamp <= previous["timestamp"]:
                continue
            self.db.execute("INSERT INTO source_state(source,point_id) VALUES (?,?) ON CONFLICT(source) DO UPDATE SET point_id=excluded.point_id", (source, point_id))
            if not trigger_reminders or abs((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()) > 1800:
                continue
            for reminder in self._rows("""SELECT r.*, p.lat AS place_lat, p.lon AS place_lon, p.radius_m,
                p.name AS place_name FROM reminders r JOIN places p ON p.id=r.place_id WHERE r.active=1"""):
                if accuracy is not None and accuracy > max(150, reminder["radius_m"]):
                    continue
                inside = metres(lat, lon, reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
                # A stale previous fix cannot prove the user remained inside.
                recent_previous = previous and (datetime.fromisoformat(timestamp) - datetime.fromisoformat(previous["timestamp"])).total_seconds() <= 1800
                was_inside = recent_previous and metres(previous["lat"], previous["lon"], reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
                if not inside or was_inside:
                    continue
                last = reminder["last_triggered_at"]
                if last and (datetime.fromisoformat(timestamp) - datetime.fromisoformat(last)).total_seconds() < reminder["cooldown_minutes"] * 60:
                    continue
                event_id = self.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?) RETURNING id", (reminder["id"], point_id, timestamp)).fetchone()["id"]
                self.db.execute("UPDATE reminders SET last_triggered_at=?,active=? WHERE id=?", (timestamp, int(bool(reminder["repeat"])), reminder["id"]))
                events.append({"id": event_id, "reminder_id": reminder["id"], "text": reminder["text"], "place": reminder["place_name"], "triggered_at": timestamp})
        if inserted and process_analysis:
            from .analysis import process_stays
            process_stays(self, {str(item.get("source") or "manual")[:80] for item in items})
        self.db.commit()
        return {"inserted": inserted, "duplicates": duplicate, "events": events}

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

    def latest(self):
        rows = self._rows("SELECT id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode FROM points ORDER BY timestamp DESC LIMIT 1")
        if not rows:
            return None
        point = rows[0]
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

    def places(self):
        return self._rows("SELECT id,name,lat,lon,radius_m,address FROM places ORDER BY name")

    def save_place(self, raw):
        name = str(raw.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("name is required (max 120 characters)")
        lat, lon = float(raw["lat"]), float(raw["lon"])
        radius = float(raw.get("radius_m", 150))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 5000):
            raise ValueError("invalid place coordinates or radius (20–5000 m)")
        address = str(raw.get("address") or "")[:300]
        if raw.get("id"):
            updated = self.db.execute("UPDATE places SET name=?,lat=?,lon=?,radius_m=?,address=? WHERE id=?", (name, lat, lon, radius, address, int(raw["id"])))
            if not updated.rowcount:
                raise ValueError("place not found")
            id_ = int(raw["id"])
        else:
            id_ = self.db.execute("INSERT INTO places(name,lat,lon,radius_m,address) VALUES (?,?,?,?,?) RETURNING id", (name, lat, lon, radius, address)).fetchone()["id"]
        self.db.commit()
        return self._rows("SELECT id,name,lat,lon,radius_m,address FROM places WHERE id=?", (id_,))[0]

    def reminders(self):
        return self._rows("""SELECT r.*, p.name AS place_name FROM reminders r JOIN places p ON p.id=r.place_id ORDER BY r.active DESC,r.id DESC""")

    def create_reminder(self, raw):
        message = str(raw.get("text") or "").strip()
        if not message or len(message) > 500:
            raise ValueError("text is required (max 500 characters)")
        place_id = int(raw["place_id"])
        if not self.db.execute("SELECT 1 FROM places WHERE id=?", (place_id,)).fetchone():
            raise ValueError("place not found")
        repeat = int(bool(raw.get("repeat", False)))
        cooldown = int(raw.get("cooldown_minutes", 1440))
        if not 1 <= cooldown <= 525600:
            raise ValueError("cooldown_minutes must be 1–525600")
        id_ = self.db.execute("INSERT INTO reminders(text,place_id,repeat,cooldown_minutes,created_at) VALUES (?,?,?,?,?) RETURNING id", (message, place_id, repeat, cooldown, parse_time(None))).fetchone()["id"]
        self.db.commit()
        return self._rows("SELECT * FROM reminders WHERE id=?", (id_,))[0]

    def set_reminder_active(self, id_, active):
        result = self.db.execute("UPDATE reminders SET active=? WHERE id=?", (int(bool(active)), int(id_)))
        self.db.commit()
        if not result.rowcount:
            raise ValueError("reminder not found")
        return {"updated": result.rowcount}

    def events(self, since_id=0, limit=100):
        return self._rows("""SELECT e.*, r.text, p.name AS place_name FROM events e
            JOIN reminders r ON r.id=e.reminder_id JOIN places p ON p.id=r.place_id
            WHERE e.id>? ORDER BY e.id DESC LIMIT ?""", (int(since_id), min(int(limit), 500)))

    def acknowledge(self, id_):
        result = self.db.execute("UPDATE events SET acknowledged_at=? WHERE id=?", (parse_time(None), int(id_)))
        self.db.commit()
        return {"updated": result.rowcount}

    def stats(self, start=None, end=None, source=None):
        return stats_from_points(list(reversed(self.points(start, end, source=source, limit=None))))

    def trips(self, start=None, end=None, source=None):
        """Movement runs, split by pauses and gaps. Estimates from samples, not map matching."""
        return trips_from_points(list(reversed(self.points(start, end, source=source, limit=None))))

    def export_geojson(self, start=None, end=None):
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": {k: v for k, v in p.items() if k not in ("lat", "lon")}} for p in self.points(start, end, limit=None)]}

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
        from .analysis import stays
        visits = [{"arrival": s["started_at"], "departure": s["ended_at"],
                   "observed_minutes": s["observed_minutes"], "points": s["point_count"],
                   "ongoing": bool(s["ongoing"]), "source": s["source"]}
                  for s in stays(self, start, end, limit=None) if s["place_id"] == int(place_id)]
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
        sources = self._rows("SELECT source,COUNT(*) AS points,MIN(timestamp) AS first,MAX(timestamp) AS last FROM points GROUP BY source ORDER BY source")
        if self.postgres:
            gaps = self._rows("""SELECT source, previous AS start, timestamp AS end,
                ROUND(((EXTRACT(EPOCH FROM timestamp::timestamptz)-EXTRACT(EPOCH FROM previous::timestamptz))/3600)::numeric,1)::double precision AS hours
                FROM (SELECT source,timestamp,LAG(timestamp) OVER
                      (PARTITION BY source ORDER BY timestamp) AS previous FROM points) p
                WHERE previous IS NOT NULL AND timestamp::timestamptz-previous::timestamptz>interval '24 hours'
                ORDER BY timestamp DESC LIMIT 200""")
        else:
            gaps = self._rows("""SELECT source, previous AS start, timestamp AS end,
            ROUND((strftime('%s',timestamp)-strftime('%s',previous))/3600.0,1) AS hours
            FROM (SELECT source,timestamp,LAG(timestamp) OVER
                  (PARTITION BY source ORDER BY timestamp) AS previous FROM points)
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
    if kind in ("building", "house", "amenity"):
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
    return {"name": label or "Unknown place", "lat": lat, "lon": lon, "address": label}


def geocode(store, query):
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError("query is required (max 200 characters)")
    key = "search:" + query.casefold()
    photon = os.environ.get("GEOTRACKER_PHOTON_URL", "").rstrip("/")
    provider = "photon:" + photon if photon else "geoapify"
    cached = store.cached(key, provider)
    if cached is not None:
        return cached
    if photon:
        data = json_get(photon + "/api?" + urllib.parse.urlencode({"q": query, "limit": 5}))
        value = [photon_result(r) for r in data.get("features", [])]
    else:
        data = provider_request("/v1/geocode/search", {"text": query, "limit": 5, "format": "json"})
        value = [{"name": r.get("formatted", query), "lat": r["lat"], "lon": r["lon"], "address": r.get("formatted", "")} for r in data.get("results", [])]
    store.cache(key, provider, value)
    return value


def reverse_geocode(store, lat, lon):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    photon = os.environ.get("GEOTRACKER_PHOTON_URL", "").rstrip("/")
    provider = "photon:" + photon if photon else "geoapify"
    # A lock coalesces simultaneous browser/MCP requests within this process.
    with _reverse_lock:
        cached = store.reverse_region(provider, lat, lon)
        if cached is not None:
            return {**cached, "lat": lat, "lon": lon, "cache": "region", "approximate": True}
        if photon:
            data = json_get(photon + "/reverse?" + urllib.parse.urlencode({"lat": lat, "lon": lon, "limit": 1}))
            feature = data["features"][0] if data.get("features") else None
            value = photon_result(feature) if feature else {"name": "Unknown address", "address": ""}
            kind = (feature or {}).get("properties", {}).get("type") or (feature or {}).get("properties", {}).get("osm_key")
            result = {**value, "lat": lat, "lon": lon}
        else:
            data = provider_request("/v1/geocode/reverse", {"lat": lat, "lon": lon, "format": "json"})
            value = (data.get("results") or [{}])[0]
            kind = value.get("result_type")
            result = {"name": value.get("formatted", "Unknown address"), "address": value.get("formatted", ""), "lat": lat, "lon": lon}
        store.save_reverse_region(provider, lat, lon, reverse_cache_radius(kind), result)
        return {**result, "cache": "provider", "approximate": False}


def deliver_webhook(store, events):
    url = os.environ.get("GEOTRACKER_WEBHOOK_URL")
    if not url or not events:
        return
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        raise ValueError("webhook URL must use HTTPS or loopback HTTP")
    for event in events:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("GEOTRACKER_WEBHOOK_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, json.dumps(event).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=8):
                status = "sent"
        except Exception as exc:
            status = "failed: " + str(exc)[:150]
        store.db.execute("UPDATE events SET webhook_status=? WHERE id=?", (status, event["id"]))
    store.db.commit()
