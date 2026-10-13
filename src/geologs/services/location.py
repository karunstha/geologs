"""Where you are now: your latest position, named."""

from ..domain.places import is_home_place, label_at, label_place, labels_by_id, zone_at
from .places import place_in_zone
from .position import latest
from .stays import stays
from .use_case import use_case


@use_case
def current_location(db):
    """Most recent fix enriched with a saved or reverse-geocoded place name."""
    point = latest(db)
    if not point:
        return None
    ongoing = next((stay for stay in stays(db, start=point["timestamp"], source=point["source"], limit=5)
                    if stay["ongoing"] and stay["started_at"] <= point["timestamp"]), None)
    labels = db.places.labels()
    label = (labels_by_id(labels).get(ongoing["label_id"]) if ongoing and ongoing.get("label_id") else None) \
        or label_at(labels, point["lat"], point["lon"])
    zone = zone_at(db.places.saved(), point["lat"], point["lon"])
    point.update({"zone_id": zone["id"] if zone else None, "zone": zone["name"] if zone else None})
    if label:
        point.update({"place_id": None, **label_place(label), "place_name": label["name"]})
        return point
    found = place_in_zone(db, point["lat"], point["lon"], zone)
    point.update({"place_id": zone["id"] if zone and is_home_place(zone) else None, "place_name": found["name"],
                  "address": found["address"], "category": found["category"],
                  "provider_place_id": found["provider_place_id"]})
    return point
