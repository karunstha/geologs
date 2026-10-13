"""One day: its stays, trips, paths, fuel, and the gaps nothing recorded."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import places as naming
from ..domain.corrections import windows_overlap
from ..domain.fuel import trip_fuel
from ..domain.motion import MOVEMENT_MODES
from ..domain.places import is_home_place, zone_at
from ..domain.routes import route_segments_from_points, smooth_route_segment
from ..domain.sources import fuse_sources, join_source_handoffs
from ..domain.trips import is_excursion, stats_from_points
from ..domain.visits import coverage_gaps, merge_split_visits
from .fuel import fuel_context, fuel_signature
from .paths import fill_trip
from .patterns import place_patterns, resolve_place_patterns
from .places import place_in_zone, places_provider_identity
from .stays import anchored_trips, stays
from .use_case import use_case


@use_case
def day_summary(db, date, timezone_name="UTC", source=None):
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    try:
        day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    day_start = day.astimezone(timezone.utc)
    day_end = (day + timedelta(days=1)).astimezone(timezone.utc)
    start = day_start.isoformat()
    end = (day_end - timedelta(microseconds=1)).isoformat()
    # Switching the places provider in Settings re-resolves every day's names.
    provider = places_provider_identity(db)

    def day_cache_key():
        # Filled routes come from paths learned on other days too.
        cache_identity = json.dumps(["day-v18", date, timezone_name, source, provider, db.settings.get()["source_priority"],
                                     fuel_signature(db), db.paths.signature()], separators=(",", ":"))
        return "day:" + hashlib.sha256(cache_identity.encode()).hexdigest()

    cache_key = day_cache_key()
    watermark = db.points.watermark(start, end, source)
    cached = db.caches.analysis(cache_key, watermark)
    if cached is not None:
        return cached
    points = db.points.observations(start, end, source=source, limit=None)
    samples_by_source = {}
    for point in points:
        samples_by_source[point["source"]] = samples_by_source.get(point["source"], 0) + 1
    unconfirmed = []
    # Unrecorded stretches are filled from the path library; reading a day
    # never adds to it (see learn_recent_paths).
    all_trips = anchored_trips(db, start, end, source)
    day_stays, trips, timeline_sources = fuse_sources(
        list(reversed(stays(db, start, end, source=source, limit=None, lookup_places=True, rejected=unconfirmed))),
        all_trips, samples_by_source, db.settings.get()["source_priority"])
    trips = [fill_trip(db, trip) for trip in trips]
    # Stops the detector found but no place confirmed, for you to keep or ignore;
    # none you already added or removed by hand.
    edits = db.corrections.stay_edits(start, end, source)
    shown_sources = set(timeline_sources) or set(samples_by_source)
    possible_stops = [{"source": row["source"], "started_at": row["started_at"], "ended_at": row["ended_at"],
                       "lat": row["lat"], "lon": row["lon"], "point_count": row["point_count"],
                       "minutes": round((datetime.fromisoformat(row["ended_at"])
                                         - datetime.fromisoformat(row["started_at"])).total_seconds() / 60, 1)}
                      for row in reversed(unconfirmed)
                      if row["source"] in shown_sources
                      and not any(edit["source"] == row["source"] and windows_overlap(edit, row["started_at"], row["ended_at"], .01)
                                  for edit in edits)]
    for stay in day_stays:
        first = max(datetime.fromisoformat(stay["started_at"]), day_start)
        last = min(datetime.fromisoformat(stay["ended_at"]), day_end)
        stay["observed_minutes"] = round(max(0, (last - first).total_seconds()) / 60, 1)
    raw_patterns = place_patterns(db, limit=None, rows=day_stays)
    raw_by_cluster = {cluster["cluster_id"]: cluster for cluster in raw_patterns[0]}
    for cluster in raw_patterns[0]:
        cluster["first_seen"] = None
        cluster["last_seen"] = None
    for stay in day_stays:
        cluster = raw_by_cluster[raw_patterns[1][stay["id"]]]
        first = max(datetime.fromisoformat(stay["started_at"]), day_start).isoformat()
        last = min(datetime.fromisoformat(stay["ended_at"]), day_end).isoformat()
        cluster["first_seen"] = min(cluster["first_seen"] or first, first)
        cluster["last_seen"] = max(cluster["last_seen"] or last, last)
    clusters, mapping = resolve_place_patterns(db, raw_patterns)
    by_cluster = {cluster["cluster_id"]: cluster for cluster in clusters}
    places_including_home = sorted(clusters, key=lambda item: item["first_seen"])
    visited_places = [place for place in places_including_home if not is_home_place(place)]
    for stay in day_stays:
        place = by_cluster[mapping[stay["id"]]]
        stay["cluster_id"] = place["cluster_id"]
        stay["display_name"] = place["name"] or place["display_name"]
        stay["category"] = place.get("category") or ""
        stay["provider_place_id"] = place.get("provider_place_id") or ""
        stay["zone"] = place.get("zone")
    chronological = list(reversed(db.points.timeline(start, end, source=source, limit=None)))
    stay_labels = {stay["id"]: by_cluster[mapping[stay["id"]]]["name"] or by_cluster[mapping[stay["id"]]]["display_name"]
                   for stay in day_stays}
    # Speed cannot tell slow driving from cycling. A lone "ride" between two
    # drives (moving the car to another lot) is the car you arrived in.
    by_source = {}
    for trip in sorted(trips, key=lambda item: item["start"]):
        by_source.setdefault(trip["source"], []).append(trip)
    for sequence in by_source.values():
        for before, trip, after in zip(sequence, sequence[1:], sequence[2:]):
            if trip["mode"] == "cycling" and not trip.get("mode_corrected") and before["mode"] == after["mode"] == "driving":
                trip.update(mode="driving", modes={"driving": sum(trip["modes"].values())}, mode_from_context=True)
    kept_trips, internal_moves, saved_places = [], {}, db.places.saved()
    for trip in trips:
        trip["from_name"] = stay_labels.get(trip["from_stay_id"], trip.get("from_name"))
        trip["to_name"] = stay_labels.get(trip["to_stay_id"], trip.get("to_name"))
        origin, destination = mapping.get(trip["from_stay_id"]), mapping.get(trip["to_stay_id"])
        if origin is not None and origin == destination:
            # Same place at both ends. Moving between two buildings of one
            # campus goes nowhere else: that is part of the visit, not a trip
            # "to" the place you were already at. A real round trip (lunch
            # off campus, a run from home) stays; drift is dropped.
            if trip.get("went_elsewhere") is False:
                move = {key: trip.get(key) for key in ("source", "start", "end", "mode", "distance_km", "duration_minutes",
                                                       "route_segments", "route_gaps", "route_fills", "mode_corrected")}
                # Crossing a campus from sparse fixes looks fast: the phone held
                # its last position, then jumped. Under 1 km inside one place is
                # walking between buildings, not a ride or the car you arrived
                # in, unless the device itself reported the speed.
                if (move["distance_km"] < 1 and not trip.get("speed_reported") and not trip.get("mode_corrected")
                        and (move["mode"] in ("running", "cycling") or trip.get("mode_from_context"))):
                    move["mode"] = "walking"
                internal_moves[(trip["from_stay_id"], trip["to_stay_id"])] = move
                continue
            if not is_excursion(trip):
                continue
            trip["loop"] = True
        if trip.get("loop"):
            # Say where a round trip went, so Home -> Home reads as an errand, not a glitch.
            turnaround = trip.get("turnaround")
            if turnaround and turnaround["stop_minutes"]:
                turnaround["name"] = place_in_zone(db, turnaround["lat"], turnaround["lon"],
                                                   zone_at(saved_places, turnaround["lat"], turnaround["lon"]),
                                                   lookup=naming.place_for_coordinate).get("name") or None
        kept_trips.append(trip)
    trips = kept_trips
    # Fuel for each drive at that day's price; a day's total only from drives that could be costed.
    costing = fuel_context(db)
    for trip in trips:
        trip["fuel"] = trip_fuel(costing, trip, date)
    costed = [trip["fuel"] for trip in trips if trip.get("fuel")]
    day_fuel = ({"litres": round(sum(item["litres"] for item in costed), 2), "cost": round(sum(item["cost"] for item in costed), 2),
                 "currency": costing["currency"], "drives": len(costed)} if costed else None)
    day_stays = merge_split_visits(day_stays, lambda stay: stay.get("cluster_id"), internal_moves)
    day_stays = join_source_handoffs(day_stays, trips)
    for stay in day_stays:
        if stay["kind"] == "area":
            path = [point for point in chronological if point["source"] == stay["source"]
                    and stay["started_at"] <= point["timestamp"] <= stay["ended_at"]]
            stay["route_segments"] = [smooth_route_segment(segment) for segment in route_segments_from_points(path)]
    route_segments = [smooth_route_segment(segment) for segment in route_segments_from_points(chronological)]
    activity_totals = {mode: {"count": 0, "distance_km": 0.0, "minutes": 0.0} for mode in MOVEMENT_MODES}
    for trip in trips:
        totals = activity_totals[trip["mode"]]
        totals["count"] += 1
        totals["distance_km"] += trip["distance_km"]
        totals["minutes"] += trip["duration_minutes"]
    for stay in day_stays:
        if stay.get("activity") in activity_totals:
            totals = activity_totals[stay["activity"]]
            totals["count"] += 1
            totals["distance_km"] += (stay.get("distance_m") or 0) / 1000
            totals["minutes"] += stay["observed_minutes"]
        for move in stay["internal_moves"]:
            totals = activity_totals[move["mode"]]
            totals["count"] += 1
            totals["distance_km"] += move["distance_km"]
            totals["minutes"] += move["duration_minutes"]
    for totals in activity_totals.values():
        totals["distance_km"] = round(totals["distance_km"], 2)
        totals["minutes"] = round(totals["minutes"], 1)
    activity_counts = {mode: totals["count"] for mode, totals in activity_totals.items()}
    stats = stats_from_points(chronological)
    stats["points"] = len(points)
    stats["unique_points"] = len(chronological)
    stats["timeline_points"] = len(chronological)
    result = {"date": date, "timezone": timezone_name, "source": source, "start": start, "end": end,
            "points": len(points), "stays": day_stays,
            "unique_points": len(chronological), "timeline_points": len(chronological), "stats": stats, "trips": trips, "route_segments": route_segments,
            "activity_counts": activity_counts, "activity_totals": activity_totals,
            "movement_minutes": round(sum(totals["minutes"] for totals in activity_totals.values()), 1),
            "movement_km": round(sum(totals["distance_km"] for totals in activity_totals.values()), 2),
            "coverage_gaps": coverage_gaps(points, trips, day_stays), "timeline_sources": timeline_sources,
            "samples_by_source": samples_by_source,
            "stay_edits": edits, "possible_stops": possible_stops, "fuel": day_fuel,
            "visited_places": visited_places, "places_including_home": places_including_home,
            "first_fix": points[-1]["timestamp"] if points else None,
            "last_fix": points[0]["timestamp"] if points else None,
            "first_location": {"lat": points[-1]["lat"], "lon": points[-1]["lon"]} if points else None,
            "last_location": {"lat": points[0]["lat"], "lon": points[0]["lon"]} if points else None,
            "coverage_note": "Stays are observed from samples; time in unobserved gaps is unknown"}
    # Learning this day may have changed the library the key depends on.
    db.caches.save_analysis(day_cache_key(), watermark, result)
    return result
