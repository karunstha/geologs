"""Detected stays (observed_stays_v2) and each source's open stay candidate (stay_observation_state)."""
from __future__ import annotations

import json

from ..domain.timing import parse_time
from .repository import Repository


class StaysRepository(Repository):
    def between(self, start=None, end=None, source=None, limit=500):
        """Stays overlapping a window, newest first, each with the id of its source's stay before it.

        previous_stay_id lets callers tell whether went_elsewhere refers to the stay they hold.
        """
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
        return self.rows("""SELECT * FROM (SELECT s.*,LAG(s.id) OVER (PARTITION BY s.source ORDER BY s.started_at,s.id)
            AS previous_stay_id FROM observed_stays_v2 s""" + where + ") ordered ORDER BY started_at DESC" + suffix, args)

    def save_detected(self, source, start_id, end_id, started_at, ended_at, lat, lon, count, ongoing, kind):
        """Create or update the stay a candidate's first sample starts."""
        self.execute("""INSERT INTO observed_stays_v2
            (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing,kind)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(start_observation_id) DO UPDATE SET
            end_observation_id=excluded.end_observation_id,ended_at=excluded.ended_at,
            lat=excluded.lat,lon=excluded.lon,point_count=excluded.point_count,ongoing=excluded.ongoing,
            kind=excluded.kind,activity=NULL,distance_m=NULL,radius_m=NULL""",
            (source, start_id, end_id, started_at, ended_at, lat, lon, count, int(ongoing), kind))

    def withdraw(self, start_id):
        """Drop the stay a sample starts (a quick stop that turned out to be traffic)."""
        self.execute("DELETE FROM observed_stays_v2 WHERE start_observation_id=?", (start_id,))

    def delete_source(self, source):
        """Forget a source's stays and its open candidate, to find them again."""
        self.execute("DELETE FROM observed_stays_v2 WHERE source=?", (source,))
        self.execute("DELETE FROM stay_observation_state WHERE source=?", (source,))

    def last_end(self, source, kind, before=None, closed=False):
        """When a source's latest stay of a kind ended (on or before `before`; only closed ones with `closed`)."""
        query, args = "SELECT MAX(ended_at) AS ended_at FROM observed_stays_v2 WHERE source=? AND kind=?", [source, kind]
        if before is not None:
            query += " AND ended_at<=?"
            args.append(before)
        if closed:
            query += " AND ongoing=0"
        return self.execute(query, args).fetchone()["ended_at"]

    # Area stays (see services.stays).

    def delete_areas(self, source, since=None):
        if since is None:
            self.execute("DELETE FROM observed_stays_v2 WHERE source=? AND kind='area'", (source,))
        else:
            self.execute("DELETE FROM observed_stays_v2 WHERE source=? AND kind='area' AND started_at>=?", (source, since))

    def stationary(self, source, since=None):
        """A source's stationary stays (ending at or after `since`), oldest first."""
        query = "SELECT id,started_at,ended_at,lat,lon,radius_m FROM observed_stays_v2 WHERE source=? AND kind='stationary'"
        args = [source]
        if since is not None:
            query += " AND ended_at>=?"
            args.append(since)
        return self.rows(query + " ORDER BY started_at,id", args)

    def last_anchor_before(self, source, since):
        """The latest stay (not a quick stop) that ended by `since`, or None."""
        return next(iter(self.rows("""SELECT id,started_at,ended_at,lat,lon,radius_m FROM observed_stays_v2
            WHERE source=? AND ended_at<=? AND kind NOT IN ('stop','pause') ORDER BY ended_at DESC,id DESC LIMIT 1""",
            (source, since))), None)

    def delete_quick_stops_within(self, source, started_at, ended_at):
        self.execute("""DELETE FROM observed_stays_v2 WHERE source=? AND kind IN ('stop','pause')
            AND started_at<=? AND ended_at>=?""", (source, ended_at, started_at))

    def add_area(self, source, window, went_elsewhere):
        self.execute("""INSERT INTO observed_stays_v2
            (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing,
             kind,activity,distance_m,radius_m,went_elsewhere)
            VALUES (?,?,?,?,?,?,?,?,?,'area',?,?,?,?) ON CONFLICT(start_observation_id) DO NOTHING""",
            (source, window["start_id"], window["end_id"], window["started_at"], window["ended_at"],
             window["lat"], window["lon"], window["point_count"], int(window["ongoing"]), window["activity"],
             round(window["distance_m"], 1), round(window["radius_m"], 1), went_elsewhere))

    def set_went_elsewhere(self, stay_id, went_elsewhere):
        self.execute("UPDATE observed_stays_v2 SET went_elsewhere=? WHERE id=?", (went_elsewhere, stay_id))

    # The open candidate: where each source's stay detection left off.

    def state(self, source):
        """{last_observation_id, last_timestamp, candidate} for a source, or None before its first sample."""
        row = self.one("SELECT last_observation_id,last_timestamp,candidate FROM stay_observation_state WHERE source=?",
                       (source,))
        if row is None:
            return None
        return {"last_observation_id": row["last_observation_id"], "last_timestamp": row["last_timestamp"],
                "candidate": json.loads(row["candidate"]) if row["candidate"] else None}

    def save_state(self, source, last_id, last_timestamp, candidate):
        self.execute("""INSERT INTO stay_observation_state(source,last_observation_id,last_timestamp,candidate)
            VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            last_observation_id=excluded.last_observation_id,last_timestamp=excluded.last_timestamp,candidate=excluded.candidate""",
            (source, last_id, last_timestamp, json.dumps(candidate)))

    def nearby_names(self, source, start_id):
        """The place names looked up around a source's open stop (the one that starts at start_id), or None."""
        row = self.one("SELECT nearby FROM stay_observation_state WHERE source=?", (source,))
        nearby = json.loads(row["nearby"]) if row and row["nearby"] else None
        return nearby["names"] if nearby and nearby["start_id"] == start_id else None

    def save_nearby_names(self, source, start_id, names):
        """Remember the place names around an open stop; a new stop starts afresh."""
        self.execute("UPDATE stay_observation_state SET nearby=? WHERE source=?",
                     (json.dumps({"start_id": start_id, "names": names}), source))
