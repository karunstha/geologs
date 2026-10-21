"""Raw samples (point_observations), the points they record, and each tracker's latest point."""
from __future__ import annotations

import hashlib
import json

from ..domain.motion import mode_for
from ..domain.timing import parse_time
from .repository import Repository

# Of several samples a tracker sent for one moment, the most accurate counts.
BEST_FIXES = """SELECT * FROM (
    SELECT o.*,p.external_id,ROW_NUMBER() OVER (PARTITION BY o.source,o.timestamp
        ORDER BY COALESCE(o.accuracy_m,1000000000),o.id DESC) AS observation_rank
    FROM point_observations o JOIN points p ON p.id=o.point_id WHERE {where}) ranked
    WHERE observation_rank=1 ORDER BY timestamp,id"""


class PointsRepository(Repository):
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
        return self.rows("SELECT id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode FROM points" + where + " ORDER BY timestamp DESC" + suffix, args)

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
        rows = self.rows("""SELECT o.id,o.point_id,p.external_id,o.source,o.timestamp,o.lat,o.lon,o.accuracy_m,
            o.speed_mps,o.speed_origin,
            o.received_at FROM point_observations o JOIN points p ON p.id=o.point_id""" + where + " ORDER BY o.timestamp DESC,COALESCE(o.accuracy_m,1000000000) ASC,o.id DESC" + suffix, args)
        speeds = self.db.settings.get()
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
        rows = self.rows("""SELECT o.id,o.point_id,p.external_id,o.source,o.timestamp,o.lat,o.lon,
            o.accuracy_m,o.speed_mps,o.speed_origin,o.received_at,t.role,t.stay_id
            FROM timeline_fixes t JOIN point_observations o ON o.id=t.observation_id
            JOIN points p ON p.id=o.point_id""" + where + " ORDER BY o.timestamp DESC,o.id DESC" + suffix, args)
        speeds = self.db.settings.get()
        for row in rows:
            row["mode"] = mode_for(row["speed_mps"], row["accuracy_m"], speeds)
        return rows

    def count(self, start=None, end=None, source=None):
        clauses, args = [], []
        if start:
            clauses.append("timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.execute("SELECT COUNT(*) AS n FROM point_observations" + where, args).fetchone()["n"]

    def local_dates(self, start, end, timezone_name):
        """The local dates (YYYY-MM-DD, in a timezone) that have a sample between two instants."""
        return [row["day"] for row in self.rows("""SELECT DISTINCT to_char(timestamp::timestamptz AT TIME ZONE ?, 'YYYY-MM-DD') AS day
            FROM point_observations WHERE timestamp>=? AND timestamp<=? ORDER BY day""",
            (timezone_name, parse_time(start), parse_time(end)))]

    def nearest_in_time(self, timestamp):
        at = parse_time(timestamp)
        columns = "id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode"
        rows = self.rows(f"SELECT {columns}, ABS(EXTRACT(EPOCH FROM timestamp::timestamptz - ?::timestamptz))::double precision AS seconds_away FROM points ORDER BY seconds_away LIMIT 1", (at,))
        return rows[0] if rows else None

    def coverage(self, gaps=True):
        """Each source's sample count and span, and (with `gaps`) its silences of over a day, which
        read every sample in order and so cost the most on a long archive."""
        sources = self.rows("SELECT source,COUNT(*) AS points,MIN(timestamp) AS first,MAX(timestamp) AS last FROM point_observations GROUP BY source ORDER BY source")
        if not gaps:
            return {"sources": sources}
        gaps = self.rows("""SELECT source, previous AS start, timestamp AS end,
            ROUND(((EXTRACT(EPOCH FROM timestamp::timestamptz)-EXTRACT(EPOCH FROM previous::timestamptz))/3600)::numeric,1)::double precision AS hours
            FROM (SELECT source,timestamp,LAG(timestamp) OVER
                  (PARTITION BY source ORDER BY timestamp) AS previous FROM point_observations) p
            WHERE previous IS NOT NULL AND timestamp::timestamptz-previous::timestamptz>interval '24 hours'
            ORDER BY timestamp DESC LIMIT 200""")
        return {"sources": sources, "gaps_over_24h": gaps}

    def watermark(self, start=None, end=None, source=None):
        clauses, args = [], []
        if start:
            clauses.append("timestamp>=?"); args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.execute("SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS max_id FROM point_observations" + where, args).fetchone()
        return f"{row['count']}:{row['max_id']}"

    def near(self, lat, lon, radius_m, start=None, end=None, limit=100, source=None):
        """Points within radius_m of a position, newest first, each with its distance."""
        clauses = ["ST_DWithin(position, ST_SetSRID(ST_MakePoint(?,?),4326)::geography, ?)"]
        where_args = [lon, lat, radius_m]
        if start:
            clauses.append("timestamp>=?"); where_args.append(parse_time(start))
        if end:
            clauses.append("timestamp<=?"); where_args.append(parse_time(end))
        if source:
            clauses.append("source=?"); where_args.append(source)
        return self.rows("SELECT id,source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode, "
                         "ROUND(ST_Distance(position,ST_SetSRID(ST_MakePoint(?,?),4326)::geography)) AS distance_m "
                         "FROM points WHERE " + " AND ".join(clauses) + " ORDER BY timestamp DESC LIMIT ?",
                         [lon, lat] + where_args + [min(int(limit), 500)])

    def sources(self):
        """Every tracker that has sent a sample."""
        return [row["source"] for row in self.rows("SELECT DISTINCT source FROM point_observations")]

    def span(self):
        """The first and last sample's timestamps (None and None when there are none)."""
        row = self.execute("SELECT MIN(timestamp) AS first,MAX(timestamp) AS last FROM point_observations").fetchone()
        return row["first"], row["last"]

    def activity(self, since):
        """Every source: how much it sent, its newest fix, when that arrived, and fixes since `since`."""
        return self.rows("""SELECT o.source,o.fixes,o.first_fix,o.last_fix,r.received_at AS last_received,
            (SELECT COUNT(*) FROM point_observations x WHERE x.source=o.source AND x.timestamp>=?) AS fixes_24h
            FROM (SELECT source,COUNT(*) AS fixes,MIN(timestamp) AS first_fix,MAX(timestamp) AS last_fix,MAX(id) AS last_id
                  FROM point_observations GROUP BY source) o
            JOIN point_observations r ON r.id=o.last_id ORDER BY r.received_at DESC""", (since,))

    def newest_fix(self, source):
        """The time of a source's newest fix, or None (an index lookup, cheap enough to ask every minute)."""
        row = self.execute("SELECT MAX(timestamp) AS last_fix FROM point_observations WHERE source=?", (source,)).fetchone()
        return row["last_fix"] if row else None

    # Recording a sample (see services.ingest).

    def live_point(self, source):
        """The point a source's geofences were last decided on."""
        return self.one("""SELECT p.id,p.source,p.external_id,p.timestamp,p.lat,p.lon,p.accuracy_m,p.speed_mps,
            p.speed_origin,p.mode FROM source_state s JOIN points p ON p.id=s.point_id WHERE s.source=?""", (source,))

    def prior_fix(self, source, timestamp):
        return self.one("""SELECT timestamp,lat,lon,accuracy_m FROM point_observations
            WHERE source=? AND timestamp<? ORDER BY timestamp DESC,id DESC LIMIT 1""", (source, timestamp))

    def insert_point(self, source, external_id, timestamp, lat, lon, accuracy, speed, origin, mode):
        """The new point's id, or None when this source already has it."""
        row = self.execute("""INSERT INTO points
            (source, external_id, timestamp, lat, lon, accuracy_m, speed_mps, speed_origin, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id""",
            (source, external_id, timestamp, lat, lon, accuracy, speed, origin, mode)).fetchone()
        return row["id"] if row else None

    def existing_point(self, source, external_id, timestamp, lat, lon):
        """The id of the point a duplicate sample recorded: by its external id, else its position and time."""
        existing = None
        if external_id is not None:
            existing = self.execute("SELECT id FROM points WHERE source=? AND external_id=?", (source, external_id)).fetchone()
        if existing is None:
            existing = self.execute("SELECT id FROM points WHERE source=? AND timestamp=? AND lat=? AND lon=?",
                                    (source, timestamp, lat, lon)).fetchone()
        return existing["id"] if existing else None

    def legacy_observation(self, point_id):
        return self.one("""SELECT timestamp,lat,lon,accuracy_m,speed_mps,speed_origin
            FROM point_observations WHERE legacy_point_id=?""", (point_id,))

    def insert_observation(self, point_id, source, timestamp, lat, lon, accuracy, speed, origin, received_at, identity):
        """Record one received sample; a retry of the same sample (same `identity`) records nothing. Returns 1 or 0."""
        fingerprint = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        return self.execute("""INSERT INTO point_observations
            (point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,received_at,fingerprint)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,fingerprint) DO NOTHING""",
            (point_id, source, timestamp, lat, lon, accuracy, speed, origin, received_at, fingerprint)).rowcount

    def set_live_point(self, source, point_id):
        self.execute("INSERT INTO source_state(source,point_id) VALUES (?,?) ON CONFLICT(user_id,source) DO UPDATE SET point_id=excluded.point_id",
                     (source, point_id))

    def lock_source(self, source):
        """Serialize a source's previous-fix and reminder decisions across workers (a user's own source)."""
        self.execute("SELECT pg_advisory_xact_lock(hashtext(geologs_user()::text || ':' || ?))", (source,))

    # Reading samples for stays (see services.stays).

    def first_new(self, source, after_id):
        """The earliest timestamp among a source's samples newer than after_id."""
        # MIN over the id range reads only the new samples (point_observations_source_id).
        return self.execute("""SELECT MIN(timestamp) AS timestamp FROM point_observations
            WHERE source=? AND id>?""", (source, after_id)).fetchone()["timestamp"]

    def max_id(self, source, default):
        return self.execute("SELECT COALESCE(MAX(id),?) AS id FROM point_observations WHERE source=?",
                            (default, source)).fetchone()["id"]

    def best_fixes_after(self, source, after_id, batch=1000):
        """A source's samples newer than after_id, one per moment, in time order, in batches."""
        cursor = self.execute(BEST_FIXES.format(where="o.source=? AND o.id>?"), (source, after_id))
        while rows := cursor.fetchmany(batch):
            yield [dict(row) for row in rows]

    def best_fixes_after_time(self, source, after, batch=1000):
        """A source's samples after a moment, one per moment, in time order, in batches."""
        cursor = self.execute(BEST_FIXES.format(where="o.source=? AND o.timestamp>?"), (source, after))
        while rows := cursor.fetchmany(batch):
            yield [dict(row) for row in rows]

    def best_fixes_between(self, source, after=None, before=None):
        """A source's samples strictly between two times (either may be open), one per moment."""
        clauses, args = ["o.source=?"], [source]
        if after:
            clauses.append("o.timestamp>?"); args.append(after)
        if before:
            clauses.append("o.timestamp<?"); args.append(before)
        return self.rows("""SELECT * FROM (
            SELECT o.id,o.timestamp,o.lat,o.lon,o.accuracy_m,o.speed_mps,o.speed_origin,
                ROW_NUMBER() OVER (PARTITION BY o.source,o.timestamp
                    ORDER BY COALESCE(o.accuracy_m,1000000000),o.id DESC) AS observation_rank
            FROM point_observations o WHERE """ + " AND ".join(clauses) + """) ranked
            WHERE observation_rank=1 ORDER BY timestamp,id""", args)

    def point_of(self, observation_id):
        row = self.execute("SELECT point_id FROM point_observations WHERE id=?", (observation_id,)).fetchone()
        return row["point_id"] if row else None

    def accurate_positions(self, source, start, end):
        """Positions of a source's accurate samples (within 100 m) in a window."""
        return self.rows("""SELECT lat,lon FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
            AND (accuracy_m IS NULL OR accuracy_m<=100)""", (source, start, end))

    # Deleting samples (see services.samples).

    def deletion_plan(self, ids):
        """What deleting these samples would remove: their points that no other sample records."""
        marks = ",".join("?" * len(ids))
        rows = self.rows(f"SELECT id,point_id,source,timestamp,lat,lon FROM point_observations WHERE id IN ({marks})", ids)
        if not rows:
            return None
        point_ids = sorted({row["point_id"] for row in rows})
        point_marks = ",".join("?" * len(point_ids))
        # Points that keep another sample stay; the rest are removed with their samples.
        kept = {row["point_id"] for row in self.rows(f"""SELECT DISTINCT point_id FROM point_observations
            WHERE point_id IN ({point_marks}) AND id NOT IN ({marks})""", point_ids + ids)}
        timestamps = sorted(row["timestamp"] for row in rows)
        return {"ids": [row["id"] for row in rows], "samples": len(rows),
                "points": [point_id for point_id in point_ids if point_id not in kept],
                "sources": sorted({row["source"] for row in rows}), "first": timestamps[0], "last": timestamps[-1],
                "timestamps": timestamps,
                "fixes": [{key: row[key] for key in ("source", "timestamp", "lat", "lon")} for row in rows]}

    def source_summary(self, source):
        """A tracker's sample count and span; None without samples."""
        row = self.one("""SELECT COUNT(*) AS samples,MIN(timestamp) AS first,MAX(timestamp) AS last
            FROM point_observations WHERE source=?""", (source,))
        return {"source": source, **row} if row["samples"] else None

    def delete_source(self, source):
        """Remove a tracker's samples and points (the rest is each repository's own; see services.samples)."""
        self.execute("DELETE FROM observed_stays WHERE source=?", (source,))
        self.execute("DELETE FROM point_observations WHERE source=?", (source,))
        self.execute("DELETE FROM source_state WHERE source=?", (source,))
        self.execute("DELETE FROM points WHERE source=?", (source,))

    def delete_observations(self, ids):
        self.execute(f"DELETE FROM point_observations WHERE id IN ({','.join('?' * len(ids))})", ids)

    def delete_points(self, point_ids):
        """Remove points no sample records any more; a source's latest point moves back to the newest one left."""
        marks = ",".join("?" * len(point_ids))
        self.execute(f"DELETE FROM observed_stays WHERE start_point_id IN ({marks}) OR end_point_id IN ({marks})",
                     point_ids + point_ids)
        for row in self.rows(f"SELECT source FROM source_state WHERE point_id IN ({marks})", point_ids):
            latest = self.one(f"""SELECT id FROM points WHERE source=? AND id NOT IN ({marks})
                ORDER BY timestamp DESC,id DESC LIMIT 1""", [row["source"], *point_ids])
            if latest:
                self.execute("UPDATE source_state SET point_id=? WHERE source=?", (latest["id"], row["source"]))
            else:
                self.execute("DELETE FROM source_state WHERE source=?", (row["source"],))
        self.execute(f"DELETE FROM points WHERE id IN ({marks})", point_ids)
