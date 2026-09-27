"""Observed stays and repeatable place patterns derived from source GPS samples."""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import address_for_coordinate, metres, parse_time, route_segments_from_points, select_best_timed_fixes, smooth_route_segment, stats_from_points, trips_from_points


STOP_RADIUS_M = 50
STOP_MIN_SECONDS = 300
MAX_SAMPLE_GAP_SECONDS = 1800


def sync_timeline(store, sources=None, rebuild=False, since_ids=None):
    """Materialize an efficient timeline from raw observations and confirmed stays.

    Samples while travelling remain available. A confirmed stationary run keeps
    only its arrival and latest/departure observations in the timeline; raw
    observations remain untouched in point_observations.
    """
    if sources is None:
        sources = [row["source"] for row in store._rows("SELECT DISTINCT source FROM point_observations")]
    since_ids = since_ids or {}
    for source in sources:
        since_id = 0 if rebuild else since_ids.get(source, 0)
        if rebuild:
            store.db.execute("DELETE FROM timeline_fixes WHERE source=?", (source,))
        store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT id,source,'movement',NULL FROM point_observations
            WHERE source=? AND id>? ON CONFLICT(observation_id) DO NOTHING""", (source, since_id))
        store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
            SELECT id FROM (SELECT id,ROW_NUMBER() OVER (
                PARTITION BY source,timestamp ORDER BY COALESCE(accuracy_m,1000000000),id DESC) AS rank
                FROM point_observations WHERE source=? AND timestamp IN (
                    SELECT timestamp FROM point_observations WHERE source=? AND id>?)) ranked WHERE rank>1)""",
            (source, source, since_id))
        # Closing a stay changes no observation ID, so it would not be selected
        # by the incremental end_observation_id query below. Promote its former
        # `latest` edge when the first point outside the stop arrives.
        store.db.execute("""UPDATE timeline_fixes SET role='departure' WHERE source=? AND role='latest'
            AND stay_id IN (SELECT id FROM observed_stays_v2 WHERE source=? AND ongoing=0)""",
            (source, source))
        stay_query = "SELECT * FROM observed_stays_v2 WHERE source=?"
        stay_args = [source]
        if not rebuild:
            stay_query += " AND end_observation_id>?"
            stay_args.append(since_id)
        stay_query += " ORDER BY started_at,id"
        for stay in store._rows(stay_query, stay_args):
            store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, stay["started_at"], stay["ended_at"], stay["start_observation_id"], stay["end_observation_id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["start_observation_id"], source, "arrival", stay["id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["end_observation_id"], source, "latest" if stay["ongoing"] else "departure", stay["id"]))
        state = store.db.execute("SELECT candidate FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        candidate = json.loads(state["candidate"]) if state and state["candidate"] else None
        if candidate and candidate["count"] >= 2 and _duration(candidate) < STOP_MIN_SECONDS:
            store.db.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, candidate["start"], candidate["end"], candidate["start_id"], candidate["end_id"]))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (candidate["start_id"], source, "arrival"))
            store.db.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (candidate["end_id"], source, "latest"))


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
    if rebuild:
        store.clear_analysis_cache(commit=False)
    if sources is None:
        sources = [r["source"] for r in store._rows("SELECT DISTINCT source FROM point_observations")]
    processed = 0
    processed_sources = set()
    rebuilt_sources = set()
    since_ids = {}
    for source in sources:
        state = store.db.execute("SELECT * FROM stay_observation_state WHERE source=?", (source,)).fetchone()
        last_id = state["last_observation_id"] if state else 0
        since_ids[source] = 0 if rebuild else last_id
        first_new = store.db.execute("""SELECT timestamp FROM point_observations
            WHERE source=? AND id>? ORDER BY timestamp,id LIMIT 1""", (source, last_id)).fetchone()
        if rebuild or (state and first_new and first_new["timestamp"] <= state["last_timestamp"]):
            rebuilt_sources.add(source)
            store.db.execute("DELETE FROM observed_stays_v2 WHERE source=?", (source,))
            store.db.execute("DELETE FROM stay_observation_state WHERE source=?", (source,))
            state = None
            last_id = 0
        source_max_id = store.db.execute(
            "SELECT COALESCE(MAX(id),?) AS id FROM point_observations WHERE source=?", (last_id, source)).fetchone()["id"]
        cursor = store.db.execute("""SELECT * FROM (
            SELECT o.*,ROW_NUMBER() OVER (PARTITION BY o.source,o.timestamp
                ORDER BY COALESCE(o.accuracy_m,1000000000),o.id DESC) AS observation_rank
            FROM point_observations o WHERE o.source=? AND o.id>?) ranked
            WHERE observation_rank=1 ORDER BY timestamp,id""", (source, last_id))
        candidate = json.loads(state["candidate"]) if state else None
        max_id, last_time = source_max_id, state["last_timestamp"] if state else None
        while batch := cursor.fetchmany(1000):
            for point in batch:
                processed += 1
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
        processed_sources.add(source)
        store.db.execute("""INSERT INTO stay_observation_state(source,last_observation_id,last_timestamp,candidate)
            VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            last_observation_id=excluded.last_observation_id,last_timestamp=excluded.last_timestamp,candidate=excluded.candidate""",
            (source, max_id, last_time, json.dumps(candidate)))
    incremental_sources = processed_sources - rebuilt_sources
    if incremental_sources:
        sync_timeline(store, incremental_sources, since_ids=since_ids)
    if rebuilt_sources:
        sync_timeline(store, rebuilt_sources, rebuild=True)
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
        cluster["display_name"] = cluster["name"] or f"{cluster['lat']:.5f}, {cluster['lon']:.5f}"
    ordered = sorted(clusters, key=lambda c: (c["observed_minutes"], c["visits"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)], mapping


def visited_places(store, limit=500):
    """Observed places ordered by total sampled time, with cache-first labels."""
    clusters, _ = place_patterns(store, limit=None)
    saved = {place["id"]: place for place in store.places(enrich=True)}
    result = []
    for cluster in clusters[:min(max(int(limit), 1), 5000)]:
        place = saved.get(cluster["place_id"])
        address = place["address"] if place else address_for_coordinate(store, cluster["lat"], cluster["lon"])
        result.append({**cluster, "address": address,
                       "display_name": cluster["name"] or address or cluster["display_name"],
                       "saved": bool(place)})
    return result


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
        edge["from_name"] = a["display_name"]
        edge["to_name"] = b["display_name"]
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
    return {"from": origin["display_name"],
            "prediction": top["to_name"], "lat": top["to_lat"], "lon": top["to_lon"],
            "observations": total, "matching_transitions": top["count"], "frequency": round(top["weighted_score"] / total_score, 2) if total_score else 0,
            "current_fix_stale": current["stale"],
            "note": "Recent transitions receive more weight; this is not a guaranteed destination"}


def insights(store, limit=50):
    rows = stays(store, limit=None)
    patterns = place_patterns(store, limit=None, rows=rows)
    for place in [place for place in patterns[0] if not place["name"]][:12]:
        place["address"] = address_for_coordinate(store, place["lat"], place["lon"])
        if place["address"]:
            place["display_name"] = place["address"]
    edges = transitions(store, limit=None, patterns=patterns, rows=rows)
    unlabeled = [p for p in patterns[0] if p["place_id"] is None][:limit]
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
    provider = "photon:" + os.environ.get("GEOTRACKER_PHOTON_URL", "") if os.environ.get("GEOTRACKER_PHOTON_URL") else "geoapify:" + str(bool(os.environ.get("GEOAPIFY_API_KEY")))
    cache_identity = json.dumps(["day-v2", date, timezone_name, source, provider], separators=(",", ":"))
    cache_key = "day:" + hashlib.sha256(cache_identity.encode()).hexdigest()
    watermark = store.analysis_watermark(start, end, source)
    cached = store.cached_analysis(cache_key, watermark)
    if cached is not None:
        return cached
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
    for stay in day_stays:
        place = by_cluster[mapping[stay["id"]]]
        stay["display_name"] = place["name"] or place["display_name"]
    points = store.observations(start, end, source=source, limit=None)
    raw_chronological = list(reversed(points))
    chronological = list(reversed(store.timeline(start, end, source=source, limit=None)))
    trips = trips_from_points(select_best_timed_fixes(raw_chronological))
    route_segments = [smooth_route_segment(segment) for segment in route_segments_from_points(chronological)]
    activity_counts = {mode: sum(trip["mode"] == mode for trip in trips) for mode in ("driving", "walking", "running_cycling")}
    stats = stats_from_points(chronological)
    stats["points"] = len(points)
    stats["unique_points"] = len(chronological)
    stats["timeline_points"] = len(chronological)
    result = {"date": date, "timezone": timezone_name, "source": source, "points": len(points), "stays": day_stays,
            "unique_points": len(chronological), "timeline_points": len(chronological), "stats": stats, "trips": trips, "route_segments": route_segments, "activity_counts": activity_counts,
            "visited_places": visited_places,
            "first_fix": points[-1]["timestamp"] if points else None,
            "last_fix": points[0]["timestamp"] if points else None,
            "first_location": {"lat": points[-1]["lat"], "lon": points[-1]["lon"]} if points else None,
            "last_location": {"lat": points[0]["lat"], "lon": points[0]["lon"]} if points else None,
            "coverage_note": "Stays are observed from samples; time in unobserved gaps is unknown"}
    store.save_analysis(cache_key, watermark, result)
    return result
