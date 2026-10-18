"""A month or a year at a glance, added up from its days' stored records.

Days with data but no record yet (an import, or a day nobody opened) are built a few at a time:
each call builds what fits in its time budget and says how many are left, so the page can ask
again and fill in as it goes. Towns are looked up the same way, a few at a time, since Nominatim
asks for no more than one request a second.
"""
import time
from datetime import date as Date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..domain.geo import metres
from . import ambient
from .overview import daily_overview
from .patterns import place_overview
from .use_case import use_case

BUILD_SECONDS = 6
# Towns looked up per call, at most, and the places a period's towns are told from.
TOWN_LOOKUPS = 3
TOWN_PLACES = 40
PHOTO_TOWNS = 4
# Places within this of each other with the same name are one place across days.
SAME_PLACE_M = 250


def _bounds(period):
    """(kind, first date, last date) of "YYYY-MM" or "YYYY"."""
    try:
        if len(period) == 7:
            first = Date.fromisoformat(period + "-01")
            last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return "month", first, last
        if len(period) == 4:
            year = int(period)
            return "year", Date(year, 1, 1), Date(year, 12, 31)
    except ValueError:
        pass
    raise ValueError("period must be YYYY-MM or YYYY")


def _instant(day, zone):
    return datetime.combine(day, datetime.min.time(), ZoneInfo(zone)).astimezone(timezone.utc)


def _add(target, values):
    for key, value in values.items():
        if isinstance(value, (int, float)) and value is not None:
            target[key] = round(target.get(key, 0) + value, 2)


def _places(records):
    """Every place of the period, merged across days: the time and visits there, and on how many days."""
    places = []
    for record in records:
        for place in record["summary"].get("places") or []:
            same = next((item for item in places if item["name"] == place["name"]
                         and metres(item["lat"], item["lon"], place["lat"], place["lon"]) <= SAME_PLACE_M), None)
            if same is None:
                same = {"name": place["name"], "address": place.get("address") or "", "category": place.get("category") or "",
                        "home": place.get("home", False), "zone": place.get("zone"), "lat": place["lat"], "lon": place["lon"],
                        "visits": 0, "observed_minutes": 0.0, "days": 0, "first_date": record["date"]}
                places.append(same)
            same["visits"] += place.get("visits") or 0
            same["observed_minutes"] = round(same["observed_minutes"] + (place.get("observed_minutes") or 0), 1)
            same["days"] += 1
            same["last_date"] = record["date"]
    return sorted(places, key=lambda item: item["observed_minutes"], reverse=True)


def _towns(db, places, period, kind):
    """The towns of the period's places, by time spent, with a photo of the first few; and how many
    places are still waiting for their town to be looked up."""
    towns, waiting, lookups = {}, 0, 0
    for place in places[:TOWN_PLACES]:
        key = f"locality:{round(place['lat'], 2):.2f},{round(place['lon'], 2):.2f}"
        cached = db.caches.lookup(key, "nominatim")
        if cached is None:
            if lookups >= TOWN_LOOKUPS:
                waiting += 1
                continue
            if lookups:
                time.sleep(1.1)
            lookups += 1
        try:
            town = cached if cached is not None else ambient.place_of(db, place["lat"], place["lon"])
        except Exception as exc:
            print(f"GeoLogs locality: {exc}", flush=True)
            continue
        if not town or not (town.get("city") or town.get("region")):
            continue
        name = town.get("city") or town.get("region")
        item = towns.setdefault((name, town.get("country")), {
            "name": name, "region": town.get("region") if town.get("region") != name else None,
            "country": town.get("country"), "country_code": town.get("country_code"),
            "lat": place["lat"], "lon": place["lon"], "observed_minutes": 0.0, "places": 0, "place": town})
        item["observed_minutes"] = round(item["observed_minutes"] + place["observed_minutes"], 1)
        item["places"] += 1
    ordered = sorted(towns.values(), key=lambda item: item["observed_minutes"], reverse=True)
    for index, town in enumerate(ordered):
        place = town.pop("place")
        if index < PHOTO_TOWNS:
            try:
                town["photo"] = ambient.city_photo(db, place, f"{period}:{town['name']}")
            except Exception as exc:
                print(f"GeoLogs Unsplash: {exc}", flush=True)
                town["photo"] = None
    return ordered, waiting


