"""Naming positions: the place provider chosen in Settings, behind caches, and your own names first."""
import threading
import uuid

from .. import config
from ..domain.geo import metres
from ..domain.places import is_home_place, label_at, label_place, saved_place_as_found, ZONE_PLACE_REACH_M
from ..integrations import places as providers
from ..integrations.places import configured_places_providers, nearest_place_radius, PLACES_PROVIDER_ORDER, reverse_cache_radius, SEARCH_NEARBY_M
from .position import latest
from .use_case import use_case


# One provider request at a time: providers limit requests per key (Mapbox
# to ten a second, and a nearby search is fourteen). Only the request waits
# here; a lookup the cache answers never does.
_provider_turn = threading.Lock()


@use_case
def places_provider(db):
    """The provider chosen in Settings when it is configured, else the first configured one."""
    configured = configured_places_providers()
    chosen = db.settings.get().get("places_provider")
    if chosen and configured.get(chosen):
        return chosen
    return next((name for name in PLACES_PROVIDER_ORDER if configured[name]), None)


@use_case
def places_provider_identity(db):
    """Changes whenever place names could change: the provider, and Photon's server."""
    name = places_provider(db)
    return f"photon:{config.photon_url()}" if name == "photon" else str(name)


def _require_provider(db):
    name = places_provider(db)
    if name is None:
        raise ValueError("Set GEOAPIFY_API_KEY, MAPBOX_ACCESS_TOKEN, or GEOLOGS_PHOTON_URL for place names and "
                         "search; saved places and recorded paths work without one")
    return name


@use_case
def place_details(db, provider_place_id, session=None):
    """Coordinates for a search result that arrived without them (Mapbox suggestions)."""
    if _require_provider(db) != "mapbox":
        raise ValueError("place details are only needed for Mapbox search results")
    provider_place_id = str(provider_place_id or "").strip()
    if not provider_place_id or len(provider_place_id) > 300:
        raise ValueError("id is required")
    key = "place:" + provider_place_id
    cached = db.caches.lookup(key, "mapbox:searchbox-v1")
    if cached is not None:
        return cached
    place = providers.retrieve(provider_place_id, session or str(uuid.uuid4()))
    if place["lat"] is None:
        raise ValueError("place has no coordinates")
    db.caches.save_lookup(key, "mapbox:searchbox-v1", place)
    return place


@use_case
def geocode(db, query, lat=None, lon=None, resolve=5):
    """Places matching `query`, nearby ones first, each with its distance from you.

    `resolve` is how many of the leading results must carry coordinates:
    Mapbox suggestions don't, and each lookup costs, so the browser asks for
    none and fetches only the place you pick (place_details).
    """
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError("query is required (max 200 characters)")
    if (lat is None) != (lon is None):
        raise ValueError("lat and lon must be provided together")
    if lat is not None:
        bias = (float(lat), float(lon))
        if not (-90 <= bias[0] <= 90 and -180 <= bias[1] <= 180):
            raise ValueError("invalid search bias coordinates")
    else:
        position = latest(db)
        bias = (float(position["lat"]), float(position["lon"])) if position else None
    name = _require_provider(db)
    # Cache by nearby search area; a result from another city must not win later.
    key = "search-v2:" + query.casefold() + (f":{bias[0]:.2f}:{bias[1]:.2f}" if bias else ":unbiased")
    provider = providers.search_cache_name(name)
    results = db.caches.lookup(key, provider)
    if results is None:
        results = providers.search(name, query, bias)
        db.caches.save_lookup(key, provider, results)
    unique, seen = [], set()
    for place in results:
        identity = place.get("provider_place_id") or (place["name"], round(place["lat"] or 0, 4), round(place["lon"] or 0, 4))
        if identity in seen:
            continue
        seen.add(identity)
        if bias and place.get("lat") is not None:
            place = {**place, "distance_m": round(metres(bias[0], bias[1], place["lat"], place["lon"]))}
        unique.append(place)
    # Nearby matches first, in the provider's order; then the rest, nearest first.
    def rank(item):
        index, place = item
        distance = place.get("distance_m")
        if distance is None:
            return (1, 0, index)
        return (0, 0, index) if distance <= SEARCH_NEARBY_M else (0, 1, distance)
    ordered = [place for _index, place in sorted(enumerate(unique), key=rank)][:8]
    for index, place in enumerate(ordered[:resolve]):
        if place.get("lat") is None:
            details = place_details(db, place["provider_place_id"], place.get("session"))
            ordered[index] = {**place, "lat": details["lat"], "lon": details["lon"],
                              "address": place["address"] or details["address"]}
    # Callers that need coordinates get only the results that have them.
    return [place for place in ordered if place.get("lat") is not None] if resolve else ordered


