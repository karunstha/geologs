"""Routes between places (place_routes) and the trip tracks they are stitched from (trip_tracks)."""
from __future__ import annotations

import json
import math

from .repository import Repository


def _box(point, reach_m):
    """(min_lat, max_lat, min_lon, max_lon) within reach_m of a [lon, lat] point."""
    pad_lat = reach_m / 110574
    pad_lon = reach_m / (111320 * math.cos(math.radians(point[1])))
    return point[1] - pad_lat, point[1] + pad_lat, point[0] - pad_lon, point[0] + pad_lon


def _ends_clause(a, b, reach_m):
    """SQL and arguments for rows whose from end lies within reach_m of a and to end of b."""
    return ("from_lat BETWEEN ? AND ? AND from_lon BETWEEN ? AND ? AND to_lat BETWEEN ? AND ? AND to_lon BETWEEN ? AND ?",
            [*_box(a, reach_m), *_box(b, reach_m)])


class PlaceRoutesRepository(Repository):
    @staticmethod
    def _track(row):
        return {**row, "from": [row["from_lon"], row["from_lat"]], "to": [row["to_lon"], row["to_lat"]],
                "pieces": json.loads(row["pieces"])}

    @staticmethod
    def _route(row):
        return {**row, "from": [row["from_lon"], row["from_lat"]], "to": [row["to_lon"], row["to_lat"]],
                "parts": json.loads(row["parts"]), "members": json.loads(row["members"])}

    def tracks_starting_between(self, start, end, source=None):
        """Tracks of trips that started in a window (from one source, when given)."""
        query, args = "SELECT * FROM trip_tracks WHERE started_at>=? AND started_at<=?", [start, end]
        if source:
            query += " AND source=?"
            args.append(source)
        return [self._track(row) for row in self.rows(query, args)]

    def track(self, source, started_at):
        row = self.one("SELECT * FROM trip_tracks WHERE source=? AND started_at=?", (source, started_at))
        return row and self._track(row)

    def tracks_between(self, a, b, reach_m):
        """Tracks of trips from within reach_m of [lon, lat] a to within reach_m of b."""
        clause, args = _ends_clause(a, b, reach_m)
        return [self._track(row) for row in self.rows("SELECT * FROM trip_tracks WHERE " + clause, args)]

    def add_track(self, track):
        self.execute("""INSERT INTO trip_tracks
            (source,started_at,ended_at,mode,from_lat,from_lon,to_lat,to_lon,from_name,to_name,distance_m,observed_m,pieces)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,started_at) DO NOTHING""",
            (track["source"], track["started_at"], track["ended_at"], track["mode"], track["from"][1], track["from"][0],
             track["to"][1], track["to"][0], track["from_name"], track["to_name"], track["distance_m"],
             track["observed_m"], json.dumps(track["pieces"], separators=(",", ":"))))

    def delete_track(self, source, started_at):
        self.execute("DELETE FROM trip_tracks WHERE source=? AND started_at=?", (source, started_at))

    def delete_tracks_through(self, source, timestamp):
        """Forget a tracker's tracks of trips that were under way at `timestamp`; returns them."""
        tracks = [self._track(row) for row in self.rows(
            "SELECT * FROM trip_tracks WHERE source=? AND started_at<=? AND ended_at>=?", (source, timestamp, timestamp))]
        for track in tracks:
            self.delete_track(track["source"], track["started_at"])
        return tracks

    def delete_source(self, source):
        self.execute("DELETE FROM trip_tracks WHERE source=?", (source,))

    def all_tracks(self):
        return [self._track(row) for row in self.rows("SELECT * FROM trip_tracks")]

    def routes_between(self, a, b, reach_m):
        """Routes from within reach_m of [lon, lat] a to within reach_m of b, either way round."""
        clause, args = _ends_clause(a, b, reach_m)
        back, back_args = _ends_clause(b, a, reach_m)
        return [self._route(row) for row in self.rows(
            f"SELECT * FROM place_routes WHERE ({clause}) OR ({back})", args + back_args)]

    def add_route(self, route, bounds):
        """Keep a route. One already kept that the same trip started (another request grouping the
        same trips just now) stays as it is."""
        min_lat, min_lon, max_lat, max_lon = bounds
        self.execute("""INSERT INTO place_routes
            (mode,from_lat,from_lon,to_lat,to_lon,from_name,to_name,distance_m,observed_m,
             min_lat,min_lon,max_lat,max_lon,parts,members,latest,seed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(seed) DO NOTHING""",
            (route["mode"], route["from"][1], route["from"][0], route["to"][1], route["to"][0], route["from_name"],
             route["to_name"], route["distance_m"], route["observed_m"], min_lat, min_lon, max_lat, max_lon,
             json.dumps(route["parts"], separators=(",", ":")), json.dumps(route["members"], separators=(",", ":")),
             route["latest"], json.dumps([route["members"][0]["source"], route["members"][0]["started_at"]])))

    def delete_route(self, route_id):
        self.execute("DELETE FROM place_routes WHERE id=?", (route_id,))

    def delete_all_routes(self):
        self.execute("DELETE FROM place_routes")

    def signature(self):
        """Changes whenever the routes do, for cache keys of days that use them."""
        row = self.execute("SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS max_id FROM place_routes").fetchone()
        return f"{row['count']}:{row['max_id']}"

    def recent(self, limit=200):
        """Routes most recently taken first, without their geometry."""
        return [{**row, "trips": len(json.loads(row.pop("members")))} for row in self.rows(
            """SELECT id,mode,from_name,to_name,distance_m,observed_m,latest,members FROM place_routes
            ORDER BY latest DESC,id DESC LIMIT ?""", (min(max(int(limit), 1), 2000),))]
