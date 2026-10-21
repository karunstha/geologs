"""Google's location exports, read as streams: they can be hundreds of megabytes.

Classic Takeout Records.json (a top-level `locations` array) and the newer Timeline exports,
Takeout's monthly Semantic Location History (`timelineObjects`) and the export a phone makes since
Timeline moved onto it in 2024 (Android: `semanticSegments` and `rawSignals`; iOS: a top-level array
of segments). Each yields fixes as {timestamp, lat, lon, accuracy_m?, speed_mps?}.
"""
import json
import re
from datetime import datetime, timedelta

# A visit is a place and a span of time; it becomes a fix every this often, so a stay can be seen.
VISIT_STEP = timedelta(minutes=30)
_CHUNK = 65536


def iter_json_array(stream, marker, near_start=False, missing=None):
    """Yield the items of the JSON array that `marker` (a regex ending at its "[") opens, without
    loading the file. `near_start`: look only in the first megabyte. When the array is not found,
    yield nothing, or raise ValueError(`missing`) if given."""
    decoder = json.JSONDecoder()
    pattern = re.compile(marker)
    buffer = ""
    read = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk or (near_start and read > 1_000_000):
            if missing:
                raise ValueError(missing)
            return
        read += len(chunk)
        buffer += chunk
        match = pattern.search(buffer)
        if match:
            buffer = buffer[match.end():]
            break
        # Keep a tail, so a marker split across two chunks is still found.
        buffer = buffer[-200:]
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return
        try:
            item, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = stream.read(_CHUNK)
            if not chunk:
                raise ValueError("the file ends in the middle of its locations")
            buffer += chunk
            continue
        yield item
        buffer = buffer[end:]


def iter_takeout_locations(stream):
    """Yield objects from a top-level `locations` array without loading the export."""
    for item in iter_json_array(stream, r'"locations"\s*:\s*\[', near_start=True,
                                missing="Takeout file has no locations array near its start"):
        if not isinstance(item, dict):
            raise ValueError("Takeout location record must be an object")
        yield item


def timeline_kind(head):
    """Which Timeline export a file's first part looks like, or None."""
    if re.search(r'"semanticSegments"\s*:|"rawSignals"\s*:', head):
        return "phone"
    if re.search(r'"timelineObjects"\s*:', head):
        return "semantic"
    if re.match(r'\s*\[\s*\{', head) and re.search(r'"(startTime|endTime)"\s*:', head):
        return "ios"
    return None


def _position(value):
    """(lat, lon) from "45.5252°, -122.6322°", "geo:45.5252,-122.6322", or E7 integers; else None."""
    if isinstance(value, dict):
        if "latLng" in value:
            return _position(value["latLng"])
        if "LatLng" in value:
            return _position(value["LatLng"])
        if "latitudeE7" in value and "longitudeE7" in value:
            return value["latitudeE7"] / 1e7, value["longitudeE7"] / 1e7
        if "latE7" in value and "lngE7" in value:
            return value["latE7"] / 1e7, value["lngE7"] / 1e7
        return None
    if not isinstance(value, str):
        return None
    numbers = re.findall(r"-?\d+(?:\.\d+)?", value.removeprefix("geo:"))
    return (float(numbers[0]), float(numbers[1])) if len(numbers) >= 2 else None


def _time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _fix(at, position, accuracy=None, speed=None):
    fix = {"timestamp": at.isoformat() if isinstance(at, datetime) else str(at), "lat": position[0], "lon": position[1]}
    if accuracy is not None:
        fix["accuracy_m"] = accuracy
    if speed is not None:
        fix["speed_mps"] = speed
    return fix


def _visit(start, end, position):
    """A place held from start to end: a fix at each end and every VISIT_STEP between."""
    at = _time(start)
    finish = _time(end)
    while at < finish:
        yield _fix(at, position)
        at += VISIT_STEP
    yield _fix(finish, position)


def _segment(segment):
    """The fixes of one phone-export segment (Android or iOS): a path, a visit or an activity."""
    start, end = segment.get("startTime"), segment.get("endTime")
    for step in segment.get("timelinePath") or []:
        position = _position(step.get("point"))
        if not position:
            continue
        if step.get("time"):
            yield _fix(step["time"], position)
        elif start and step.get("durationMinutesOffsetFromStartTime") is not None:
            yield _fix(_time(start) + timedelta(minutes=float(step["durationMinutesOffsetFromStartTime"])), position)
    visit = segment.get("visit")
    if visit and start and end:
        position = _position((visit.get("topCandidate") or {}).get("placeLocation"))
        if position:
            yield from _visit(start, end, position)
    activity = segment.get("activity")
    if activity and start and end:
        for at, place in ((start, activity.get("start")), (end, activity.get("end"))):
            position = _position(place)
            if position:
                yield _fix(at, position)


def _semantic(item):
    """The fixes of one Takeout Semantic Location History object: a place visit or a trip."""
    if "placeVisit" in item:
        visit = item["placeVisit"]
        position, span = _position(visit.get("location") or {}), visit.get("duration") or {}
        if position and span.get("startTimestamp") and span.get("endTimestamp"):
            yield from _visit(span["startTimestamp"], span["endTimestamp"], position)
    if "activitySegment" in item:
        trip = item["activitySegment"]
        span = trip.get("duration") or {}
        for key, at in (("startLocation", span.get("startTimestamp")), ("endLocation", span.get("endTimestamp"))):
            position = _position(trip.get(key) or {})
            if position and at:
                yield _fix(at, position)
        for point in (trip.get("simplifiedRawPath") or {}).get("points") or []:
            position = _position(point)
            if position and point.get("timestamp"):
                yield _fix(point["timestamp"], position)


def iter_timeline_fixes(open_text, kind):
    """Yield the fixes of a Timeline export of `kind` (timeline_kind). `open_text()` opens the file
    afresh: the phone export keeps its raw fixes and its segments in two arrays, read one at a time."""
    if kind == "ios":
        with open_text() as stream:
            for segment in iter_json_array(stream, r"\A\s*\[", near_start=True, missing="not a Google Timeline export"):
                if isinstance(segment, dict):
                    yield from _segment(segment)
    elif kind == "semantic":
        with open_text() as stream:
            for item in iter_json_array(stream, r'"timelineObjects"\s*:\s*\[', missing="no timelineObjects in this file"):
                if isinstance(item, dict):
                    yield from _semantic(item)
    elif kind == "phone":
        with open_text() as stream:
            for signal in iter_json_array(stream, r'"rawSignals"\s*:\s*\['):
                position = isinstance(signal, dict) and signal.get("position")
                where = position and _position(position)
                if where and position.get("timestamp"):
                    yield _fix(position["timestamp"], where, position.get("accuracyMeters"), position.get("speedMetersPerSecond"))
        with open_text() as stream:
            for segment in iter_json_array(stream, r'"semanticSegments"\s*:\s*\['):
                if isinstance(segment, dict):
                    yield from _segment(segment)
    else:
        raise ValueError("not a Google Timeline export")
