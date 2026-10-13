"""Travel between stays: its mode, distance, and whether it went anywhere."""
import math
from datetime import datetime

from .geo import metres
from .motion import classify_motion, connected_elapsed, move_uncertainty, reported_speed, same_source_state, select_best_timed_fixes
from .routes import route_gaps, route_segments_from_points, smooth_route_segment
from .timing import parse_time
from .visits import MAX_SAMPLE_GAP_SECONDS


# Travel that leaves and returns to one place is kept only when it really went somewhere;
# smaller excursions are GPS drift between two halves of one stay.
ROUND_TRIP_MIN_EXCURSION_M = 150
ROUND_TRIP_MIN_DISTANCE_M = 300
# Leaving a place takes this many accurate fixes away from it, spanning this
# long. Indoor GPS jumps last a sample or two; a real errand lasts longer.
AWAY_MIN_FIXES = 3
AWAY_MIN_SECONDS = 240


def _corridor_distance(point, origin, destination):
    """Metres from a fix to the straight corridor joining two stays."""
    x_scale = 111320 * math.cos(math.radians(origin["lat"]))
    bx, by = (destination["lon"] - origin["lon"]) * x_scale, (destination["lat"] - origin["lat"]) * 110574
    px, py = (point["lon"] - origin["lon"]) * x_scale, (point["lat"] - origin["lat"]) * 110574
    length = bx * bx + by * by
    t = 0.0 if length == 0 else max(0.0, min(1.0, (px * bx + py * by) / length))
    return math.hypot(px - t * bx, py - t * by)


def went_elsewhere_between(points, origin, destination):
    """Whether travel between two stays went somewhere else.

    `points` are the fixes strictly between the stays. A fix is away when,
    even after allowing for its reported accuracy, it lies more than
    ROUND_TRIP_MIN_EXCURSION_M outside the corridor joining the stays (and
    beyond an area stay's extent). Walking between two buildings stays in the
    corridor; an indoor GPS jump lasts a sample or two; an errand needs
    AWAY_MIN_FIXES away fixes spanning AWAY_MIN_SECONDS. Returns None when a
    coverage gap longer than a stay may bridge hides what happened.
    """
    times = ([datetime.fromisoformat(origin["ended_at"])]
             + [datetime.fromisoformat(point["timestamp"]) for point in points]
             + [datetime.fromisoformat(destination["started_at"])])
    if any((later - earlier).total_seconds() > MAX_SAMPLE_GAP_SECONDS for earlier, later in zip(times, times[1:])):
        return None
    reach = max(origin.get("radius_m") or 0, destination.get("radius_m") or 0)
    away = [point for point in points
            if (point["accuracy_m"] is None or point["accuracy_m"] <= 100)
            and _corridor_distance(point, origin, destination) - reach - (point["accuracy_m"] or 0)
            >= ROUND_TRIP_MIN_EXCURSION_M]
    if len(away) < AWAY_MIN_FIXES:
        return False
    span = datetime.fromisoformat(away[-1]["timestamp"]) - datetime.fromisoformat(away[0]["timestamp"])
    return span.total_seconds() >= AWAY_MIN_SECONDS


def trip_turnaround(points, origin):
    """Where a round trip went: its longest stop of two minutes or more, else its farthest fix."""
    usable = [point for point in points if point["accuracy_m"] is None or point["accuracy_m"] <= 100]
    best, index = None, 0
    while index < len(usable):
        end = index
        while end + 1 < len(usable) and metres(usable[index]["lat"], usable[index]["lon"],
                                               usable[end + 1]["lat"], usable[end + 1]["lon"]) <= 60:
            end += 1
        seconds = (datetime.fromisoformat(usable[end]["timestamp"]) - datetime.fromisoformat(usable[index]["timestamp"])).total_seconds()
        if seconds >= 120 and (best is None or seconds > best[0]):
            best = (seconds, index, end)
        index = end + 1
    if best:
        seconds, first, last = best
        stop = usable[first:last + 1]
        return {"lat": sum(point["lat"] for point in stop) / len(stop), "lon": sum(point["lon"] for point in stop) / len(stop),
                "stop_minutes": round(seconds / 60, 1), "arrived_at": stop[0]["timestamp"],
                "distance_m": round(metres(origin["lat"], origin["lon"], stop[0]["lat"], stop[0]["lon"]))}
    farthest = max(usable or points, key=lambda point: metres(origin["lat"], origin["lon"], point["lat"], point["lon"]))
    return {"lat": farthest["lat"], "lon": farthest["lon"], "stop_minutes": 0, "arrived_at": farthest["timestamp"],
            "distance_m": round(metres(origin["lat"], origin["lon"], farthest["lat"], farthest["lon"]))}


def is_excursion(trip):
    """True when a trip went far enough to matter even if it ended where it began.

    A trip known not to have left (see went_elsewhere_between) never qualifies; one
    whose coverage is unknown must at least reach far enough.
    """
    return (trip.get("went_elsewhere") is not False
            and trip.get("max_excursion_m", 0) >= ROUND_TRIP_MIN_EXCURSION_M
            and trip["distance_km"] * 1000 >= ROUND_TRIP_MIN_DISTANCE_M)


