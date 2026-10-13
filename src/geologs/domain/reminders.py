"""When a name-only reminder may fire."""
from datetime import datetime
from .geo import metres


# Name-only reminders check the places around you after this long still. A
# full 15 minute stay would miss a quick coffee run.
NAME_REMINDER_DWELL_SECONDS = 180
NAME_REMINDER_RADIUS_M = 150


def arrived(reminder, lat, lon, accuracy, timestamp, previous):
    """Whether a fix (with `previous`, the source's fix before it) is an arrival at a reminder's place.

    A fix too coarse for the place's radius says nothing, and a stale previous
    fix cannot prove you were already inside.
    """
    if accuracy is not None and accuracy > max(150, reminder["radius_m"]):
        return False
    inside = metres(lat, lon, reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
    recent_previous = previous and (datetime.fromisoformat(timestamp) - datetime.fromisoformat(previous["timestamp"])).total_seconds() <= 1800
    was_inside = recent_previous and metres(previous["lat"], previous["lon"], reminder["place_lat"], reminder["place_lon"]) <= reminder["radius_m"]
    return inside and not was_inside
