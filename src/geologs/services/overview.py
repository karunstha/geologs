"""Daily records: the structured summary of each day, its brief, and the catalog that keeps them."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.briefs import daily_brief, home_return_context, HOME_RETURN_GRACE_HOURS, home_return_text
from ..domain.geo import metres
from ..domain.places import is_home_place
from ..domain.settings import ANALYSIS_DEFAULTS
from .day import day_summary
from .fuel import fuel_signature
from .stays import stays
from .use_case import use_case


def _home_return(db, day_start, day_end, source=None):
    """An observed Home return just after midnight, for either adjoining day."""
    homes = {place["id"]: place["name"] for place in db.places.saved() if is_home_place(place)}
    if not homes:
        return None
    context_start, context_end = home_return_context(day_start, day_end)
    rows = sorted(stays(db, context_start.isoformat(), context_end.isoformat(), source=source, limit=None),
                  key=lambda stay: (stay["started_at"], stay["id"]))
    previous_by_source, returns = {}, []
    for stay in rows:
        previous = previous_by_source.get(stay["source"])
        home_name = homes.get(stay.get("place_id"))
        is_home = home_name is not None
        if is_home and previous is not None and previous.get("place_id") not in homes:
            returned_at = datetime.fromisoformat(stay["started_at"])
            previous_started = datetime.fromisoformat(previous["started_at"])
            next_morning = day_end <= returned_at < day_end + timedelta(hours=HOME_RETURN_GRACE_HOURS)
            after_midnight = day_start <= returned_at < day_start + timedelta(hours=HOME_RETURN_GRACE_HOURS)
            if next_morning and previous_started < day_end:
                returns.append({"returned_at": returned_at.isoformat(), "home_name": home_name, "timing": "next_day"})
            elif after_midnight and previous_started < day_start:
                returns.append({"returned_at": returned_at.isoformat(), "home_name": home_name, "timing": "after_midnight"})
        previous_by_source[stay["source"]] = stay
    return min(returns, key=lambda item: item["returned_at"], default=None)


@use_case
def daily_overview(db, date, timezone_name=None, source=None):
    """Materialize a compact, structured record intended for API and LLM use.

    The catalog holds one record per date: the configured timezone across all
    sources. Other timezones or a single source are computed on request but not
    stored, so an agent asking in another zone cannot add a second record.
    """
    configured = db.settings.get()["timezone"]
    timezone_name = timezone_name or configured
    canonical = timezone_name == configured and not source
    try:
        zone = ZoneInfo(timezone_name)
        local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=zone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    day_start = local_start.astimezone(timezone.utc)
    day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    start = day_start.isoformat()
    end = (day_end - timedelta(microseconds=1)).isoformat()
    home_context_start, home_context_end = home_return_context(day_start, day_end)
    watermark = overview_watermark(db, start, end, source, home_context_start.isoformat(), home_context_end.isoformat())
    cached = db.summaries.get(date, timezone_name, source, watermark) if canonical else None
    if cached is not None:
        return cached
    day = day_summary(db, date, timezone_name, source)
    reminder_events = db.reminders.events_between(start, end, source)
    reminders_created = db.reminders.created_between(start, end)
    reminders_cancelled = db.reminders.cancelled_between(start, end)
    reminders_active_at_end = db.reminders.active_at(end)
    places = [{"name": place.get("name") or place.get("display_name"),
               "address": place.get("address") or "", "home": is_home_place(place),
               "zone": place.get("zone"), "category": place.get("category") or "",
               "provider_place_id": place.get("provider_place_id") or "",
               "lat": place["lat"], "lon": place["lon"],
               "visits": place["visits"], "observed_minutes": place["observed_minutes"],
               "activities": place.get("activities") or {},
               "first_seen": place.get("first_seen"), "last_seen": place.get("last_seen")}
              for place in day["places_including_home"]]
    def endpoint_name(coordinate):
        if not coordinate:
            return None
        matches = [(metres(coordinate[1], coordinate[0], place["lat"], place["lon"]), place["name"])
                   for place in places]
        matches = [match for match in matches if match[0] <= 100]
        return min(matches, default=(0, None), key=lambda match: match[0])[1]
    trips = []
    for trip in day["trips"]:
        duration = max(0, (datetime.fromisoformat(trip["end"]) - datetime.fromisoformat(trip["start"])).total_seconds())
        trips.append({"start": trip["start"], "end": trip["end"], "duration_minutes": round(duration / 60, 1),
                      "mode": trip["mode"], "distance_km": trip["distance_km"],
                      "from": trip.get("from_name") or endpoint_name([trip["from_lon"], trip["from_lat"]]),
                      "to": trip.get("to_name") or endpoint_name([trip["to_lon"], trip["to_lat"]]),
                      "round_trip": bool(trip.get("loop")), "via": (trip.get("turnaround") or {}).get("name") if trip.get("loop") else None,
                      "mode_corrected": bool(trip.get("mode_corrected")),
                      "coverage_complete": trip["coverage_complete"],
                      # Distance along routes taken from past trips rather than recorded.
                      "filled_km": trip.get("filled_km", 0),
                      "fuel_cost": (trip.get("fuel") or {}).get("cost"), "fuel_litres": (trip.get("fuel") or {}).get("litres")})
    visits = [{"place": stay.get("display_name") or stay.get("place_name"), "zone": stay.get("zone"),
               "arrival": stay["started_at"], "departure": stay["ended_at"],
               "observed_minutes": stay["observed_minutes"], "kind": stay["kind"],
               "activity": stay.get("activity"),
               "distance_km": round((stay.get("distance_m") or 0) / 1000, 2) if stay["kind"] == "area" else None,
               "moved_within": [{"mode": move["mode"], "distance_km": move["distance_km"]} for move in stay["internal_moves"]],
               "ongoing": bool(stay["ongoing"])}
              for stay in day["stays"]]
    # Saved places are zones: time inside each one, across the places in it.
    zones = {}
    for visit in visits:
        if visit["zone"]:
            item = zones.setdefault(visit["zone"], {"name": visit["zone"], "observed_minutes": 0.0, "places": []})
            item["observed_minutes"] += visit["observed_minutes"]
            if visit["place"] not in item["places"]:
                item["places"].append(visit["place"])
    zones = [{**item, "observed_minutes": round(item["observed_minutes"], 1)} for item in zones.values()
             if any(place != item["name"] for place in item["places"])]
    home_return = _home_return(db, day_start, day_end, source)
    if home_return:
        home_return["summary"] = home_return_text(home_return, zone)
    trip_distance_km = round(sum(trip["distance_km"] for trip in trips), 2)
    totals = {"raw_fixes": day["points"], "timeline_fixes": day["timeline_points"],
              "distance_km": trip_distance_km, "sampled_distance_km": day["stats"]["distance_km"],
              "moving_hours": day["stats"]["moving_hours"],
              "trips": len(trips), "places": len(places),
              "reminders_created": len(reminders_created), "reminders_triggered": len(reminder_events),
              "reminders_cancelled": len(reminders_cancelled),
              "active_reminders_at_end": len(reminders_active_at_end),
              "fuel_cost": (day.get("fuel") or {}).get("cost"), "fuel_litres": (day.get("fuel") or {}).get("litres"),
              "currency": (day.get("fuel") or {}).get("currency")}
    activities = [f"{mode} {values['minutes']:.0f} min ({values['distance_km']:.1f} km)"
                  for mode, values in day["activity_totals"].items() if values["count"]]
    overview = (f"{len(trips)} trips covering {totals['distance_km']:.2f} km; "
                f"{len(places)} observed places; "
                + (f"activity: {', '.join(activities)}; " if activities else "")
                + f"{len(reminder_events)} reminders triggered; {day['points']} raw fixes.")
    if home_return:
        overview += " " + home_return["summary"]
    value = {"schema_version": 7, "date": date, "timezone": timezone_name, "source": source,
             "overview": overview, "totals": totals, "activity_counts": day["activity_counts"],
             "activity_totals": day["activity_totals"], "visits": visits,
             "places": places, "zones": zones, "trips": trips, "reminders_created": reminders_created,
             "reminders_triggered": reminder_events, "reminders_cancelled": reminders_cancelled,
             "active_reminders_at_end": reminders_active_at_end, "first_fix": day["first_fix"],
             "last_fix": day["last_fix"], "coverage_gaps": day["coverage_gaps"], "coverage_note": day["coverage_note"],
             "home_return": home_return,
             "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if canonical:
        # A day nothing was recorded on is not worth a record; drop one saved before data was removed.
        if totals["raw_fixes"]:
            db.summaries.save(value, watermark, daily_brief(value))
        else:
            db.summaries.delete(date, timezone_name)
    return value


@use_case
def daily_briefs(db, date, end_date=None):
    """Briefs for consecutive local days, oldest first: up to 31 per call."""
    try:
        first = datetime.strptime(date, "%Y-%m-%d").date()
        last = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else first
    except ValueError as exc:
        raise ValueError("date and end_date must be YYYY-MM-DD") from exc
    if not 0 <= (last - first).days <= 30:
        raise ValueError("end_date must be on or up to 30 days after date")
    return [daily_brief(daily_overview(db, (first + timedelta(days=offset)).isoformat()))
            for offset in range((last - first).days + 1)]


@use_case
def stored_daily_record(db, date):
    """Exactly what the catalog holds for a date, and whether its inputs changed since.

    Reading never rebuilds the record; daily_overview() does that.
    """
    timezone_name = db.settings.get()["timezone"]
    try:
        local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(timezone_name))
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    row = db.summaries.record(date, timezone_name)
    if row is None:
        raise ValueError(f"no summary is stored for {date} yet")
    start = local_start.astimezone(timezone.utc).isoformat()
    day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    end = (day_end - timedelta(microseconds=1)).isoformat()
    home_context_start, home_context_end = home_return_context(local_start.astimezone(timezone.utc), day_end)
    return {"date": row["summary_date"], "timezone": row["timezone"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "stale": row["watermark"] != overview_watermark(
                db, start, end, None, home_context_start.isoformat(), home_context_end.isoformat()),
            "summary": json.loads(row["summary"]), "brief": json.loads(row["brief"]) if row["brief"] else None}


@use_case
def finalize_previous_day(db, now=None):
    timezone_name = db.settings.get()["timezone"]
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown configured timezone") from exc
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    date = (local_now.date() - timedelta(days=1)).isoformat()
    return daily_overview(db, date, timezone_name)


@use_case
def save_scheduled_summaries(db, now=None):
    """Save the day that ended and, on an interval schedule, today so far.

    Runs every minute. The completed day is always kept current; with the
    interval schedule today's record is rebuilt once it is that many hours old
    (a record nobody has asked for yet counts as old).
    """
    finalize_previous_day(db, now)
    settings = db.settings.get()
    if settings["summary_schedule"] != "interval":
        return
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(settings["timezone"]))
    today = local_now.date().isoformat()
    updated_at = db.summaries.updated_at(today, settings["timezone"])
    if updated_at is None:
        # Nothing recorded yet today: there is no record to save, so skip the work.
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        due = db.points.count(midnight.astimezone(timezone.utc).isoformat(), now.isoformat()) > 0
    else:
        due = now - datetime.fromisoformat(updated_at) >= timedelta(hours=settings["summary_interval_hours"])
    if due:
        daily_overview(db, today, settings["timezone"])


@use_case
def overview_watermark(db, start, end, source=None, home_context_start=None, home_context_end=None):
    """Changes whenever anything a day's record is built from does, so a stored one can be reused until then."""
    events, reminders = db.reminders.for_watermark(start, end, source)
    modes, stay_edits = db.corrections.for_watermark()
    places, labels = db.places.for_watermark()
    # A daily record can also report an early-morning return Home. Include
    # that small cross-midnight context so a prior day's stored record goes
    # stale when the return is recorded after midnight.
    context_start = home_context_start or start
    context_end = home_context_end or end
    settings = db.settings.get()
    # Stay length and mode speeds change what a day contains.
    analysis = {key: settings[key] for key in ANALYSIS_DEFAULTS}
    value = ["stay-trips-v12", modes, stay_edits, analysis, settings["source_priority"],
             fuel_signature(db), db.points.watermark(start, end, source),
             db.points.watermark(context_start, context_end, source),
             *events, places, reminders, labels]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@use_case
def daily_overviews(db, start=None, end=None, limit=90):
    """The catalog: one stored record per date with data, in the configured timezone across all sources."""
    return db.summaries.catalog(db.settings.get()["timezone"], start, end, limit)
