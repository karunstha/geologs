"""Trips of frozen days (trips): kept whole as they were found, as their raw samples are thinned."""
from __future__ import annotations

import json

from .repository import Repository


class TripsRepository(Repository):
    def save(self, trip, analysis_version, frozen_at):
        """Keep one trip as found; a trip kept already (same source and start) stays as it was."""
        self.execute("""INSERT INTO trips(source,started_at,ended_at,mode,distance_m,from_stay_id,to_stay_id,
            trip,analysis_version,frozen_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (trip["source"], trip["start"], trip["end"], trip.get("mode"), round(trip.get("distance_km", 0) * 1000, 1),
             trip.get("from_stay_id"), trip.get("to_stay_id"), json.dumps(trip, separators=(",", ":")),
             analysis_version, frozen_at))

    def between(self, start=None, end=None, source=None):
        """Kept trips overlapping a window (either end may be open), newest first, as trip dicts."""
        clauses, args = [], []
        if start:
            clauses.append("ended_at>=?"); args.append(start)
        if end:
            clauses.append("started_at<=?"); args.append(end)
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [json.loads(row["trip"]) for row in
                self.rows("SELECT trip FROM trips" + where + " ORDER BY started_at DESC", args)]

    def count(self):
        return self.execute("SELECT COUNT(*) AS n FROM trips").fetchone()["n"]

    def delete_source(self, source):
        self.execute("DELETE FROM trips WHERE source=?", (source,))
