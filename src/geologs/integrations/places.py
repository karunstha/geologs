"""Place providers: Photon (self-hosted), Geoapify and Mapbox.

Each answers the same four questions, in the shape the rest of GeoLogs uses:
search by name, named places near a position, the name of one position, and
(Mapbox only) a search result's coordinates. Nothing here stores anything;
callers cache what these return.
"""
import json
import math
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..domain.geo import metres


def reverse_cache_radius(result_type):
    """Only reuse precise addresses nearby; a city bounding box is not a safe boundary."""
    kind = str(result_type or "").lower()
    if kind in ("building", "house", "amenity", "place", "address"):
        return 15
    if kind == "street":
        return 30
    return 0


def json_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "GeoLogs/0.1 (personal self-hosted location archive)"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def provider_request(path, params):
    key = config.geoapify_key()
    if not key:
        raise ValueError("Set GEOAPIFY_API_KEY for geocoding; saved places and recorded paths work without it")
    params = {**params, "apiKey": key}
    url = "https://api.geoapify.com" + path + "?" + urllib.parse.urlencode(params)
    return json_get(url)


def photon_result(feature):
    props = feature.get("properties", {})
    lon, lat = feature["geometry"]["coordinates"][:2]
    label = ", ".join(str(props[k]) for k in ("name", "street", "city", "state", "country") if props.get(k))
    place_name = str(props.get("name") or "").strip()
    category = ".".join(str(props[key]) for key in ("osm_key", "osm_value") if props.get(key))
    provider_place_id = ":".join(str(props[key]) for key in ("osm_type", "osm_id") if props.get(key))
    return {"name": label or "Unknown place", "place_name": place_name,
            "lat": lat, "lon": lon, "address": label, "category": category,
            "categories": [category] if category else [], "provider_place_id": provider_place_id}


def geoapify_reverse_result(value, lat, lon):
    """Keep Geoapify's place name separate from its formatted address."""
    address = str(value.get("formatted") or "").strip()
    if not address:
        address = ", ".join(str(value.get(key)).strip() for key in ("address_line1", "address_line2") if value.get(key))
    place_name = str(value.get("name") or "").strip()
    datasource = value.get("datasource") if isinstance(value.get("datasource"), dict) else {}
    raw = datasource.get("raw") if isinstance(datasource.get("raw"), dict) else {}
    if not place_name:
        place_name = str(raw.get("name") or "").strip()
    # Geoapify documents address_line1 as the amenity name for amenity results.
    if not place_name and value.get("result_type") == "amenity":
        place_name = str(value.get("address_line1") or "").strip()
    categories = value.get("categories") if isinstance(value.get("categories"), list) else []
    category = value.get("category") or (categories[0] if categories else None)
    return {"name": place_name or address or "Unknown place",
            "place_name": place_name, "address": address, "lat": lat, "lon": lon,
            "result_type": value.get("result_type"), "category": category,
            "categories": categories, "provider_place_id": value.get("place_id"),
            "distance_m": value.get("distance")}


# Place types a person visits. Roads, parking, transit stops, boundaries, and
# street furniture would otherwise be "nearest" to almost every stay.
#
# Geoapify cannot mix these two groups in one request: adding any category of
# the second group made it drop the first entirely (a Kroger 558 m away and the
# park the point was in both vanished, even inside a circle filter). Each group
# is therefore its own request, and the nearest named place of both wins.
NEAREST_PLACE_CATEGORY_GROUPS = (
    ",".join(("activity", "adult", "building", "camping", "catering", "childcare", "commercial", "education",
              "emergency", "entertainment", "healthcare", "heritage", "leisure", "memorial", "natural", "office",
              "pet", "production", "religion", "rental", "service", "ski")),
    ",".join(("accommodation", "airport", "beach", "national_park", "sport", "tourism")),
)
SKIPPED_PLACE_SUFFIXES = (".atm", ".vending_machine", ".charging_station", ".bicycle_parking", ".post_box")
# Search radii, smallest first. A stay's averaged position is searched within
# the smallest radius that covers its extent; 150 m covers a big-box store
# seen from its parking lot. Buckets keep the reverse cache reusable.
NEAREST_PLACE_RADII_M = (150, 300, 600, 1000)
PHOTON_UNNAMED_KEYS = ("highway", "boundary", "place", "railway", "landuse", "waterway", "power")


