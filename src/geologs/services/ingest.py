"""Recording samples as they arrive: their speed and mode, reminders they fire, and stays they change."""
import math
from datetime import datetime, timezone

from ..domain.geo import metres
from ..domain.motion import mode_for
from ..domain.timing import parse_time
from .devices import note_ingest_token_use, record_ingest
from .reminders import arrival_events, check_name_reminders, deliver_webhook
from .stays import process_stays
from .use_case import use_case


@use_case
def ingest(db, payload, trigger_reminders=True, process_analysis=True):
    """Record one sample or a list of up to 1000; returns what was new, duplicated, recorded, and fired."""
    items = payload if isinstance(payload, list) else [payload]
    if len(items) > 1000:
        raise ValueError("maximum 1000 points per request")
    # Serialize each source's previous-fix and reminder decisions across
    # concurrent HTTP workers. Sorting prevents opposing batch lock order.
    for source in sorted({str(item.get("source") or "manual")[:80] for item in items}):
        db.points.lock_source(source)
    speed_settings = db.settings.get()
    inserted, duplicate, recorded, events = 0, 0, 0, []
    for raw in items:
        lat = float(raw.get("lat", raw.get("latitude")))
        lon = float(raw.get("lon", raw.get("longitude")))
        if not math.isfinite(lat) or not -90 <= lat <= 90 or not math.isfinite(lon) or not -180 <= lon <= 180:
            raise ValueError("invalid latitude or longitude")
        source = str(raw.get("source") or "manual")[:80]
        timestamp = parse_time(raw.get("timestamp"))
        accuracy = raw.get("accuracy_m", raw.get("gps_accuracy"))
        accuracy = float(accuracy) if accuracy is not None else None
        if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
            raise ValueError("accuracy_m must be nonnegative")
        reported_speed = raw.get("speed_mps")
        reported_speed = float(reported_speed) if reported_speed is not None else None
        if reported_speed is not None and (not math.isfinite(reported_speed) or reported_speed < 0):
            raise ValueError("speed_mps must be nonnegative")
        use_reported = speed_settings["speed_strategy"] == "reported" and reported_speed is not None
        speed = max(0.0, reported_speed + speed_settings["reported_speed_offset_kmh"] / 3.6) if use_reported else None
        previous = db.points.live_point(source)
        origin = "reported_adjusted" if use_reported and speed_settings["reported_speed_offset_kmh"] else "reported" if use_reported else None
        if speed is None:
            prior_fix = db.points.prior_fix(source, timestamp)
            if prior_fix:
                elapsed = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(prior_fix["timestamp"])).total_seconds()
                if 0 < elapsed <= 900 and (accuracy is None or accuracy <= 150) and (prior_fix["accuracy_m"] is None or prior_fix["accuracy_m"] <= 150):
                    distance = metres(lat, lon, prior_fix["lat"], prior_fix["lon"])
                    if distance <= 1:
                        speed, origin = 0.0, "estimated"
                    elif elapsed >= 5 and distance / elapsed <= 55.6:
                        speed, origin = distance / elapsed, "estimated"
        mode = mode_for(speed, accuracy, speed_settings)
        external_id = raw.get("external_id")
        external_id = str(external_id) if external_id is not None else None
        point_id = db.points.insert_point(source, external_id, timestamp, lat, lon, accuracy, speed, origin, mode)
        new = point_id is not None
        if not new:
            point_id = db.points.existing_point(source, external_id, timestamp, lat, lon)
            if point_id is None:
                raise RuntimeError("conflicting point could not be found")
        legacy_retry = False
        if not new:
            legacy = db.points.legacy_observation(point_id)
            legacy_retry = bool(legacy and legacy["timestamp"] == timestamp and legacy["lat"] == lat
                and legacy["lon"] == lon and legacy["accuracy_m"] == accuracy
                and (legacy["speed_mps"] == speed or
                     (speed is None and legacy["speed_origin"] in (None, "estimated"))))
        if not legacy_retry:
            recorded += db.points.insert_observation(
                point_id, source, timestamp, lat, lon, accuracy, speed, origin, parse_time(None),
                [source, external_id, timestamp, lat, lon, accuracy, reported_speed])
        if not new:
            duplicate += 1
            continue
        inserted += 1
        # Late imports are stored but do not rewind the live geofence state.
        if previous and timestamp <= previous["timestamp"]:
            continue
        db.points.set_live_point(source, point_id)
        if not trigger_reminders or abs((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()) > 1800:
            continue
        events += arrival_events(db, point_id, lat, lon, accuracy, timestamp, previous)
    if recorded and process_analysis:
        ingested_sources = {str(item.get("source") or "manual")[:80] for item in items}
        process_stays(db, ingested_sources)
        if trigger_reminders:
            events += check_name_reminders(db, ingested_sources)
    return {"inserted": inserted, "duplicates": duplicate, "recorded": recorded, "events": events}


@use_case
def record_locations(db, points, kind=None, device=None, token_id=None, note=None):
    """Record points a tracker sent (in batches of 1000), tell your webhook what they fired,
    and, for a tracker app (`kind`), note that its device got through and with which token.

    The points and the reminders they fired are kept before the webhook is
    called: a slow or failing webhook never loses them, and an event is never
    announced and then rolled back.
    """
    totals = {"inserted": 0, "duplicates": 0, "recorded": 0, "events": []}
    for start in range(0, len(points), 1000):
        result = ingest(db, points[start:start + 1000])
        for key in ("inserted", "duplicates", "recorded"):
            totals[key] += result[key]
        totals["events"] += result["events"]
    db.commit()
    deliver_webhook(db, totals["events"])
    if kind is not None:
        record_ingest(db, kind, 200, note, device, totals["inserted"])
    if token_id is not None:
        note_ingest_token_use(db, token_id, device)
    return totals

