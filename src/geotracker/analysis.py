"""Observed stays and repeatable place patterns derived from source GPS samples."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import address_for_coordinate, metres, parse_time, route_segments_from_points, select_best_timed_fixes, select_route_fixes, select_unique_fixes, stats_from_points, trips_from_points


STOP_RADIUS_M = 50
STOP_MIN_SECONDS = 300
MAX_SAMPLE_GAP_SECONDS = 1800


def _duration(candidate):
    return (datetime.fromisoformat(candidate["end"]) - datetime.fromisoformat(candidate["start"])).total_seconds()


def _eligible(point):
    return (point["accuracy_m"] is None or point["accuracy_m"] <= 100)


def _new_candidate(point):
    return {"start_id": point["id"], "end_id": point["id"], "start": point["timestamp"], "end": point["timestamp"],
            "first_lat": point["lat"], "first_lon": point["lon"], "lat": point["lat"], "lon": point["lon"], "count": 1}


def _save_candidate(store, source, candidate, ongoing):
    if candidate is None or candidate["count"] < 2 or _duration(candidate) < STOP_MIN_SECONDS:
        return
    store.db.execute("""INSERT INTO observed_stays_v2
        (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(start_observation_id) DO UPDATE SET
        end_observation_id=excluded.end_observation_id,ended_at=excluded.ended_at,
        lat=excluded.lat,lon=excluded.lon,point_count=excluded.point_count,ongoing=excluded.ongoing""",
        (source, candidate["start_id"], candidate["end_id"], candidate["start"], candidate["end"],
         candidate["lat"], candidate["lon"], candidate["count"], int(ongoing)))


def process_stays(store, sources=None, rebuild=False):
    """Process raw observations; a late import rebuilds that source safely."""
    if sources is None:
        sources = [r["source"] for r in store._rows("SELECT DISTINCT source FROM point_observations")]
    processed = 0
    for source in sources:
        state = store.db.execute("SELECT * FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        last_id = state["last_observation_id"] if state else 0
        first_new = store.db.execute("""SELECT timestamp FROM point_observations
            WHERE source=? AND id>? ORDER BY timestamp,id LIMIT 1""", (source, last_id)).fetchone()
        if rebuild or (state and first_new and first_new["timestamp"] <= state["last_timestamp"]):
            store.db.execute("DELETE FROM observed_stays_v2 WHERE source=?", (source,))
            store.db.execute("DELETE FROM stay_observation_state WHERE source=?", (source,))
            state = None
            last_id = 0
        cursor = store.db.execute("""SELECT * FROM point_observations
            WHERE source=? AND id>? ORDER BY timestamp,id""", (source, last_id))
        candidate = json.loads(state["candidate"]) if state else None
        max_id, last_time = last_id, state["last_timestamp"] if state else None
        while batch := cursor.fetchmany(1000):
            for point in batch:
                processed += 1
                max_id = max(max_id, point["id"])
                last_time = point["timestamp"]
                if candidate:
                    elapsed = (datetime.fromisoformat(point["timestamp"]) - datetime.fromisoformat(candidate["end"])).total_seconds()
                    reported_moving = point["speed_origin"] == "reported" and point["speed_mps"] is not None and point["speed_mps"] > 2.5
                    within = (0 < elapsed <= MAX_SAMPLE_GAP_SECONDS and _eligible(point) and not reported_moving
                              and metres(point["lat"], point["lon"], candidate["first_lat"], candidate["first_lon"]) <= STOP_RADIUS_M
                              and metres(point["lat"], point["lon"], candidate["lat"], candidate["lon"]) <= STOP_RADIUS_M)
                    if within:
                        count = candidate["count"] + 1
                        candidate["lat"] += (point["lat"] - candidate["lat"]) / count
                        candidate["lon"] += (point["lon"] - candidate["lon"]) / count
                        candidate["count"] = count
                        candidate["end"] = point["timestamp"]
                        candidate["end_id"] = point["id"]
                        _save_candidate(store, source, candidate, True)
                        continue
                    _save_candidate(store, source, candidate, False)
                candidate = _new_candidate(point) if _eligible(point) else None
        if max_id == last_id:
            continue
        store.db.execute("""INSERT INTO stay_observation_state(source,last_observation_id,last_timestamp,candidate)
            VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            last_observation_id=excluded.last_observation_id,last_timestamp=excluded.last_timestamp,candidate=excluded.candidate""",
            (source, max_id, last_time, json.dumps(candidate)))
    return {"processed_points": processed}


def _matching_place(lat, lon, places):
    matches = [(metres(lat, lon, p["lat"], p["lon"]), p) for p in places]
    matches = [(d, p) for d, p in matches if d <= p["radius_m"]]
    return min(matches, key=lambda item: item[0])[1] if matches else None


def stays(store, start=None, end=None, source=None, limit=500):
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
    rows = store._rows("SELECT * FROM observed_stays_v2" + where + " ORDER BY started_at DESC" + suffix, args)
    places = store.places()
    for row in rows:
        row["observed_minutes"] = round((datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds() / 60, 1)
        place = _matching_place(row["lat"], row["lon"], places)
        row["place_id"] = place["id"] if place else None
        row["place_name"] = place["name"] if place else None
    return rows


def _bucket(lat, lon):
    """75 m ECEF grid: nearby places only need 27 neighboring cells checked."""
    a, b = math.radians(lat), math.radians(lon)
    r = 6371000 / 75
    return (math.floor(r * math.cos(a) * math.cos(b)), math.floor(r * math.cos(a) * math.sin(b)), math.floor(r * math.sin(a)))


def place_patterns(store, limit=50, rows=None):
    rows = rows if rows is not None else stays(store, limit=None)
    places = {p["id"]: p for p in store.places()}
    clusters, bucket_index, mapping = [], {}, {}
    for stay in rows:
        key = _bucket(stay["lat"], stay["lon"])
        target = None
        if stay["place_id"] is not None:
            target = next((i for i, c in enumerate(clusters) if c["place_id"] == stay["place_id"]), None)
        else:
            candidates = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        candidates.update(bucket_index.get((key[0]+dx, key[1]+dy, key[2]+dz), ()))
            target = next((i for i in candidates if clusters[i]["place_id"] is None and metres(stay["lat"], stay["lon"], clusters[i]["lat"], clusters[i]["lon"]) <= 75), None)
        if target is None:
            place = places.get(stay["place_id"])
            target = len(clusters)
            clusters.append({"cluster_id": target, "place_id": stay["place_id"], "name": place["name"] if place else None,
                             "lat": stay["lat"], "lon": stay["lon"], "visits": 0, "observed_minutes": 0,
                             "last_seen": stay["ended_at"]})
            bucket_index.setdefault(key, set()).add(target)
        cluster = clusters[target]
        cluster["visits"] += 1
        cluster["observed_minutes"] += stay["observed_minutes"]
        if stay["ended_at"] > cluster["last_seen"]:
            cluster["last_seen"] = stay["ended_at"]
        mapping[stay["id"]] = target
    for cluster in clusters:
        cluster["observed_minutes"] = round(cluster["observed_minutes"], 1)
    ordered = sorted(clusters, key=lambda c: (c["observed_minutes"], c["visits"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)], mapping


def transitions(store, limit=50, patterns=None, rows=None):
    rows = rows if rows is not None else stays(store, limit=None)
    clusters, mapping = patterns if patterns is not None else place_patterns(store, limit=None, rows=rows)
    cluster_by_id = {c["cluster_id"]: c for c in clusters}
    rows = list(reversed(rows))
    edges = {}
    by_source = {}
    now = datetime.now(timezone.utc)
    for stay in rows:
        previous = by_source.get(stay["source"])
        target = mapping.get(stay["id"])
        if previous and target is not None:
            origin = mapping.get(previous["id"])
            gap = (datetime.fromisoformat(stay["started_at"]) - datetime.fromisoformat(previous["ended_at"])).total_seconds()
            if origin is not None and origin != target and 0 <= gap <= 86400:
                key = (origin, target)
                edge = edges.setdefault(key, {"from_cluster_id": origin, "to_cluster_id": target, "count": 0, "weighted_score": 0.0, "last_seen": stay["started_at"]})
                edge["count"] += 1
                age_days = max(0.0, (now - datetime.fromisoformat(stay["started_at"])).total_seconds() / 86400)
                edge["weighted_score"] += 0.5 ** (age_days / 90)
                edge["last_seen"] = max(edge["last_seen"], stay["started_at"])
        by_source[stay["source"]] = stay
    result = list(edges.values())
    for edge in result:
        a, b = cluster_by_id[edge["from_cluster_id"]], cluster_by_id[edge["to_cluster_id"]]
        edge["from_name"] = a["name"] or f"Unlabeled place {a['cluster_id']}"
        edge["to_name"] = b["name"] or f"Unlabeled place {b['cluster_id']}"
        edge["to_lat"], edge["to_lon"] = b["lat"], b["lon"]
        edge["weighted_score"] = round(edge["weighted_score"], 4)
    ordered = sorted(result, key=lambda e: (e["weighted_score"], e["count"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)]


def predict_next(store, patterns=None, edges=None):
    current = store.latest()
    if not current:
        return {"prediction": None, "reason": "no location data"}
    clusters, _ = patterns if patterns is not None else place_patterns(store, limit=None)
    origin = next((c for c in clusters if metres(current["lat"], current["lon"], c["lat"], c["lon"]) <= 75), None)
    if not origin:
        return {"prediction": None, "reason": "current point is not at an observed stay"}
    edges = [e for e in (edges if edges is not None else transitions(store, limit=None, patterns=patterns)) if e["from_cluster_id"] == origin["cluster_id"]]
    total = sum(e["count"] for e in edges)
    if total < 3:
        return {"prediction": None, "reason": "fewer than three observed departures", "observations": total}
    top = max(edges, key=lambda e: e["weighted_score"])
    newest = max(e["last_seen"] for e in edges)
    if (datetime.now(timezone.utc) - datetime.fromisoformat(newest)).total_seconds() > 180 * 86400:
        return {"prediction": None, "reason": "transition evidence is older than 180 days", "observations": total}
    total_score = sum(e["weighted_score"] for e in edges)
    return {"from": origin["name"] or f"Unlabeled place {origin['cluster_id']}",
            "prediction": top["to_name"], "lat": top["to_lat"], "lon": top["to_lon"],
            "observations": total, "matching_transitions": top["count"], "frequency": round(top["weighted_score"] / total_score, 2) if total_score else 0,
            "current_fix_stale": current["stale"],
            "note": "Recent transitions receive more weight; this is not a guaranteed destination"}


def insights(store, limit=50):
    rows = stays(store, limit=None)
    patterns = place_patterns(store, limit=None, rows=rows)
    edges = transitions(store, limit=None, patterns=patterns, rows=rows)
    unlabeled = [p for p in patterns[0] if p["place_id"] is None][:limit]
    for place in unlabeled[:12]:
        place["address"] = address_for_coordinate(store, place["lat"], place["lon"])
    return {"frequent_places": patterns[0][:limit],
            "unlabeled_places": unlabeled,
            "transitions": edges[:limit],
            "prediction": predict_next(store, patterns=patterns, edges=edges),
            "observed_stays": len(rows)}


def day_summary(store, date, timezone_name="UTC", source=None):
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
    day_stays = list(reversed(stays(store, start, end, source=source, limit=None)))
    for stay in day_stays:
        first = max(datetime.fromisoformat(stay["started_at"]), day_start)
        last = min(datetime.fromisoformat(stay["ended_at"]), day_end)
        stay["observed_minutes"] = round(max(0, (last - first).total_seconds()) / 60, 1)
    clusters, mapping = place_patterns(store, limit=None, rows=day_stays)
    by_cluster = {cluster["cluster_id"]: cluster for cluster in clusters}
    for stay in day_stays:
        cluster = by_cluster[mapping[stay["id"]]]
        first = max(datetime.fromisoformat(stay["started_at"]), day_start).isoformat()
        cluster["first_seen"] = min(cluster.get("first_seen", first), first)
    visited_places = sorted(clusters, key=lambda item: item["first_seen"])
    for place in visited_places:
        place["address"] = address_for_coordinate(store, place["lat"], place["lon"])
        if not place["name"] and place["address"]:
            place["name"] = place["address"]
    points = store.observations(start, end, source=source, limit=None)
    raw_chronological = list(reversed(points))
    chronological = select_unique_fixes(raw_chronological)
    trips = trips_from_points(select_best_timed_fixes(raw_chronological))
    route_segments = route_segments_from_points(select_route_fixes(raw_chronological))
    activity_counts = {mode: sum(trip["mode"] == mode for trip in trips) for mode in ("driving", "walking", "running_cycling")}
    stats = stats_from_points(chronological)
    stats["points"] = len(points)
    stats["unique_points"] = len(chronological)
    return {"date": date, "timezone": timezone_name, "source": source, "points": len(points), "stays": day_stays,
            "unique_points": len(chronological), "stats": stats, "trips": trips, "route_segments": route_segments, "activity_counts": activity_counts,
            "visited_places": visited_places,
            "first_fix": points[-1]["timestamp"] if points else None,
            "last_fix": points[0]["timestamp"] if points else None,
            "first_location": {"lat": points[-1]["lat"], "lon": points[-1]["lon"]} if points else None,
            "last_location": {"lat": points[0]["lat"], "lon": points[0]["lon"]} if points else None,
            "coverage_note": "Stays are observed from samples; time in unobserved gaps is unknown"}
