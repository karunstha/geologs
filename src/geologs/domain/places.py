"""Naming places: saved zones, name corrections, and matching place names."""
import difflib
import math
import re

from .geo import metres


# Inside a saved zone, a named place this close (or within the stay's extent) names the stay.
ZONE_PLACE_REACH_M = 60


def search_text(value):
    """Lowercase words; apostrophes vanish so "Sam's" matches "sams" and "McDonald's" "mcdonalds"."""
    text = re.sub(r"['’`]", "", str(value or "").casefold())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def street_key(address):
    """The street and town of a house address: "9004 Longacre Drive, Little Rock, ..." -> "longacre drive|little rock".

    None when the first part does not start with a house number.
    """
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    match = re.match(r"^\d+[\w/-]*\s+(.+)$", parts[0]) if len(parts) >= 2 else None
    return search_text(match.group(1)) + "|" + search_text(parts[1]) if match else None


def name_matches(pattern, name):
    """Whether a place name matches what a reminder asked for.

    Every word of the pattern must start a word of the name ("star" finds
    "Starbucks Coffee") or, for words of four letters or more, be a close
    spelling of one ("starbuks", "walgreens" for "Walgreen Co").
    """
    wanted, words = search_text(pattern).split(), search_text(name).split()
    if not wanted or not words:
        return False
    def close(term):
        return any(word.startswith(term)
                   or (len(term) >= 4 and difflib.SequenceMatcher(None, term, word).ratio() >= 0.8)
                   for word in words)
    return all(close(term) for term in wanted)


def label_at(labels, lat, lon, source=None, started_at=None, ended_at=None):
    """The name correction for a position, or for a stay when its source and times are given.

    A visit-only correction applies to a stay from its source that overlaps its
    window at that spot, and beats an every-visit one; among equals the nearest
    centre wins. Without a stay's times only every-visit corrections apply.
    """
    covering = []
    for label in labels:
        distance = metres(lat, lon, label["lat"], label["lon"])
        if distance > label["radius_m"]:
            continue
        if label.get("started_at"):
            if not started_at or (label.get("source") and label["source"] != source):
                continue
            if started_at > label["ended_at"] or ended_at < label["started_at"]:
                continue
        covering.append((not label.get("started_at"), distance, label))
    return min(covering, key=lambda item: item[:2])[2] if covering else None


def labels_by_id(labels):
    return {label["id"]: label for label in labels}


def label_place(label):
    return {"name": label["name"], "named": True, "address": label.get("address") or "", "category": label.get("category") or "",
            "categories": [label["category"]] if label.get("category") else [],
            "provider_place_id": label.get("provider_place_id") or "", "label_id": label["id"]}


def is_home_place(place):
    return str(place.get("name") or place.get("display_name") or "").strip().casefold() in ("home", "my home")


def zone_at(places, lat, lon):
    """The saved place whose circle covers a position, nearest centre first."""
    matches = [(metres(lat, lon, place["lat"], place["lon"]), place) for place in places]
    matches = [(distance, place) for distance, place in matches if distance <= place["radius_m"]]
    return min(matches, key=lambda item: item[0])[1] if matches else None


def saved_place_as_found(place):
    return {"name": place["name"], "named": True, "address": place.get("address") or "",
            "category": place.get("category") or "", "categories": [place["category"]] if place.get("category") else [],
            "provider_place_id": place.get("provider_place_id") or ""}


POSSIBLE_STOP_PLACE_M = 75
# Places known only by a street address merge with others on the same street
# this close: indoor GPS drifts 100 m or more, and every drift picks another
# house number.
ADDRESS_MERGE_M = 200


def place_bucket(lat, lon):
    """75 m ECEF grid: nearby places only need 27 neighboring cells checked."""
    a, b = math.radians(lat), math.radians(lon)
    r = 6371000 / 75
    return (math.floor(r * math.cos(a) * math.cos(b)), math.floor(r * math.cos(a) * math.sin(b)), math.floor(r * math.sin(a)))


def normalized_place_name(value):
    """Normalize provider labels for matching without changing their display text."""
    normalized = " ".join(str(value or "").casefold().split())
    return "" if normalized in ("unknown", "unknown place") else normalized
