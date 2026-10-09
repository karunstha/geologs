"""Receive locations from tracking apps in their own formats.

Each adapter turns one request into GeoLogs points and the reply that app
expects. A bad point is skipped rather than failing the request: apps that
queue offline (OwnTracks, Overland) resend a rejected request forever, so one
malformed fix would otherwise block everything queued behind it.
"""
import math
import re

from .core import parse_time

MAX_SOURCE_LENGTH = 80


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _time(value):
    """Epoch seconds or milliseconds, or ISO 8601 with a timezone; None when absent."""
    if value is None or value == "":
        return None
    number = _number(value)
    if number is not None:
        return parse_time(number / 1000 if number > 1e12 else number)
    return parse_time(str(value).strip().replace(" ", "T", 1))


def _source(kind, device, override=None):
    name = str(override or "").strip() or f"{kind}:{str(device or '').strip() or 'device'}"
    return re.sub(r"\s+", " ", name)[:MAX_SOURCE_LENGTH]


def _point(source, lat, lon, timestamp=None, accuracy=None, speed_mps=None, external_id=None):
    """A validated point, or None when it cannot be stored."""
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    try:
        timestamp = _time(timestamp)
    except (ValueError, OverflowError, OSError):
        return None
    accuracy, speed = _number(accuracy), _number(speed_mps)
    point = {"source": source, "lat": lat, "lon": lon, "timestamp": timestamp,
             # Apps report -1 (or nothing) when they do not know.
             "accuracy_m": accuracy if accuracy is not None and accuracy >= 0 else None,
             "speed_mps": speed if speed is not None and speed >= 0 else None}
    if external_id not in (None, ""):
        point["external_id"] = str(external_id)[:200]
    return point


def owntracks(payload, query, headers):
    """OwnTracks HTTP mode: one message per POST; only `location` messages carry a fix.

    The app names itself in X-Limit-U/X-Limit-D (or ?u=&d=, or the payload's
    topic). `tst` is the time of the fix, so queued fixes keep their own time.
    """
    reply = []  # OwnTracks expects a JSON array (of messages for the phone).
    if not isinstance(payload, dict) or payload.get("_type") != "location":
        if isinstance(payload, dict) and payload.get("_type") == "encrypted":
            raise ValueError("encrypted OwnTracks payloads are not supported; turn off the encryption key")
        return [], 0, reply
    user = headers.get("X-Limit-U") or query.get("u")
    device = headers.get("X-Limit-D") or query.get("d")
    if not (user or device) and isinstance(payload.get("topic"), str):
        parts = payload["topic"].split("/")
        user, device = (parts[1], parts[2]) if len(parts) >= 3 else (None, None)
    name = "/".join(part for part in (user, device) if part) or payload.get("tid")
    speed = _number(payload.get("vel"))  # km/h, omitted when zero or unknown
    point = _point(_source("owntracks", name, query.get("source")), payload.get("lat"), payload.get("lon"),
                   payload.get("tst"), payload.get("acc"), speed / 3.6 if speed is not None else None,
                   payload.get("tst"))
    return ([point] if point else []), (0 if point else 1), reply


def overland(payload, query, headers):
    """Overland (iOS): a batch of GeoJSON points; it clears its queue only on {"result": "ok"}."""
    reply = {"result": "ok"}
    features = payload.get("locations") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError('expected {"locations": [...]}')
    points, skipped = [], 0
    for feature in features:
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        properties = (feature.get("properties") if isinstance(feature, dict) else None) or {}
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "Point" else None
        point = None
        if isinstance(coordinates, list) and len(coordinates) >= 2 and isinstance(properties, dict):
            point = _point(_source("overland", properties.get("device_id") or query.get("device"), query.get("source")),
                           coordinates[1], coordinates[0], properties.get("timestamp"),
                           properties.get("horizontal_accuracy"), properties.get("speed"), properties.get("timestamp"))
        if point:
            points.append(point)
        else:
            skipped += 1
    return points, skipped, reply


def osmand(payload, query, headers):
    """The OsmAnd protocol used by Traccar Client and OsmAnd's online tracking.

    Older clients send query or form fields (speed in knots); Traccar Client 9+
    posts JSON with a `location` object (speed in m/s).
    """
    location = payload.get("location") if isinstance(payload, dict) else None
    if isinstance(location, dict):
        coords = location.get("coords") or {}
        point = _point(_source("osmand", payload.get("device_id") or query.get("id"), query.get("source")),
                       coords.get("latitude"), coords.get("longitude"), location.get("timestamp"),
                       coords.get("accuracy"), coords.get("speed"), location.get("timestamp"))
        return ([point] if point else []), (0 if point else 1), {"ok": True}
    fields = {**(payload if isinstance(payload, dict) else {}), **query}
    lat, lon = fields.get("lat"), fields.get("lon")
    if (lat is None or lon is None) and fields.get("location"):
        lat, _, lon = str(fields["location"]).partition(",")
    knots = _number(fields.get("speed"))
    point = _point(_source("osmand", fields.get("id") or fields.get("deviceid"), fields.get("source")),
                   lat, lon, fields.get("timestamp"), fields.get("accuracy"),
                   knots * 0.514444 if knots is not None else None, fields.get("timestamp"))
    return ([point] if point else []), (0 if point else 1), {"ok": True}


def _first(fields, *names):
    return next((fields[name] for name in names if fields.get(name) not in (None, "")), None)


def generic(payload, query, headers):
    """Flat fields from a query string, a form, a JSON object, or a JSON array of objects.

    For GPSLogger, Tasker, iOS Shortcuts, Home Assistant automations, or a
    script. Units are named: speed_mps (or speed) in m/s, speed_kmh in km/h.
    """
    items = payload if isinstance(payload, list) else [payload if isinstance(payload, dict) else {}]
    points, skipped = [], 0
    for item in items:
        fields = {**query, **item} if isinstance(item, dict) else dict(query)
        kmh = _number(_first(fields, "speed_kmh", "vel"))
        speed = _first(fields, "speed_mps", "speed")
        timestamp = _first(fields, "timestamp", "time", "tst")
        point = _point(_source("generic", _first(fields, "device", "id", "tid"), fields.get("source")),
                       _first(fields, "lat", "latitude"), _first(fields, "lon", "lng", "longitude"), timestamp,
                       _first(fields, "accuracy_m", "accuracy", "acc"), kmh / 3.6 if kmh is not None else speed,
                       _first(fields, "external_id") or timestamp)
        if point:
            points.append(point)
        else:
            skipped += 1
    return points, skipped, None


def claimed_source(kind, query, headers):
    """The source a request names before its body is read, so a rejected phone can still be told apart."""
    if query.get("source"):
        return _source(kind, None, query["source"])
    if kind == "owntracks":
        name = "/".join(part for part in (headers.get("X-Limit-U") or query.get("u"), headers.get("X-Limit-D") or query.get("d")) if part)
    elif kind == "osmand":
        name = query.get("id") or query.get("deviceid")
    elif kind == "generic":
        name = _first(query, "device", "id", "tid")
    else:
        name = query.get("device")
    return _source(kind, name) if name else None


FORMATS = {"owntracks": owntracks, "overland": overland, "osmand": osmand, "generic": generic}
