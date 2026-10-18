"""Reminders: setting them, firing them on arrival, and telling your webhook."""
import os
from datetime import datetime, timezone

from . import places as naming
from ..domain.geo import metres
from ..domain.places import name_matches, search_text
from ..domain.reminders import arrived, NAME_REMINDER_DWELL_SECONDS, NAME_REMINDER_RADIUS_M
from ..domain.stay_detection import candidate_duration
from ..domain.timing import parse_time
from ..integrations import webhooks
from .places import geocode
from ..data import OWNER_ID
from .use_case import use_case


@use_case
def create_reminder(db, raw):
    message = str(raw.get("text") or "").strip()
    if not message or len(message) > 500:
        raise ValueError("text is required (max 500 characters)")
    match_name = str(raw.get("match_name") or "").strip()
    if match_name:
        # Name-only: fires at the first stop near any place with a matching name.
        if len(match_name) > 120 or not search_text(match_name):
            raise ValueError("match_name must contain letters or digits (max 120 characters)")
        place_id = None
        destination = {"name": match_name, "address": "", "category": "", "provider_place_id": ""}
    elif raw.get("place_id") not in (None, ""):
        place_id = int(raw["place_id"])
        place = db.places.get(place_id)
        if not place:
            raise ValueError("place not found")
        destination = {"name": place["name"], "address": place["address"] or "",
                       "category": place["category"] or "", "provider_place_id": place["provider_place_id"] or ""}
    else:
        destination_name = str(raw.get("place_name") or "").strip()
        lat, lon = raw.get("lat"), raw.get("lon")
        if (lat is None or lon is None) and destination_name:
            matches = geocode(db, destination_name, resolve=1)
            if not matches:
                raise ValueError("place name was not found; provide coordinates")
            match = matches[0]
            lat, lon = match["lat"], match["lon"]
            raw = {**match, **raw, "lat": lat, "lon": lon}
        if lat is None or lon is None:
            raise ValueError("place_id or destination coordinates are required")
        lat, lon = float(lat), float(lon)
        radius = float(raw.get("radius_m", 150))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 20 <= radius <= 5000):
            raise ValueError("invalid destination coordinates or radius (20–5000 m)")
        found = naming.place_for_coordinate(db, lat, lon)
        destination = {
            "name": destination_name or str(raw.get("name") or found.get("name") or f"{lat:.5f}, {lon:.5f}").strip(),
            "address": str(raw.get("address") or found.get("address") or "")[:300],
            "category": str(raw.get("category") or found.get("category") or "")[:160],
            "provider_place_id": str(raw.get("provider_place_id") or found.get("provider_place_id") or "")[:300],
        }
        if not destination["name"] or len(destination["name"]) > 120:
            raise ValueError("place_name is required (max 120 characters)")
        # The destination is a place of its own, kept off your saved places.
        place_id = db.places.add("Reminder destination " + os.urandom(8).hex(), lat, lon, radius, destination["address"],
                                 destination["category"], destination["provider_place_id"], saved=False)
    reminder_id = db.reminders.add(message, place_id, parse_time(None), destination, match_name or None)
    return db.reminders.get_active(reminder_id)


@use_case
def cancel_reminder(db, reminder_id):
    cancelled = db.reminders.cancel(reminder_id, parse_time(None))
    if not cancelled:
        raise ValueError("active reminder not found")
    return {"updated": 1}


@use_case
def delete_reminder(db, reminder_id=None):
    """Remove a fired or cancelled reminder from history, or with no id all of them."""
    ended = db.reminders.delete_ended(reminder_id)
    if reminder_id is not None and not ended:
        raise ValueError("reminder not found in history")
    for place_id in {reminder["place_id"] for reminder in ended if reminder["place_id"] is not None}:
        db.places.delete_if_unused(place_id)
    return {"deleted": len(ended)}


@use_case
def arrival_events(db, point_id, lat, lon, accuracy, timestamp, previous):
    """Fire the reminders a fix arrives at; returns their events. Does not commit."""
    events = []
    for reminder in db.reminders.geofenced():
        if not arrived(reminder, lat, lon, accuracy, timestamp, previous):
            continue
        event_id = db.reminders.record_event(reminder["id"], point_id, timestamp)
        db.reminders.trigger(reminder["id"], timestamp)
        events.append({"id": event_id, "reminder_id": reminder["id"], "text": reminder["text"],
                       "place": reminder["place_name"], "triggered_at": timestamp})
    return events


@use_case
def check_name_reminders(db, sources, now=None):
    """Fire name-only reminders once you have been still for a few minutes near a matching place.

    The places around the current stationary candidate are looked up once and
    kept beside it (not in it: the candidate is stay detection's own), so later polls (and reminders created meanwhile)
    are checked without asking the provider again. Every named place within
    NAME_REMINDER_RADIUS_M counts, not just the nearest, so a busy strip mall
    cannot hide the match. Saved places count by their own radius.
    """
    reminders = db.reminders.name_only()
    if not reminders:
        return []
    now = now or datetime.now(timezone.utc)
    events = []
    for source in sources:
        state = db.stays.state(source)
        candidate = state["candidate"] if state else None
        if not candidate or candidate["count"] < 2 or candidate_duration(candidate) < NAME_REMINDER_DWELL_SECONDS:
            continue
        if (now - datetime.fromisoformat(candidate["end"])).total_seconds() > 1800:
            continue  # historical data never fires reminders
        nearby = db.stays.nearby_names(source, candidate["start_id"])
        if nearby is None:
            names = [place["name"] for place in db.places.saved() + [label for label in db.places.labels() if not label["started_at"]]
                     if metres(candidate["lat"], candidate["lon"], place["lat"], place["lon"]) <= place["radius_m"]]
            try:
                names += [place["name"] for place in
                          naming.nearby_places(db, candidate["lat"], candidate["lon"], NAME_REMINDER_RADIUS_M)
                          if (place.get("distance_m") or 0) <= NAME_REMINDER_RADIUS_M]
            except (ValueError, OSError, KeyError):
                pass  # no provider: saved places still match
            nearby = list(dict.fromkeys(name for name in names if name))
            db.stays.save_nearby_names(source, candidate["start_id"], nearby)
        point_id = db.points.point_of(candidate["end_id"])
        for reminder in reminders:
            matched = next((name for name in nearby if name_matches(reminder["match_name"], name)), None)
            if matched is None or point_id is None:
                continue
            # A reminder fires once, even when two sources stop at the same place.
            if not db.reminders.trigger(reminder["id"], candidate["end"], only_active=True):
                continue
            event_id = db.reminders.record_event(reminder["id"], point_id, candidate["end"])
            events.append({"id": event_id, "reminder_id": reminder["id"], "text": reminder["text"],
                           "place": matched, "triggered_at": candidate["end"]})
    return events


@use_case
def deliver_webhook(db, events):
    """Send the owner's fired reminders to GEOLOGS_WEBHOOK_URL, noting how each went. The webhook is
    the server owner's (their assistant, say), so other users' reminders never go to it."""
    target = webhooks.url()
    if not target or not events or db.user_id != OWNER_ID:
        return
    for event in events:
        db.reminders.set_webhook_status(event["id"], webhooks.send(target, event))
