"""A day as a replay: each way it went, timed, and the places it stopped, for a moving dot to follow."""
from datetime import datetime

from ..domain.places import is_home_place
from ..domain.replay import timed_path
from .day import day_summary
from .use_case import use_case


def _seconds(value):
    return datetime.fromisoformat(value).timestamp()


def _way(legs):
    return [point for leg in legs or [] for point in leg["coordinates"]]


@use_case
def day_replay(db, date, timezone_name="UTC", source=None):
    """The day as Home draws it (see day_summary), with times along every way: its trips, the moves
    within a place, and an activity around one (a lake run). Times are seconds after the day's
    start, and each way's points are [seconds, lon, lat]."""
    day = day_summary(db, date, timezone_name, source)
    origin = _seconds(day["start"])
    fixes = {}
    for point in sorted(db.points.timeline(day["start"], day["end"], source=source, limit=None),
                        key=lambda point: point["timestamp"]):
        fixes.setdefault(point["source"], []).append([_seconds(point["timestamp"]), point["lon"], point["lat"]])

    def track(way, item_source, start, end):
        return [[round(seconds - origin, 1), round(lon, 6), round(lat, 6)]
                for seconds, lon, lat in timed_path(way, fixes.get(item_source, []), _seconds(start), _seconds(end))]

    def move(kind, item, way, start, end, **details):
        return {"kind": kind, "mode": item.get("mode"), "start": round(_seconds(start) - origin, 1),
                "end": round(_seconds(end) - origin, 1), **details, "track": track(way, item["source"], start, end)}

    moves, stops = [], []
    for trip in day["trips"]:
        way = _way(trip.get("route")) or [[trip["from_lon"], trip["from_lat"]], [trip["to_lon"], trip["to_lat"]]]
        moves.append(move("trip", trip, way, trip["start"], trip["end"], from_name=trip.get("from_name"),
                          to_name=trip.get("to_name"), distance_km=trip["distance_km"], recorded=trip.get("recorded", True)))
    for stay in day["stays"]:
        name = stay["display_name"]
        stops.append({"name": name, "lat": stay["lat"], "lon": stay["lon"], "kind": stay["kind"],
                      "activity": stay.get("activity"), "home": is_home_place({"name": name}),
                      "start": round(max(_seconds(stay["started_at"]), origin) - origin, 1),
                      "end": round(min(_seconds(stay["ended_at"]), _seconds(day["end"])) - origin, 1)})
        if stay.get("activity") and stay.get("route_segments"):
            way = [point for segment in stay["route_segments"] for point in segment]
            moves.append(move("activity", {**stay, "mode": stay["activity"]}, way, stay["started_at"], stay["ended_at"],
                              from_name=name, to_name=name, distance_km=round((stay.get("distance_m") or 0) / 1000, 2),
                              recorded=True))
        for within in stay["internal_moves"]:
            if within.get("route"):
                moves.append(move("move", within, _way(within["route"]), within["start"], within["end"],
                                  from_name=name, to_name=name, distance_km=within["distance_km"],
                                  recorded=within.get("recorded", True)))
    moves.sort(key=lambda item: item["start"])
    stops.sort(key=lambda item: item["start"])
    return {"date": date, "timezone": timezone_name, "source": source, "start": day["start"],
            "distance_km": day["movement_km"], "moving_minutes": day["movement_minutes"],
            "stays": stops, "moves": moves}
