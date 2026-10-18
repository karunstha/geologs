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
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,source,started_at) DO NOTHING""",
            (source, started_at, ended_at, mode, distance_m, min_lat, min_lon, max_lat, max_lon, coordinates))

    def delete(self, path_id):
        self.execute("DELETE FROM known_paths WHERE id=?", (path_id,))

    def delete_source(self, source):
        """Forget every path a tracker taught, and the ones you excluded from it."""
        self.execute("DELETE FROM known_paths WHERE source=?", (source,))
        self.execute("DELETE FROM path_exclusions WHERE source=?", (source,))

    def get(self, source, started_at):
        return self.one("""SELECT id,source,started_at,ended_at,mode,distance_m FROM known_paths
            WHERE source=? AND started_at=?""", (source, started_at))

    def exclude(self, path, now):
        """Never learn this trip's path again (kept with what it was, to list and undo)."""
        self.execute("""INSERT INTO path_exclusions(source,started_at,ended_at,mode,distance_m,created_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,source,started_at) DO NOTHING""",
            (path["source"], path["started_at"], path["ended_at"], path["mode"], path["distance_m"], now))

    def include(self, source, started_at):
        """Learn this trip's path again; False when it was not excluded."""
        return bool(self.execute("DELETE FROM path_exclusions WHERE source=? AND started_at=?",
                                 (source, started_at)).rowcount)

    def excluded(self):
        return self.rows("""SELECT source,started_at,ended_at,mode,distance_m,created_at FROM path_exclusions
            ORDER BY started_at DESC""")

    def excluded_trips(self, start, end, source=None):
        """(source, started_at) of excluded trips that started in a window."""
        query, args = "SELECT source,started_at FROM path_exclusions WHERE started_at>=? AND started_at<=?", [start, end]
        if source:
            query += " AND source=?"
            args.append(source)
        return {(row["source"], row["started_at"]) for row in self.rows(query, args)}

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

    def recent(self, limit=200):
        """The latest learned paths, newest trip first (without their coordinates)."""
        return self.rows("""SELECT id,source,started_at,ended_at,mode,distance_m FROM known_paths
            ORDER BY started_at DESC,id DESC LIMIT ?""", (min(max(int(limit), 1), 2000),))

    def summary_rows(self):
        return self.rows("SELECT source,mode,distance_m,started_at FROM known_paths")