def nearest_place_radius(extent_m=None):
    """Search radius for a position whose samples spread over `extent_m`."""
    extent = float(extent_m or 0)
    return next((radius for radius in NEAREST_PLACE_RADII_M if radius >= extent), NEAREST_PLACE_RADII_M[-1])


def geoapify_candidates(data):
    """Named places in a Places API response, in its order, each with its own position."""
    candidates = []
    for feature in data.get("features", []) if isinstance(data, dict) else []:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        name = str(props.get("name") or "").strip()
        categories = props.get("categories") if isinstance(props.get("categories"), list) else []
        if not name or any(str(category).endswith(SKIPPED_PLACE_SUFFIXES) for category in categories):
            continue
        address = str(props.get("formatted") or "").strip()
        if not address:
            address = ", ".join(str(props.get(key)).strip() for key in ("address_line1", "address_line2") if props.get(key))
        candidates.append({
            "name": name, "place_name": name, "address": address,
            # The deepest category ("commercial.supermarket") says the most.
            "category": max(categories, key=lambda category: str(category).count("."), default=None),
            "categories": categories, "provider_place_id": props.get("place_id"),
            "distance_m": props.get("distance"), "place_lat": props.get("lat"), "place_lon": props.get("lon"),
            "source": "geoapify_places",
        })
    return candidates


def geoapify_nearest_result(data, lat, lon):
    """The first named place in a distance-ordered Places API response."""
    candidates = geoapify_candidates(data)
    return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"} if candidates else None


PLACES_PROVIDERS = {
    "geoapify": {"label": "Geoapify", "env": "GEOAPIFY_API_KEY"},
    "mapbox": {"label": "Mapbox", "env": "MAPBOX_ACCESS_TOKEN"},
    "photon": {"label": "Photon (self-hosted)", "env": "GEOLOGS_PHOTON_URL"},
}
# Without a choice in Settings, the first configured provider is used; a
# self-hosted Photon keeps the precedence it always had.
PLACES_PROVIDER_ORDER = ("photon", "geoapify", "mapbox")
# Mapbox POI categories that would otherwise name almost every stay.
MAPBOX_SKIPPED_CATEGORIES = {"atm", "parking", "parking_lot", "parking_garage", "bus_stop", "bus_station",
                             "ev_charging_station", "charging_station", "post_box", "vending_machine"}
# Reverse lookup only returns POIs associated with the exact coordinate. Nearby
# browsing uses Search Box category searches instead. Mapbox's category catalog
# has no parent/child relationship, so cover its broad visit-relevant groups and
# the separate grocery/wholesale groups that do not reliably appear in Shopping.
MAPBOX_NEARBY_CATEGORIES = (
    "food_and_drink", "services", "shopping", "office", "education", "grocery", "outdoors", "lodging",
    "wholesale_store", "place_of_worship", "sports", "tourist_attraction", "entertainment",
)
MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS = .12


def configured_places_providers():
    return {name: config.place_provider_configured(info["env"]) for name, info in PLACES_PROVIDERS.items()}


def mapbox_request(path, params):
    token = config.mapbox_token()
    if not token:
        raise ValueError("Set MAPBOX_ACCESS_TOKEN to use Mapbox")
    return json_get("https://api.mapbox.com" + path + "?" + urllib.parse.urlencode({**params, "access_token": token}))


def _mapbox_position(feature):
    props = feature.get("properties") or {}
    coordinates = props.get("coordinates") if isinstance(props.get("coordinates"), dict) else {}
    if coordinates.get("latitude") is not None and coordinates.get("longitude") is not None:
        return float(coordinates["latitude"]), float(coordinates["longitude"])
    lon, lat = (feature.get("geometry") or {}).get("coordinates", [None, None])[:2]
    return (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)