def stats_from_points(points):
    """Summarize chronological fixes without bridging source or coverage gaps."""
    distance, moving_seconds, by_mode = 0, 0, {}
    by_source = {}
    for point in points:
        by_mode[point["mode"]] = by_mode.get(point["mode"], 0) + 1
        by_source.setdefault(point["source"], []).append(point)
    for source_points in by_source.values():
        source_points.sort(key=lambda point: (point["timestamp"], point.get("id", 0)))
        held_since = source_points[0] if source_points else None
        for prev, point in zip(source_points, source_points[1:]):
            elapsed = connected_elapsed(prev, point, held_since)
            segment = metres(prev["lat"], prev["lon"], point["lat"], point["lon"])
            if elapsed is not None:
                distance += segment
                if point["mode"] in ("walking", "running_cycling", "driving"):
                    moving_seconds += elapsed
            if not same_source_state(prev, point):
                held_since = point
    return {"points": len(points), "distance_km": round(distance / 1000, 2), "moving_hours": round(moving_seconds / 3600, 2), "modes": by_mode,
            "first": points[0]["timestamp"] if points else None, "last": points[-1]["timestamp"] if points else None}


def trips_between_stays(points, stay_rows, start=None, end=None, speeds=None):
    """Build one trip between each pair of consecutive confirmed stays."""
    points_by_source, stays_by_source = {}, {}
    for point in select_best_timed_fixes(points):
        points_by_source.setdefault(point["source"], []).append(point)
    for stay in stay_rows:
        stays_by_source.setdefault(stay["source"], []).append(stay)
    start_at = datetime.fromisoformat(parse_time(start)) if start else None
    end_at = datetime.fromisoformat(parse_time(end)) if end else None
    trips = []
    for source, source_stays in stays_by_source.items():
        source_stays.sort(key=lambda stay: (stay["started_at"], stay["id"]))
        source_points = points_by_source.get(source, [])
        for origin, destination in zip(source_stays, source_stays[1:]):
            departure = datetime.fromisoformat(origin["ended_at"])
            arrival = datetime.fromisoformat(destination["started_at"])
            if arrival <= departure or (arrival - departure).total_seconds() > 86400:
                continue
            if start_at and arrival < start_at:
                continue
            if end_at and departure > end_at:
                continue
            trip_points = [point for point in source_points
                           if departure <= datetime.fromisoformat(point["timestamp"]) <= arrival]
            if len(trip_points) < 2:
                continue
            moves, held_since = [], trip_points[0]
            for previous, point in zip(trip_points, trip_points[1:]):
                seconds = connected_elapsed(previous, point, held_since)
                distance = metres(previous["lat"], previous["lon"], point["lat"], point["lon"])
                if seconds is not None and distance >= 10:
                    moves.append((distance, seconds, reported_speed(point), move_uncertainty(previous, point)))
                if not same_source_state(previous, point):
                    held_since = point
            motion = classify_motion(moves, speeds)
            if motion is None:
                continue
            # Stays carry this flag from analysis; compute it for hand-built rows.
            left = (destination["went_elsewhere"] if "went_elsewhere" in destination
                    else went_elsewhere_between(trip_points[1:-1], origin, destination))
            # Area stays (a lake loop) have an extent; distance inside it is not an excursion.
            origin_reach = origin.get("radius_m") or 0
            destination_reach = destination.get("radius_m") or 0
            excursion = max(metres(origin["lat"], origin["lon"], point["lat"], point["lon"]) for point in trip_points) - origin_reach
            loop = (metres(origin["lat"], origin["lon"], destination["lat"], destination["lon"])
                    <= max(100, origin_reach + 50, destination_reach + 50))
            trip = {
                "start": origin["ended_at"], "end": destination["started_at"],
                "source": source, "distance_km": round(motion["distance_m"] / 1000, 2),
                "duration_minutes": round((arrival - departure).total_seconds() / 60, 1),
                "moving_minutes": round(motion["moving_seconds"] / 60, 1),
                "median_kmh": motion["median_kmh"], "speed_reported": motion["speed_reported"],
                "max_excursion_m": max(0, round(excursion)),
                "mode": motion["mode"], "modes": {motion["mode"]: len(moves)}, "loop": loop,
                "went_elsewhere": None if left is None else bool(left),
                "from_stay_id": origin["id"], "to_stay_id": destination["id"],
                "from_name": origin.get("place_name"), "to_name": destination.get("place_name"),
                "from_lat": origin["lat"], "from_lon": origin["lon"],
                "to_lat": destination["lat"], "to_lon": destination["lon"],
                "recorded_points": len(trip_points),
            }
            # Two stays at one spot joined by drift or a GPS jump are one visit, not a trip.
            if loop and not is_excursion(trip):
                continue
            # Named only for round trips, where it says where you went.
            trip["turnaround"] = trip_turnaround(trip_points[1:-1] or trip_points, origin)
            pieces = [smooth_route_segment(segment) for segment in route_segments_from_points(trip_points, keep_lone=True)]
            route_segments = [piece for piece in pieces if len(piece) > 1]
            trip.update({"route_segments": route_segments, "route_gaps": route_gaps(pieces, origin, destination),
                         "coordinates": route_segments[0] if len(route_segments) == 1 else [],
                         "coverage_complete": len(route_segments) == 1})
            trips.append(trip)
    return sorted(trips, key=lambda trip: trip["start"], reverse=True)
