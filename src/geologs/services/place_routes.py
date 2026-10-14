"""Keeping routes between places in step with the trips that took them (see domain.place_routes)."""
import json
from datetime import datetime

from ..domain.paths import STAY_MATCH_M, path_bounds
from ..domain.place_routes import group, track_from_trip
from .use_case import use_case

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