def mapbox_place(feature):
    """A Search Box feature in the shape the rest of GeoLogs uses."""
    props = feature.get("properties") or {}
    lat, lon = _mapbox_position(feature)
    name = str(props.get("name") or "").strip()
    address = str(props.get("full_address") or "").strip() or ", ".join(
        part for part in (name, str(props.get("place_formatted") or "").strip()) if part)
    category_ids = [str(item) for item in props.get("poi_category_ids") or []]
    categories = list(dict.fromkeys(category_ids + [str(item) for item in props.get("poi_category") or []]))
    is_poi = props.get("feature_type") == "poi"
    return {"name": name or address or "Unknown place", "place_name": name if is_poi else "",
            "lat": lat, "lon": lon, "address": address,
            "category": category_ids[0] if category_ids else (props.get("feature_type") or None),
            "categories": categories, "provider_place_id": props.get("mapbox_id"),
            "result_type": props.get("feature_type")}


def mapbox_candidates(data, lat, lon, radius):
    """Named POIs within `radius`, nearest first; Search Box /reverse takes no radius and states no order."""
    candidates = []
    for feature in data.get("features", []) if isinstance(data, dict) else []:
        place = mapbox_place(feature)
        if not place["place_name"] or place["lat"] is None:
            continue
        if MAPBOX_SKIPPED_CATEGORIES & set(place["categories"]):
            continue
        distance = metres(lat, lon, place["lat"], place["lon"])
        if distance <= radius:
            candidates.append({**place, "place_lat": place["lat"], "place_lon": place["lon"],
                               "distance_m": round(distance), "source": "mapbox_searchbox"})
    return sorted(candidates, key=lambda place: place["distance_m"])


def _mapbox_bbox(lat, lon, radius):
    """Bounding box for a radius in metres; None when it would cross the antimeridian."""
    lat_delta = radius / 111320
    lon_delta = radius / (111320 * max(.01, math.cos(math.radians(lat))))
    west, east = lon - lon_delta, lon + lon_delta
    if west < -180 or east > 180:
        return None
    return ",".join(str(value) for value in (west, max(-90, lat - lat_delta), east, min(90, lat + lat_delta)))


def _mapbox_nearby_candidates(lat, lon, radius):
    """POIs around a position from category search, merged with exact reverse hits."""
    bbox = _mapbox_bbox(lat, lon, radius)
    request_lock, last_request = threading.Lock(), [0.0]

    def limited_request(path, params):
        # Space request starts below Mapbox's default ten-per-second limit while
        # still allowing their network waits to overlap.
        with request_lock:
            delay = last_request[0] + MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            last_request[0] = time.monotonic()
        return mapbox_request(path, params)

    def fetch(category):
        try:
            if category is None:
                data = limited_request("/search/searchbox/v1/reverse", {
                    "longitude": lon, "latitude": lat, "types": "poi", "limit": 10,
                })
            else:
                params = {"proximity": f"{lon},{lat}", "limit": 25, "exclude_fields": "photos,reviews"}
                if bbox:
                    params["bbox"] = bbox
                data = limited_request(f"/search/searchbox/v1/category/{category}", params)
            return mapbox_candidates(data, lat, lon, radius), None
        except (OSError, TypeError, KeyError, ValueError) as exc:
            return [], exc

    # Four workers avoid a long serial wait across the category requests.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, (None, *MAPBOX_NEARBY_CATEGORIES)))
    if all(error is not None for _group, error in results):
        raise results[0][1]
    unique = {}
    for place in (place for group, _error in results for place in group):
        key = place.get("provider_place_id") or (
            place["name"].casefold(), round(place["place_lat"], 6), round(place["place_lon"], 6))
        previous = unique.get(key)
        if previous is None or place["distance_m"] < previous["distance_m"]:
            unique[key] = place
    return sorted(unique.values(), key=lambda place: place["distance_m"])


def mapbox_nearest_result(data, lat, lon, radius):
    candidates = mapbox_candidates(data, lat, lon, radius)
    return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"} if candidates else None


# Place search looks near you first: within this distance a match is "nearby".
SEARCH_NEARBY_M = 100_000


def _geoapify_results(params):
    data = provider_request("/v1/geocode/search", {"limit": 5, "format": "json", **params})
    return [{"name": r.get("name") or r.get("formatted") or params["text"],
             "place_name": r.get("name") or "", "lat": r["lat"], "lon": r["lon"],
             "address": r.get("formatted", ""),
             "category": r.get("category") or ((r.get("categories") or [None])[0]),
             "categories": r.get("categories") if isinstance(r.get("categories"), list) else [],
             "provider_place_id": r.get("place_id")} for r in data.get("results", [])]