@use_case
def period_summary(db, period, budget_seconds=BUILD_SECONDS):
    """A month ("2026-10") or year ("2026"): totals, how you travelled, the places and towns you
    spent time in, its highlights, and each day's distance for a calendar. `pending` counts the
    days and towns still to add; ask again to fill them in."""
    kind, first, last = _bounds(period)
    settings = db.settings.get()
    zone = settings["timezone"]
    today = datetime.now(ZoneInfo(zone)).date()
    last = min(last, today)
    if first > today:
        raise ValueError("that period has not started yet")
    start, end = _instant(first, zone), _instant(last + timedelta(days=1), zone) - timedelta(microseconds=1)
    with_data = db.points.local_dates(start.isoformat(), end.isoformat(), zone)
    stored = set(db.summaries.dates_between(zone, first.isoformat(), last.isoformat()))
    # Today keeps changing, so it is always built fresh; other days once.
    missing = [day for day in with_data if day not in stored or day == today.isoformat()]
    deadline = time.monotonic() + budget_seconds
    built = 0
    for day in missing:
        if built and time.monotonic() > deadline:
            break
        daily_overview(db, day, zone)
        built += 1
    records = db.summaries.records_between(zone, first.isoformat(), last.isoformat())
    totals, activity = {"days": len(records)}, {}
    by_day, trips, seen_trips = [], [], set()
    for record in records:
        summary = record["summary"]
        day_totals = summary.get("totals") or {}
        _add(totals, {key: day_totals.get(key) for key in ("moving_hours", "reminders_triggered")})
        totals["currency"] = totals.get("currency") or day_totals.get("currency")
        for mode, values in (summary.get("activity_totals") or {}).items():
            _add(activity.setdefault(mode, {}), {key: values.get(key) for key in ("count", "minutes", "distance_km")})
        by_day.append({"date": record["date"], "distance_km": day_totals.get("distance_km") or 0,
                       "trips": day_totals.get("trips") or 0, "places": day_totals.get("places") or 0,
                       "moving_hours": day_totals.get("moving_hours") or 0})
        # A trip across midnight is in both days' records: it counts once, on the day it began.
        for trip in summary.get("trips") or []:
            if trip["start"] not in seen_trips:
                seen_trips.add(trip["start"])
                trips.append({**trip, "date": record["date"]})
    totals["trips"] = len(trips)
    _add(totals, {"distance_km": sum(trip.get("distance_km") or 0 for trip in trips),
                  "fuel_cost": sum(trip.get("fuel_cost") or 0 for trip in trips),
                  "fuel_litres": sum(trip.get("fuel_litres") or 0 for trip in trips)})
    places = _places(records)
    away = [place for place in places if not place["home"]]
    towns, towns_waiting = _towns(db, away, period, kind)
    # Places you went for the first time in this period, of all you have been.
    window_start, window_end = start.isoformat(), end.isoformat()
    new_places = [item for item in place_overview(db, 5000)["visited"]
                  if item.get("first_seen") and window_start <= item["first_seen"] <= window_end]
    busiest = max(by_day, key=lambda day: day["distance_km"], default=None)
    longest = max(trips, key=lambda trip: trip.get("distance_km") or 0, default=None)
    weekdays = [{"weekday": index, "distance_km": 0.0, "days": 0} for index in range(7)]
    for day in by_day:
        weekday = weekdays[Date.fromisoformat(day["date"]).weekday()]
        weekday["distance_km"] = round(weekday["distance_km"] + day["distance_km"], 2)
        weekday["days"] += 1
    months = []
    if kind == "year":
        for month in range(1, 13):
            key = f"{first.year}-{month:02d}"
            days = [day for day in by_day if day["date"].startswith(key)]
            months.append({"month": key, "days": len(days), "distance_km": round(sum(day["distance_km"] for day in days), 2),
                           "trips": sum(day["trips"] for day in days)})
    totals["places"] = len(away)
    return {"period": period, "kind": kind, "first": first.isoformat(), "last": last.isoformat(),
            "timezone": zone, "days_with_data": len(with_data),
            "pending": {"days": max(0, len(missing) - built), "towns": towns_waiting},
            "totals": totals, "activity": activity, "places": away[:60],
            "home": next(({"observed_minutes": place["observed_minutes"], "days": place["days"]}
                          for place in places if place["home"]), None),
            "new_places": [{key: item.get(key) for key in ("display_name", "category", "lat", "lon", "visits",
                                                           "observed_minutes", "first_seen")}
                           for item in sorted(new_places, key=lambda item: item["first_seen"])][:30],
            "new_place_count": len(new_places), "towns": towns,
            "highlights": {"busiest_day": busiest if busiest and busiest["distance_km"] else None,
                           "longest_trip": {key: longest.get(key) for key in ("date", "from", "to", "mode", "distance_km",
                                                                               "duration_minutes")}
                           if longest and longest.get("distance_km") else None},
            "weekdays": weekdays, "months": months, "days": by_day}