@use_case
def nearby_places(db, lat, lon, radius_m=None, limit=15):
    """Named places around a position, nearest first, from the provider chosen in Settings.

    Used to correct a visit that was named after the wrong neighbour in a busy
    place, and to match name-only reminders against every place close by.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    radius = nearest_place_radius(radius_m)
    name = _require_provider(db)
    provider = providers.nearby_cache_key(name)
    key = provider + f":candidates:r{radius}"
    candidates = db.caches.reverse_region(key, lat, lon)
    if candidates is None:
        with _provider_turn:
            candidates = providers.nearby(name, lat, lon, radius)
        db.caches.save_reverse_region(key, lat, lon, 15, candidates)
    places = []
    for place in candidates:
        if place.get("place_lat") is not None and place.get("place_lon") is not None:
            place = {**place, "distance_m": round(metres(lat, lon, place["place_lat"], place["place_lon"]))}
        places.append(place)
    places.sort(key=lambda place: (place.get("distance_m") is None, place.get("distance_m") or 0))
    return places[:max(1, min(int(limit), 50))]


@use_case
def reverse_geocode(db, lat, lon, radius_m=None):
    """Name a position after the nearest named place, else its street address.

    Callers pass the averaged position of a stay and, for an area stay, its
    extent as `radius_m`. The provider chosen in Settings is asked for named
    places within the search radius: Geoapify's Places API and Photon's reverse
    endpoint take the radius directly; Mapbox's Search Box reverse lookup does
    not, so its POIs are filtered by distance here. Only when nothing named is
    nearby does the result fall back to the street address.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    radius = nearest_place_radius(radius_m)
    name = _require_provider(db)
    provider = providers.lookup_cache_key(name) + f":r{radius}"
    cached = db.caches.reverse_region(provider, lat, lon)
    if cached is not None:
        approximate = metres(lat, lon, cached["lat"], cached["lon"]) > 0.5
        return {**cached, "lat": lat, "lon": lon, "cache": "region", "approximate": approximate}
    with _provider_turn:
        result, kind = providers.lookup(name, lat, lon, radius)
    db.caches.save_reverse_region(provider, lat, lon, reverse_cache_radius(kind), result)
    return {**result, "cache": "provider", "approximate": False}


@use_case
def named_place_near(db, lat, lon, within_m, lookup=True):
    """The named place within `within_m` of a position, or None.

    With lookup=False only the reverse-lookup cache is read, so a sweep over
    the whole history spends no provider quota.
    """
    try:
        if lookup:
            value = reverse_geocode(db, lat, lon)
        else:
            radius = nearest_place_radius(None)
            value = db.caches.reverse_region(providers.lookup_cache_key(_require_provider(db)) + f":r{radius}", float(lat), float(lon))
    except (ValueError, OSError, KeyError):
        return None
    if not value or not value.get("place_name"):
        return None
    if value.get("place_lat") is not None and value.get("place_lon") is not None:
        distance = metres(lat, lon, value["place_lat"], value["place_lon"])
    elif value.get("distance_m") is not None:
        # Results cached before places kept their own position give only their
        # distance from where that lookup was made.
        distance = float(value["distance_m"]) + metres(lat, lon, value.get("lat", lat), value.get("lon", lon))
    else:
        return None
    return value if distance <= within_m else None


@use_case
def place_for_coordinate(db, lat, lon, radius_m=None):
    """Return the nearest named place (or address) plus its full address.

    A name you pinned to this spot wins over the provider.
    """
    label = label_at(db.places.labels(), lat, lon)
    if label:
        return label_place(label)
    try:
        value = reverse_geocode(db, lat, lon, radius_m)
        # `named` is false when only a street address was found nearby.
        return {"name": str(value.get("place_name") or value.get("name") or value.get("address") or "")[:300],
                "named": bool(value.get("place_name")),
                "address": str(value.get("address") or "")[:300],
                "category": str(value.get("category") or "")[:160],
                "categories": value.get("categories") if isinstance(value.get("categories"), list) else [],
                "provider_place_id": str(value.get("provider_place_id") or "")[:300],
                "place_lat": value.get("place_lat"), "place_lon": value.get("place_lon")}
    except (ValueError, OSError, KeyError):
        return {"name": "", "named": False, "address": "", "category": "", "categories": [], "provider_place_id": ""}


@use_case
def place_in_zone(db, lat, lon, zone, radius_m=None, lookup=None):
    """Name a position that may lie inside a saved place.

    Saved places are zones, not names: a university holds many buildings, and
    each is a place of its own. Inside a zone, a named place counts only when it
    is right there (within ZONE_PLACE_REACH_M, or the spot's own extent), so a
    house is not named after a shop down the street; otherwise the zone's name
    is used. Home is the one zone that names everything inside it. A name you
    pinned to the spot still wins.
    """
    if zone is not None and is_home_place(zone) and not label_at(db.places.labels(), lat, lon):
        return saved_place_as_found(zone)
    found = (lookup or place_for_coordinate)(db, lat, lon, radius_m)
    if zone is None or found.get("label_id"):
        return found
    reach = max(ZONE_PLACE_REACH_M, float(radius_m or 0))
    if found.get("named", True) and found.get("name") and (found.get("place_lat") is None
                           or metres(lat, lon, found["place_lat"], found["place_lon"]) <= reach):
        return found
    return {**saved_place_as_found(zone), "address": found.get("address") or zone.get("address") or ""}


@use_case
def address_for_coordinate(db, lat, lon):
    """Use the reverse cache first, then the configured provider when available."""
    return place_for_coordinate(db, lat, lon)["address"]
