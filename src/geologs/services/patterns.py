"""Your places over time: clustering stays into places, naming them, searching visits, and where you go next."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import places as naming
from ..domain.geo import metres
from ..domain.places import ADDRESS_MERGE_M, is_home_place, label_place, normalized_place_name, place_bucket, search_text, street_key, zone_at
from ..domain.visits import count_visits, merge_split_visits
from .places import place_in_zone
from .position import latest
from .stays import stays
from .use_case import use_case


@use_case
def place_patterns(db, limit=50, rows=None):
    rows = rows if rows is not None else stays(db, limit=None)
    places = {p["id"]: p for p in db.places.saved()}
    labels = {label["id"]: label for label in db.places.labels()}
    clusters, bucket_index, mapping = [], {}, {}
    wide_clusters = []
    for stay in rows:
        key = place_bucket(stay["lat"], stay["lon"])
        radius = stay.get("radius_m") or 0
        target = None
        if stay.get("label_id") is not None:
            target = next((i for i, c in enumerate(clusters) if c.get("label_id") == stay["label_id"]), None)
        elif stay["place_id"] is not None:
            target = next((i for i, c in enumerate(clusters) if c["place_id"] == stay["place_id"]), None)
        else:
            candidates = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        candidates.update(bucket_index.get((key[0]+dx, key[1]+dy, key[2]+dz), ()))
            target = next((i for i in candidates if clusters[i]["place_id"] is None and clusters[i].get("label_id") is None
                           and metres(stay["lat"], stay["lon"], clusters[i]["lat"], clusters[i]["lon"]) <= 75), None)
            if target is None and radius > 75:
                # Laps of one lake or park do not share a centre, so wide areas
                # join when their centres fall inside each other's extent.
                target = next((i for i in wide_clusters if clusters[i]["place_id"] is None and clusters[i].get("label_id") is None
                               and metres(stay["lat"], stay["lon"], clusters[i]["lat"], clusters[i]["lon"])
                               <= min(max(radius, clusters[i]["radius_m"]), 500)), None)
        if target is None:
            place = labels.get(stay.get("label_id")) or places.get(stay["place_id"])
            target = len(clusters)
            clusters.append({"cluster_id": target, "place_id": stay["place_id"], "label_id": stay.get("label_id"),
                             "name": place["name"] if place else None,
                             "lat": stay["lat"], "lon": stay["lon"], "visits": 0, "observed_minutes": 0,
                             "first_seen": stay["started_at"], "last_seen": stay["ended_at"], "radius_m": 0, "activities": {}, "_spans": [], "_members": []})
            bucket_index.setdefault(key, set()).add(target)
        cluster = clusters[target]
        if radius > 75 and cluster["radius_m"] <= 75 and target not in wide_clusters:
            wide_clusters.append(target)
        cluster["radius_m"] = max(cluster["radius_m"], round(radius))
        if stay.get("activity"):
            cluster["activities"][stay["activity"]] = cluster["activities"].get(stay["activity"], 0) + 1
        cluster["_spans"].append((stay["id"], stay.get("previous_stay_id"), stay.get("went_elsewhere")))
        cluster["_members"].append((stay["lat"], stay["lon"], radius, max(stay.get("point_count") or 1, 1)))
        cluster["observed_minutes"] += stay["observed_minutes"]
        if stay["ended_at"] > cluster["last_seen"]:
            cluster["last_seen"] = stay["ended_at"]
        if stay["started_at"] < cluster["first_seen"]:
            cluster["first_seen"] = stay["started_at"]
        mapping[stay["id"]] = target
    # Spans stay attached so resolve_place_patterns can recount after clusters merge.
    counts = count_visits([(*span, index) for index, cluster in enumerate(clusters) for span in cluster["_spans"]])
    for index, cluster in enumerate(clusters):
        # Matching used each cluster's first stay; the place itself sits at the
        # sample-weighted average of its stays (each already the average of its
        # fixes), and extent_m covers their spread for the place-name search.
        members = cluster.pop("_members")
        weight = sum(member[3] for member in members)
        cluster["lat"] = sum(lat * count for lat, _lon, _radius, count in members) / weight
        cluster["lon"] = sum(lon * count for _lat, lon, _radius, count in members) / weight
        cluster["extent_m"] = round(max(metres(cluster["lat"], cluster["lon"], lat, lon) + radius
                                        for lat, lon, radius, _count in members))
        cluster["visits"] = counts.get(index, 0)
        cluster["observed_minutes"] = round(cluster["observed_minutes"], 1)
        cluster["display_name"] = cluster["name"] or f"{cluster['lat']:.5f}, {cluster['lon']:.5f}"
        zone = places.get(cluster["place_id"]) or zone_at(places.values(), cluster["lat"], cluster["lon"])
        cluster["zone_id"], cluster["zone"] = (zone["id"], zone["name"]) if zone else (None, None)
    ordered = sorted(clusters, key=lambda c: (c["observed_minutes"], c["visits"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)], mapping


@use_case
def resolve_place_patterns(db, patterns):
    """Resolve coordinate clusters and merge clusters that describe one real place.

    Provider IDs are authoritative. Equal normalized names are also merged when
    their coordinates are within 2.5 km, which joins large parks and lakes while
    keeping distant branches of a chain separate. A place with no name, only a
    street address, joins Home or the nearest name correction on the same
    street within ADDRESS_MERGE_M (indoor GPS drifted out of Home's circle),
    else other address-only places there. Inside another saved place (a zone)
    a place is named after the building it is in, else after the zone.
    """
    clusters, stay_mapping = patterns
    saved = {place["id"]: place for place in db.places.saved()}
    labels = {label["id"]: label for label in db.places.labels()}
    resolved = []
    for cluster in clusters:
        place = saved.get(cluster["place_id"])
        label = labels.get(cluster.get("label_id"))
        found = (label_place(label) if label else place if place
                 else place_in_zone(db, cluster["lat"], cluster["lon"], saved.get(cluster.get("zone_id")),
                                    cluster.get("extent_m"), lookup=naming.place_for_coordinate))
        name = cluster.get("name") or found.get("name") or found.get("address") or ""
        categories = found.get("categories") if isinstance(found.get("categories"), list) else []
        category = found.get("category") or (categories[0] if categories else "")
        resolved.append({**cluster, "name": name, "display_name": name or cluster["display_name"],
                         "address": found.get("address") or "", "category": category or "",
                         "categories": categories, "provider_place_id": found.get("provider_place_id") or "",
                         "saved": bool(place), "label_id": cluster.get("label_id") or found.get("label_id"),
                         "_street": street_key(found.get("address")),
                         "_anchor": bool(place or label), "_address_only": not (place or label or found.get("named", True))})

    parent = list(range(len(resolved)))
    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(a, b):
        a, b = root(a), root(b)
        if a != b:
            parent[b] = a

    saved_ids, provider_ids, names, streets = {}, {}, {}, {}
    for index, place in enumerate(resolved):
        if place.get("place_id") is not None:
            key = place["place_id"]
            if key in saved_ids:
                union(index, saved_ids[key])
            else:
                saved_ids[key] = index
        if place.get("provider_place_id"):
            key = place["provider_place_id"]
            if key in provider_ids:
                union(index, provider_ids[key])
            else:
                provider_ids[key] = index
        name = normalized_place_name(place.get("name"))
        if name:
            for other in names.get(name, []):
                if metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]) <= 2500:
                    union(index, other)
            names.setdefault(name, []).append(index)
    # Address-only places: join the nearest saved place or correction on the
    # same street, else other address-only places there (never bridging two
    # saved places through an address).
    anchors = {}
    for index, place in enumerate(resolved):
        if place["_anchor"] and place["_street"]:
            anchors.setdefault(place["_street"], []).append(index)
    for index, place in enumerate(resolved):
        if not (place["_address_only"] and place["_street"]):
            continue
        nearby = [(metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]), other)
                  for other in anchors.get(place["_street"], [])]
        nearby = [item for item in nearby if item[0] <= ADDRESS_MERGE_M]
        if nearby:
            union(min(nearby)[1], index)
            continue
        for other in streets.get(place["_street"], []):
            if metres(place["lat"], place["lon"], resolved[other]["lat"], resolved[other]["lon"]) <= ADDRESS_MERGE_M:
                union(index, other)
        streets.setdefault(place["_street"], []).append(index)

    grouped, raw_to_merged = [], {}
    by_root = {}
    for index, place in enumerate(resolved):
        group_root = root(index)
        target = by_root.get(group_root)
        weight = max(float(place.get("observed_minutes") or 0), 1.0)
        if target is None:
            target = len(grouped)
            by_root[group_root] = target
            grouped.append({**place, "cluster_id": target, "_weight": weight})
        else:
            item = grouped[target]
            total_weight = item["_weight"] + weight
            item["lat"] = (item["lat"] * item["_weight"] + place["lat"] * weight) / total_weight
            item["lon"] = (item["lon"] * item["_weight"] + place["lon"] * weight) / total_weight
            item["_weight"] = total_weight
            item["visits"] += place["visits"]
            item["observed_minutes"] = round(item["observed_minutes"] + place["observed_minutes"], 1)
            item["last_seen"] = max(item.get("last_seen") or "", place.get("last_seen") or "") or None
            if "first_seen" in item or "first_seen" in place:
                values = [value for value in (item.get("first_seen"), place.get("first_seen")) if value]
                item["first_seen"] = min(values) if values else None
            if place.get("saved") and not item.get("saved"):
                for field in ("place_id", "name", "display_name", "address", "category", "provider_place_id"):
                    item[field] = place.get(field) or item.get(field)
            else:
                for field in ("address", "category", "provider_place_id"):
                    item[field] = item.get(field) or place.get(field) or ""
            item["saved"] = item.get("saved") or place.get("saved")
            item["label_id"] = item.get("label_id") or place.get("label_id")
            if not item.get("zone_id"):
                item["zone_id"], item["zone"] = place.get("zone_id"), place.get("zone")
            item["categories"] = list(dict.fromkeys((item.get("categories") or []) + (place.get("categories") or [])))
            item["radius_m"] = max(item.get("radius_m") or 0, place.get("radius_m") or 0)
            activities = dict(item.get("activities") or {})
            for activity, count in (place.get("activities") or {}).items():
                activities[activity] = activities.get(activity, 0) + count
            item["activities"] = activities
            if "_spans" in item or "_spans" in place:
                item["_spans"] = (item.get("_spans") or []) + (place.get("_spans") or [])
        raw_to_merged[place["cluster_id"]] = target
    counts = count_visits([(*span, index) for index, place in enumerate(grouped) for span in place.get("_spans") or []])
    for index, place in enumerate(grouped):
        for key in ("_weight", "_street", "_anchor", "_address_only"):
            place.pop(key, None)
        if place.pop("_spans", None) is not None:
            place["visits"] = counts.get(index, 0)
    mapping = {stay_id: raw_to_merged[cluster_id] for stay_id, cluster_id in stay_mapping.items()}
    grouped.sort(key=lambda item: (item["observed_minutes"], item["visits"]), reverse=True)
    # Sorting changes public cluster IDs, so remap stays and IDs together.
    old_to_new = {item["cluster_id"]: index for index, item in enumerate(grouped)}
    for index, item in enumerate(grouped):
        item["cluster_id"] = index
    mapping = {stay_id: old_to_new[cluster_id] for stay_id, cluster_id in mapping.items()}
    return grouped, mapping


@use_case
def visited_places(db, limit=500):
    """Observed places ordered by total sampled time, with cache-first labels."""
    return place_overview(db, limit)["visited"]


@use_case
def place_overview(db, limit=100):
    """Visited places (without Home) and visit totals for every saved place (with Home), from one pass.

    A saved place is a zone, so its totals cover every place inside it: one
    visit to campus is one visit however many buildings it took in.
    """
    rows = stays(db, limit=None)
    clusters, mapping = resolve_place_patterns(db, place_patterns(db, limit=None, rows=rows))
    by_cluster = {item["cluster_id"]: item for item in clusters}
    # Home also owns indoor GPS drift that its place absorbed from down the street.
    zone_of = {stay["id"]: by_cluster[mapping[stay["id"]]].get("place_id") or stay.get("zone_id") for stay in rows}
    inside = [stay for stay in rows if zone_of[stay["id"]] is not None]
    visits = count_visits([(stay["id"], stay.get("previous_stay_id"), stay.get("went_elsewhere"), zone_of[stay["id"]])
                            for stay in inside])
    saved = {}
    for stay in inside:
        zone_id = zone_of[stay["id"]]
        item = saved.setdefault(str(zone_id), {"visits": visits.get(zone_id, 0), "observed_minutes": 0, "last_seen": None})
        item["observed_minutes"] = round(item["observed_minutes"] + stay["observed_minutes"], 1)
        item["last_seen"] = max(item["last_seen"] or "", stay["ended_at"])
    return {"visited": [item for item in clusters if not is_home_place(item)][:min(max(int(limit), 1), 5000)],
            "saved": saved}


@use_case
def search_visits(db, query, start=None, end=None, limit=50, timezone_name=None):
    """Find when you were at a place by name, address, or category.

    Matching is exact on words, not semantic: "costco" never returns Sam's
    Club. An LLM caller supplies synonyms or categories when a query misses.
    """
    terms = search_text(query).split()
    if not terms or len(query) > 200:
        raise ValueError("query is required (max 200 characters)")
    timezone_name = timezone_name or db.settings.get()["timezone"]
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown timezone") from exc
    rows = stays(db, start, end, limit=None)
    clusters, mapping = resolve_place_patterns(db, place_patterns(db, limit=None, rows=rows))
    matched = {}
    for cluster in clusters:
        words = " ".join(search_text(value) for value in (
            cluster.get("name"), cluster.get("display_name"), cluster.get("address"), cluster.get("zone"),
            cluster.get("category"), *(cluster.get("categories") or []))).split()
        # Word prefixes: "gas" finds "Gasoline", "lake" does not find "Blake Street".
        if all(any(word.startswith(term) for word in words) for term in terms):
            matched[cluster["cluster_id"]] = cluster
    visits = []
    for stay in merge_split_visits(list(reversed(rows)), lambda stay: mapping.get(stay["id"])):
        place = matched.get(mapping.get(stay["id"]))
        if place is None:
            continue
        arrival = datetime.fromisoformat(stay["started_at"]).astimezone(zone)
        departure = datetime.fromisoformat(stay["ended_at"]).astimezone(zone)
        visits.append({"date": arrival.date().isoformat(), "weekday": arrival.strftime("%A"),
                       "arrival": arrival.isoformat(timespec="minutes"),
                       "departure": departure.isoformat(timespec="minutes"),
                       "observed_minutes": stay["observed_minutes"], "ongoing": bool(stay["ongoing"]),
                       "place": place["name"] or place["display_name"], "address": place.get("address") or "",
                       "zone": place.get("zone"), "kind": stay["kind"], "activity": stay.get("activity"),
                       "lat": stay["lat"], "lon": stay["lon"], "source": stay["source"],
                       "stay_id": stay["id"], "started_at": stay["started_at"], "ended_at": stay["ended_at"]})
    visits.sort(key=lambda visit: visit["arrival"], reverse=True)
    places = [{"name": place["name"] or place["display_name"], "address": place.get("address") or "",
               "zone": place.get("zone"), "category": place.get("category") or "", "visits": place["visits"],
               "observed_minutes": place["observed_minutes"], "last_seen": place.get("last_seen"),
               "saved": bool(place.get("saved"))}
              for place in sorted(matched.values(), key=lambda item: item.get("last_seen") or "", reverse=True)]
    return {"query": query, "timezone": timezone_name, "total_visits": len(visits), "places": places,
            "visits": visits[:min(max(int(limit), 1), 1000)],
            "note": f"Visits are observed stays of at least {db.settings.get()['stay_min_minutes']} minutes; sparse GPS can miss short stops"}


@use_case
def transitions(db, limit=50, patterns=None, rows=None):
    rows = rows if rows is not None else stays(db, limit=None)
    clusters, mapping = patterns if patterns is not None else resolve_place_patterns(
        db, place_patterns(db, limit=None, rows=rows))
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
        edge["from_lat"], edge["from_lon"] = a["lat"], a["lon"]
        edge["to_lat"], edge["to_lon"] = b["lat"], b["lon"]
        edge["weighted_score"] = round(edge["weighted_score"], 4)
    ordered = sorted(result, key=lambda e: (e["weighted_score"], e["count"]), reverse=True)
    return ordered if limit is None else ordered[:min(int(limit), 5000)]


@use_case
def predict_next(db, patterns=None, edges=None):
    current = latest(db)
    if not current:
        return {"prediction": None, "reason": "no location data"}
    clusters, _ = patterns if patterns is not None else resolve_place_patterns(db, place_patterns(db, limit=None))
    origin = next((c for c in clusters if metres(current["lat"], current["lon"], c["lat"], c["lon"])
                   <= max(75, c.get("radius_m") or 0)), None)
    if not origin:
        return {"prediction": None, "reason": "current point is not at an observed stay"}
    edges = [e for e in (edges if edges is not None else transitions(db, limit=None, patterns=patterns)) if e["from_cluster_id"] == origin["cluster_id"]]
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


@use_case
def insights(db, limit=50):
    rows = stays(db, limit=None)
    patterns = resolve_place_patterns(db, place_patterns(db, limit=None, rows=rows))
    edges = transitions(db, limit=None, patterns=patterns, rows=rows)
    unlabeled = [p for p in patterns[0] if p["place_id"] is None and not p.get("label_id") and p.get("zone_id") is None][:limit]
    return {"frequent_places": patterns[0][:limit],
            "unlabeled_places": unlabeled,
            "transitions": edges[:limit],
            "prediction": predict_next(db, patterns=patterns, edges=edges),
            "observed_stays": len(rows)}
