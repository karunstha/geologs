"""Keeping routes between places in step with the trips that took them (see domain.place_routes)."""
import json
import math
from datetime import datetime

from ..domain.paths import STAY_MATCH_M, path_bounds
from ..domain.place_routes import group, track_from_trip
from .use_case import use_case

# A route on the Places map keeps at most this many points per part: enough to follow the roads.
MAP_POINTS = 160

# Trips whose ends lie this close to a changed trip's may share a route with it: their routes
# are grouped again together. Twice the farthest a stay may lie from a route's end.
NEIGHBOURHOOD_M = 2 * STAY_MATCH_M
# A track is rewritten only when what it says about the way changed (names come and go with lookups).
TRACK_FIELDS = ("ended_at", "mode", "from", "to", "pieces")


def _same(row, track):
    return all(json.dumps(row[field]) == json.dumps(track[field]) for field in TRACK_FIELDS)


def learn_tracks(db, trips, start, end, excluded, source=None):
    """Keep trip_tracks in step with the trips that start in [start, end]; returns the [from, to]
    ends of tracks that were added, changed or removed."""
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    wanted = {}
    for trip in trips:
        if first <= datetime.fromisoformat(trip["start"]) <= last and (trip["source"], trip["start"]) not in excluded:
            track = track_from_trip(trip)
            if track:
                wanted[(track["source"], track["started_at"])] = track
    changed = []
    for row in db.place_routes.tracks_starting_between(start, end, source):
        if first <= datetime.fromisoformat(row["started_at"]) <= last:
            want = wanted.get((row["source"], row["started_at"]))
            if want is not None and _same(row, want):
                wanted.pop((row["source"], row["started_at"]))
                continue
            db.place_routes.delete_track(row["source"], row["started_at"])
            changed.append((row["from"], row["to"]))
    for track in wanted.values():
        db.place_routes.add_track(track)
        changed.append((track["from"], track["to"]))
    return changed


def regroup(db, ends):
    """Group again the routes of trips between places near these [from, to] ends, and of the trips
    that took those routes."""
    tracks, routes = {}, {}
    for a, b in ends:
        for track in db.place_routes.tracks_between(a, b, NEIGHBOURHOOD_M):
            tracks[(track["source"], track["started_at"])] = track
        for route in db.place_routes.routes_between(a, b, NEIGHBOURHOOD_M):
            routes[route["id"]] = route
    for route in routes.values():
        for member in route["members"]:
            key = (member["source"], member["started_at"])
            if key not in tracks:
                track = db.place_routes.track(*key)
                if track:
                    tracks[key] = track
        db.place_routes.delete_route(route["id"])
    for route in group(list(tracks.values())):
        db.place_routes.add_route(route, path_bounds([point for part in route["parts"] for point in part["coordinates"]]))


@use_case
def rebuild_routes(db):
    """Group every trip track into routes again (after a tracker is removed, or the rules change)."""
    db.place_routes.delete_all_routes()
    for route in group(db.place_routes.all_tracks()):
        db.place_routes.add_route(route, path_bounds([point for part in route["parts"] for point in part["coordinates"]]))


def routes_between(db, a, b):
    """Routes that may join [lon, lat] a and b, either way round."""
    return db.place_routes.routes_between(a, b, NEIGHBOURHOOD_M)


def _thin(coordinates, most=MAP_POINTS):
    """Every nth point of a line, keeping both ends, so it has at most `most`."""
    step = max(1, math.ceil(len(coordinates) / most))
    kept = coordinates[::step]
    return kept if kept[-1] == coordinates[-1] else kept + [coordinates[-1]]


@use_case
def route_map(db, south, west, north, east, limit=300):
    """Routes between places that pass through a map's view, each with how many trips took it and
    its way as [lat, lon] lines, for the Places map."""
    south, west, north, east = (float(value) for value in (south, west, north, east))
    if not (-90 <= south <= north <= 90) or not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("bounds must be south ≤ north within ±90 and west, east within ±180")
    if west > east:  # A view across the antimeridian: everything east and west of it.
        west, east = -180.0, 180.0
    routes = db.place_routes.in_bounds(south, west, north, east, limit)
    return {"routes": [{"id": route["id"], "mode": route["mode"], "from_name": route["from_name"],
                        "to_name": route["to_name"], "distance_m": route["distance_m"], "latest": route["latest"],
                        "trips": len(route["members"]),
                        "lines": [[[point[1], point[0]] for point in _thin(part["coordinates"])]
                                  for part in route["parts"] if len(part["coordinates"]) >= 2]}
                       for route in routes],
            "limited": len(routes) >= min(max(int(limit), 1), 1000)}