def _geoapify_search(query, bias):
    """Matches near you, then the best matches anywhere.

    A proximity bias alone lets a strong text match on another continent win
    (an exact "Tea Capital" in India over a tea shop across town), so the
    nearby area is searched on its own first.
    """
    if not bias:
        return _geoapify_results({"text": query})
    near = {"bias": f"proximity:{bias[1]},{bias[0]}"}
    return (_geoapify_results({"text": query, **near, "filter": f"circle:{bias[1]},{bias[0]},{SEARCH_NEARBY_M}"})
            + _geoapify_results({"text": query, **near}))


def _photon_search(query, bias):
    photon = config.photon_url()
    params = {"q": query, "limit": 5}
    if bias:
        params.update({"lat": bias[0], "lon": bias[1]})
    data = json_get(photon + "/api?" + urllib.parse.urlencode(params))
    return [photon_result(r) for r in data.get("features", [])]


def _mapbox_search(query, bias):
    """Mapbox's /suggest, made for finding places by name near you.

    Its /forward endpoint reads words such as "capital" as a region, so
    "Tea Capital" returned tea shops in Kuwait City; /suggest returns the shop
    across town first. Suggestions carry no coordinates: place_details()
    fetches them, in the same billing session, for the one that is chosen.
    """
    session = str(uuid.uuid4())
    params = {"q": query[:256], "limit": 8, "session_token": session}
    if bias:
        params["proximity"] = f"{bias[1]},{bias[0]}"
        params["origin"] = f"{bias[1]},{bias[0]}"
    data = mapbox_request("/search/searchbox/v1/suggest", params)
    results = []
    for item in data.get("suggestions", []):
        if not item.get("mapbox_id"):
            continue
        name = str(item.get("name") or "").strip()
        address = str(item.get("full_address") or "").strip() or ", ".join(
            part for part in (name, str(item.get("place_formatted") or "").strip()) if part)
        category_ids = [str(value) for value in item.get("poi_category_ids") or []]
        results.append({"name": name or address or "Unknown place",
                        "place_name": name if item.get("feature_type") == "poi" else "",
                        "lat": None, "lon": None, "address": address,
                        "category": category_ids[0] if category_ids else item.get("feature_type"),
                        "categories": category_ids, "provider_place_id": item["mapbox_id"],
                        "distance_m": round(item["distance"]) if item.get("distance") is not None else None,
                        "session": session})
    return results


