"""Places you saved (zones) and names you gave spots or visits."""
import os

from . import places as naming
from ..domain.geo import metres
from ..domain.timing import parse_time
from ..domain.visits import merge_split_visits
from .stays import stays
from .use_case import use_case


@use_case
def enrich_saved_places(db):
    """Look up the address, category and provider id of saved places missing them (saved before
    lookups kept them). The background worker runs this, so reading your places never looks anything up."""
    rows = db.places.saved()
    for place in rows:
        if place["address"] and place["category"] and place["provider_place_id"]:
            continue
        found = naming.place_for_coordinate(db, place["lat"], place["lon"])
        address = place["address"] or found.get("address") or ""
        category = place["category"] or found.get("category") or ""
        provider_place_id = place["provider_place_id"] or found.get("provider_place_id") or ""
        if (address, category, provider_place_id) != (place["address"] or "", place["category"] or "", place["provider_place_id"] or ""):
            place.update({"address": address, "category": category, "provider_place_id": provider_place_id})
            db.places.set_details(place["id"], address, category, provider_place_id)
    return rows


@use_case
def save_place(db, raw):
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")
    lat, lon = float(raw["lat"]), float(raw["lon"])
    radius = float(raw.get("radius_m", 150))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 5000):
        raise ValueError("invalid place coordinates or radius (20–5000 m)")
    same_name = db.places.named(name)
    if same_name is not None and same_name != (int(raw["id"]) if raw.get("id") else None):
        raise ValueError(f"you already have a place named {name}")
    existing = db.places.get(raw["id"]) if raw.get("id") else None
    address = str(raw.get("address") or (existing["address"] if existing else "") or "")[:300]
    category = str(raw.get("category") or (existing["category"] if existing else "") or "")[:160]
    provider_place_id = str(raw.get("provider_place_id") or (existing["provider_place_id"] if existing else "") or "")[:300]
    if not address or not category or not provider_place_id:
        found = naming.place_for_coordinate(db, lat, lon)
        address = address or found.get("address") or ""
        category = category or found.get("category") or ""
        provider_place_id = provider_place_id or found.get("provider_place_id") or ""
    if raw.get("id"):
        if not db.places.update(raw["id"], name, lat, lon, radius, address, category, provider_place_id):
            raise ValueError("place not found")
        place_id = int(raw["id"])
    else:
        place_id = db.places.add(name, lat, lon, radius, address, category, provider_place_id)
    db.caches.clear_analysis()
    return db.places.saved_place(place_id)


@use_case
def delete_place(db, place_id):
    """Remove a saved place from the list and from naming visits.

    A place that a reminder points at is hidden instead, so the reminder
    keeps its destination and its history stays readable.
    """
    if not db.places.is_saved(place_id):
        raise ValueError("saved place not found")
    if db.reminders.points_to(place_id):
        # Names are unique; the hidden row must not block reusing this one.
        db.places.hide(place_id, "Deleted place " + os.urandom(8).hex())
    else:
        db.places.delete(place_id)
    db.caches.clear_analysis()
    return {"deleted": int(place_id)}


@use_case
def save_label(db, raw):
    """Pin a name to a position, overriding saved places and provider lookups there.

    With started_at and ended_at it applies only to stays overlapping that
    window (from `source`, when given): one visit, untouched by later ones.
    Without them it names every visit to the spot, past and future.
    Correcting the same visit, or the same spot, again replaces the label.
    """
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")
    lat, lon = float(raw["lat"]), float(raw["lon"])
    radius = float(raw.get("radius_m") or 50)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 1000):
        raise ValueError("invalid label coordinates or radius (20–1000 m)")
    if bool(raw.get("started_at")) != bool(raw.get("ended_at")):
        raise ValueError("started_at and ended_at must be given together")
    started_at = parse_time(raw["started_at"]) if raw.get("started_at") else None
    ended_at = parse_time(raw["ended_at"]) if raw.get("ended_at") else None
    if started_at and ended_at < started_at:
        raise ValueError("ended_at must not be before started_at")
    source = str(raw.get("source") or "").strip()[:80] or None if started_at else None
    values = (name, lat, lon, radius, str(raw.get("address") or "")[:300], str(raw.get("category") or "")[:160],
              str(raw.get("provider_place_id") or "")[:300], source, started_at, ended_at, parse_time(None))

    def same_correction(label):
        if metres(lat, lon, label["lat"], label["lon"]) > 25 or bool(label["started_at"]) != bool(started_at):
            return False
        return not started_at or (label["source"] == source and label["started_at"] <= ended_at and label["ended_at"] >= started_at)
    target = int(raw["id"]) if raw.get("id") else next(
        (label["id"] for label in db.places.labels() if same_correction(label)), None)
    if target is not None:
        if not db.places.update_label(target, *values):
            raise ValueError("label not found")
    else:
        target = db.places.add_label(*values)
    db.caches.clear_analysis()
    return db.places.label(target)


@use_case
def delete_label(db, label_id):
    if not db.places.delete_label(label_id):
        raise ValueError("label not found")
    db.caches.clear_analysis()
    return {"deleted": int(label_id)}


@use_case
def place_visits(db, place_id, start=None, end=None):
    place = db.places.get(place_id)
    if not place:
        raise ValueError("place not found")
    # Merge over every stay so each went_elsewhere flag meets the stay it refers to.
    # A saved place is a zone: a visit is time anywhere inside it, across its buildings.
    merged = merge_split_visits(list(reversed(stays(db, start, end, limit=None))), lambda s: s["zone_id"])
    visits = [{"arrival": s["started_at"], "departure": s["ended_at"],
               "observed_minutes": s["observed_minutes"], "points": s["point_count"],
               "ongoing": bool(s["ongoing"]), "source": s["source"],
               "kind": s["kind"], "activity": s["activity"]}
              for s in reversed(merged) if s["zone_id"] == int(place_id)]
    return {"place": {key: place[key] for key in ("id", "name", "lat", "lon", "radius_m", "address")}, "visits": visits,
            "total_observed_minutes": round(sum(v["observed_minutes"] for v in visits), 1)}


@use_case
def save_place_label(db, raw):
    """Save a name correction, looking up its address when none is given.

    A label's address is the only one its visits get, so a rename without one
    (an MCP call that passes just a name) would leave visit search with only
    coordinates. A named place's address is used only when that place lies
    inside the label: a friend's house must not take the café down the street's.
    """
    if not str(raw.get("address") or "").strip():
        try:
            lat, lon = float(raw["lat"]), float(raw["lon"])
            value = naming.reverse_geocode(db, lat, lon)
            place_lat, place_lon = value.get("place_lat"), value.get("place_lon")
            inside = not value.get("place_name") or (
                place_lat is not None and place_lon is not None
                and metres(lat, lon, place_lat, place_lon) <= float(raw.get("radius_m") or 50))
            if inside and value.get("address"):
                raw = {**raw, "address": str(value["address"])[:300]}
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return save_label(db, raw)
