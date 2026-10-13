"""Known paths (known_paths): routes learned from well-recorded trips."""
from __future__ import annotations

from .repository import Repository


class PathsRepository(Repository):
    def starting_between(self, start, end, source=None):
        """Paths of trips that started in a window (from one source, when given)."""
        clauses, args = ["started_at>=?", "started_at<=?"], [start, end]
        if source:
            clauses.append("source=?")
            args.append(source)
        return self.rows("SELECT * FROM known_paths WHERE " + " AND ".join(clauses), args)

    def add(self, source, started_at, ended_at, mode, distance_m, bounds, coordinates):
        """Keep a trip's path. One already kept for that trip (another request learning the
        same day just now, from the same samples) stays as it is."""
        min_lat, min_lon, max_lat, max_lon = bounds
        self.execute("""INSERT INTO known_paths
            (source,started_at,ended_at,mode,distance_m,min_lat,min_lon,max_lat,max_lon,coordinates)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,started_at) DO NOTHING""",
            (source, started_at, ended_at, mode, distance_m, min_lat, min_lon, max_lat, max_lon, coordinates))

    def delete(self, path_id):
        self.execute("DELETE FROM known_paths WHERE id=?", (path_id,))

    def delete_source(self, source):
        """Forget every path a tracker taught."""
        self.execute("DELETE FROM known_paths WHERE source=?", (source,))

    def delete_through(self, source, timestamp):
        """Forget a tracker's paths of trips that were under way at `timestamp`."""
        self.execute("DELETE FROM known_paths WHERE source=? AND started_at<=? AND ended_at>=?",
                     (source, timestamp, timestamp))

    def signature(self):
        """Changes whenever the library does, for cache keys of days that use it."""
        row = self.execute("SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS max_id FROM known_paths").fetchone()
        return f"{row['count']}:{row['max_id']}"

    def covering(self, min_lat, max_lat, min_lon, max_lon, limit):
        """The latest paths whose bounds reach over a box, newest first."""
        return self.rows("""SELECT mode,started_at,coordinates FROM known_paths
            WHERE min_lat<=? AND max_lat>=? AND min_lon<=? AND max_lon>=? ORDER BY started_at DESC LIMIT ?""",
            (min_lat, max_lat, min_lon, max_lon, limit))

    def summary_rows(self):
        return self.rows("SELECT source,mode,distance_m,started_at FROM known_paths")