def _photon_features(lat, lon, radius, limit=10):
    photon = config.photon_url()
    data = json_get(photon + "/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "radius": radius / 1000, "limit": limit}))
    features = data.get("features") or []
    named = [feature for feature in features
             if (feature.get("properties") or {}).get("name")
             and (feature.get("properties") or {}).get("osm_key") not in PHOTON_UNNAMED_KEYS]
    return features, named


def _photon_candidates(lat, lon, radius):
    candidates = []
    for feature in _photon_features(lat, lon, radius, limit=15)[1]:
        place = photon_result(feature)
        candidates.append({**place, "name": place["place_name"], "place_lat": place["lat"], "place_lon": place["lon"],
                           "distance_m": round(metres(lat, lon, place["lat"], place["lon"])), "source": "photon"})
    return sorted(candidates, key=lambda place: place["distance_m"])


def _photon_lookup(lat, lon, radius):
    features, named = _photon_features(lat, lon, radius)
    feature = named[0] if named else (features[0] if features else None)
    value = photon_result(feature) if feature else {"name": "Unknown place", "place_name": "", "address": ""}
    if named:
        value["place_lat"], value["place_lon"] = value["lat"], value["lon"]
    else:
        value["place_name"] = ""
    kind = "place" if named else (feature or {}).get("properties", {}).get("type")
    return {**value, "name": value.get("place_name") or value.get("address") or "Unknown place",
            "lat": lat, "lon": lon}, kind


def _geoapify_candidates(lat, lon, radius):
    features = []
    for categories in NEAREST_PLACE_CATEGORY_GROUPS:
        try:
            data = provider_request("/v2/places", {
                "categories": categories, "conditions": "named",
                "filter": f"circle:{lon},{lat},{radius}", "bias": f"proximity:{lon},{lat}", "limit": 20,
            })
        except (OSError, TypeError, KeyError, ValueError):
            continue  # the other group, or the address fallback, still names the point
        if isinstance(data, dict):
            features.extend(data.get("features") or [])
    # Each group arrives ordered by distance; merge so the nearest of both comes first.
    features.sort(key=lambda feature: (((feature or {}).get("properties") or {}).get("distance") is None,
                                       ((feature or {}).get("properties") or {}).get("distance") or 0))
    return geoapify_candidates({"features": features})


def _geoapify_lookup(lat, lon, radius):
    candidates = _geoapify_candidates(lat, lon, radius)
    if candidates:
        return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"}, "place"
    data = provider_request("/v1/geocode/reverse", {"lat": lat, "lon": lon, "format": "json"})
    value = (data.get("results") or [{}])[0]
    return {**geoapify_reverse_result(value, lat, lon), "source": "geoapify_reverse"}, value.get("result_type")


def _mapbox_candidates(lat, lon, radius):
    return mapbox_candidates(mapbox_request("/search/searchbox/v1/reverse", {
        "longitude": lon, "latitude": lat, "types": "poi", "limit": 10}), lat, lon, radius)


def _mapbox_lookup(lat, lon, radius):
    try:
        candidates = _mapbox_candidates(lat, lon, radius)
    except (OSError, TypeError, KeyError, ValueError):
        candidates = []
    if candidates:
        return {**candidates[0], "lat": lat, "lon": lon, "result_type": "place"}, "place"
    data = mapbox_request("/search/searchbox/v1/reverse", {
        "longitude": lon, "latitude": lat, "types": "address,street", "limit": 1})
    feature = (data.get("features") or [None])[0]
    if feature is None:
        return {"name": "Unknown place", "place_name": "", "address": "", "lat": lat, "lon": lon,
                "source": "mapbox_searchbox"}, None
    place = mapbox_place(feature)
    return {**place, "name": place["address"] or place["name"], "place_name": "", "lat": lat, "lon": lon,
            "source": "mapbox_searchbox"}, place["result_type"]


def lookup_cache_key(name):
    # Versions the reverse cache and keeps providers apart. Geoapify v2 drops
    # lookups made with the single mixed-category request, which cached
    # street addresses where named places existed.
    return {"photon": "photon:nearest-v1:" + config.photon_url(),
            "geoapify": "geoapify:nearest-v2", "mapbox": "mapbox:nearest-v1"}[name]


def search(provider, query, bias):
    """Places matching `query`, biased towards `bias` (lat, lon) when given."""
    return {"photon": _photon_search, "geoapify": _geoapify_search, "mapbox": _mapbox_search}[provider](query, bias)


def nearby(provider, lat, lon, radius):
    """Named places within `radius` metres, nearest first."""
    return {"photon": _photon_candidates, "geoapify": _geoapify_candidates,
            "mapbox": _mapbox_nearby_candidates}[provider](lat, lon, radius)


def lookup(provider, lat, lon, radius):
    """The nearest named place within `radius`, else the street address, with its result type."""
    return {"photon": _photon_lookup, "geoapify": _geoapify_lookup, "mapbox": _mapbox_lookup}[provider](lat, lon, radius)


def retrieve(provider_place_id, session):
    """Coordinates for a Mapbox suggestion, which arrives without them."""
    data = mapbox_request("/search/searchbox/v1/retrieve/" + urllib.parse.quote(provider_place_id, safe=""),
                          {"session_token": session})
    feature = (data.get("features") or [None])[0]
    if feature is None:
        raise ValueError("place not found")
    return mapbox_place(feature)


def search_cache_name(provider):
    """Where search results from a provider are cached; Photon's depend on its server."""
    return {"photon": "photon:" + config.photon_url(),
            "geoapify": "geoapify", "mapbox": "mapbox:searchbox-v1"}[provider]


def nearby_cache_key(provider):
    """Mapbox's category-backed list is versioned apart from its reverse lookup."""
    return "mapbox:categories-v1" if provider == "mapbox" else lookup_cache_key(provider)
