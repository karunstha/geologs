"""Home Assistant as a tracker: poll a device_tracker entity (HA_URL, HA_TOKEN, HA_ENTITY).

A way in, like a tracker app posting: it reads a position and hands it to the services (or to a
GeoLogs server).
"""
import json
import urllib.request
from datetime import datetime, timezone

from .. import config


def read_point():
    """The entity's position now, as a point to ingest."""
    base, token, entity = config.home_assistant()
    if not base or not token or not entity:
        raise ValueError("HA_URL, HA_TOKEN and HA_ENTITY are required")
    req = urllib.request.Request(base + "/api/states/" + entity, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=12) as response:
        state = json.load(response)
    attrs = state.get("attributes") or {}
    if attrs.get("latitude") is None or attrs.get("longitude") is None:
        raise ValueError("Home Assistant entity has no latitude/longitude attributes")
    # Every poll is a received sample. Keep the Home Assistant update as the
    # canonical event ID, but date each sample at the time it was received.
    polled_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return {"source": "home_assistant:" + entity, "external_id": state.get("last_updated"),
            "timestamp": polled_at,
            "lat": attrs["latitude"], "lon": attrs["longitude"],
            "accuracy_m": attrs.get("gps_accuracy")}


def push(endpoint, token, point):
    """Send a point to a GeoLogs server's /api/points and return its reply."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/points", json.dumps(point).encode(),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)
