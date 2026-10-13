"""Fuel cost of your drives: your car's economy times a price for each day.

Prices come from you (each change dated, so earlier drives keep their price)
or, in the US, from the EIA's weekly retail averages for your region, which
is worked out from where you live. Everything is stored per litre; the
browser shows gallons when you use imperial units.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import places as naming
from ..domain.fuel import EIA_AREAS, EIA_PRODUCTS, eia_series, LITRES_PER_GALLON, manual_price, price_key, price_on, STATE_AREAS, state_from_address, trip_fuel
from ..integrations import eia
from .position import latest
from .settings import save_settings
from .stays import anchored_trips
from .use_case import use_case


# Weekly data: check for a new week at most this often.
EIA_REFRESH = timedelta(hours=12)


def _today(db):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(db.settings.get()["timezone"])).date().isoformat()


@use_case
def set_manual_price(db, price_per_litre, effective_date=None):
    """Your price from a date on (today by default); drives before it keep the earlier price."""
    day = effective_date or _today(db)
    db.fuel.save_price("manual", day, manual_price(price_per_litre), datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.caches.clear_analysis()


@use_case
def detect_area(db):
    """Your EIA price area, from where you live (Home, else your latest fix)."""
    home = next((place for place in db.places.saved() if place["name"].strip().lower() in ("home", "my home")), None)
    spot = (home["lat"], home["lon"]) if home else None
    if spot is None:
        position = latest(db)
        spot = (position["lat"], position["lon"]) if position else None
    if spot is None:
        return None, None
    state = state_from_address((home or {}).get("address")) or state_from_address(naming.place_for_coordinate(db, *spot)["address"])
    return (STATE_AREAS.get(state), state) if state else (None, None)


def _area(db, settings):
    if settings["fuel_area"]:
        return settings["fuel_area"], None
    cached = _state(db).get("detected")
    if cached and cached.get("area"):
        return cached["area"], cached.get("state")
    area, state = detect_area(db)
    if area:
        _save_state(db, {**_state(db), "detected": {"area": area, "state": state}})
    return area, state


def _state(db):
    """What fuel pricing remembers: the area detected from where you live, and when each series was fetched."""
    return db.settings.value("fuel_state", {})


def _save_state(db, value):
    db.settings.set_value("fuel_state", value)


@use_case
def refresh_prices(db, force=False):
    """Fetch the EIA's weekly prices for your area and grade (five years the first time).

    Runs while you use your own price too, so Settings can show what Automatic
    would charge. Does nothing without EIA_API_KEY, outside the US, or within
    EIA_REFRESH of the last fetch unless forced. Returns what happened.
    """
    settings = db.settings.get()
    if not eia.api_key():
        return {"fetched": 0, "reason": "no_key"}
    area, _state_code = _area(db, settings)
    if not area:
        return {"fetched": 0, "reason": "no_area"}
    key = eia_series(settings, area)
    state = _state(db)
    last = state.get("fetched", {}).get(key)
    if not force and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < EIA_REFRESH:
        return {"fetched": 0, "reason": "fresh"}
    known = db.fuel.count(key)
    rows = eia.weekly_prices(area, EIA_PRODUCTS[settings["fuel_grade"]], 8 if known else 260)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        if row.get("value") in (None, ""):
            continue
        db.fuel.save_price(key, row["period"], round(float(row["value"]) / LITRES_PER_GALLON, 4), now)
    _save_state(db, {**state, "fetched": {**state.get("fetched", {}), key: now}})
    db.caches.clear_analysis()
    return {"fetched": len(rows), "reason": "ok"}


@use_case
def fuel_context(db):
    """What costing a drive needs: economy, the price series, and a signature for caches."""
    settings = db.settings.get()
    economy = settings["fuel_economy_l100km"]
    if not economy:
        return None
    area = settings["fuel_area"] or (_state(db).get("detected") or {}).get("area")
    key = price_key(settings, area)
    table = db.fuel.table(key)
    if not table:
        return None
    return {"economy": economy, "table": table, "currency": settings["currency"], "key": key}


@use_case
def fuel_signature(db):
    """Changes whenever a drive's cost could: economy, currency, the series, and its prices."""
    settings = db.settings.get()
    area = settings["fuel_area"] or (_state(db).get("detected") or {}).get("area")
    key = price_key(settings, area)
    count, fetched_at = db.fuel.signature(key)
    return [settings["fuel_economy_l100km"], settings["currency"], key, count, fetched_at]


@use_case
def fuel_status(db):
    """Everything the Car & fuel settings show: the price in use, and both sources' latest prices."""
    settings = db.settings.get()
    saved = _state(db)
    area, state = (settings["fuel_area"], None) if settings["fuel_area"] else (
        (saved.get("detected") or {}).get("area"), (saved.get("detected") or {}).get("state"))
    today = _today(db)

    def latest(key):
        table = db.fuel.table(key)
        found = price_on(table, today) if table else None
        return {"price_per_l": found[1] if found else None, "price_date": found[0] if found else None}

    series = eia_series(settings, area)
    manual = latest("manual")
    automatic = {**latest(series), "fetched_at": (saved.get("fetched") or {}).get(series)}
    current = manual if settings["fuel_price_source"] == "manual" else automatic
    return {"eia_key": bool(eia.api_key()), "area": area, "area_label": EIA_AREAS.get(area), "state": state,
            "areas": EIA_AREAS, **current, "manual": manual, "eia": automatic,
            "fetched_at": automatic["fetched_at"], "currency": settings["currency"]}


@use_case
def fuel_costs(db, days=30, now=None):
    """Fuel spent on drives over the last `days` local days, and the average per day.

    The average counts every calendar day in the window from your first
    recorded day on, so quiet days lower it as they would your spending.
    """
    return fuel_cost_windows(db, (days,), now)[int(days)]


@use_case
def fuel_cost_windows(db, windows=(7, 30), now=None):
    """fuel_costs for several windows at once, finding the trips of the longest only once."""
    context = fuel_context(db)
    settings = db.settings.get()
    zone = ZoneInfo(settings["timezone"])
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    windows = sorted({int(days) for days in windows})
    earliest = db.points.span()[0]
    earliest_day = datetime.fromisoformat(earliest).astimezone(zone).date() if earliest else None
    results = {}
    for days in windows:
        first = today - timedelta(days=days - 1)
        if earliest_day:
            first = max(first, earliest_day)
        results[days] = {"days": days, "covered_days": max(0, (today - first).days + 1), "drives": 0, "driving_km": 0.0,
                         "litres": 0.0, "cost": 0.0, "currency": settings["currency"], "configured": context is not None,
                         "first": first.isoformat()}
    if context is not None and earliest:
        first = today - timedelta(days=windows[-1] - 1)
        start = datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc)
        end = datetime.combine(today + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc) - timedelta(microseconds=1)
        for trip in anchored_trips(db, start.isoformat(), end.isoformat()):
            if trip.get("mode") != "driving":
                continue
            day = datetime.fromisoformat(trip["start"]).astimezone(zone).date()
            cost = trip_fuel(context, trip, day.isoformat())
            if not cost:
                continue
            for days, totals in results.items():
                if day >= today - timedelta(days=days - 1):
                    totals["drives"] += 1
                    totals["driving_km"] += trip["distance_km"]
                    totals["litres"] += cost["litres"]
                    totals["cost"] += cost["cost"]
    for totals in results.values():
        totals.pop("first")
        configured = totals["configured"] and earliest
        totals.update(driving_km=round(totals["driving_km"], 1), litres=round(totals["litres"], 1), cost=round(totals["cost"], 2),
                      cost_per_day=round(totals["cost"] / totals["covered_days"], 2) if configured and totals["covered_days"] else None)
    return results


FUEL_SETTINGS = ("currency", "fuel_economy_l100km", "fuel_grade", "fuel_price_source", "fuel_area")


@use_case
def update_fuel(db, raw):
    """Save the Car & fuel settings and, when given, your price (dated today), then refresh EIA prices.

    The settings and your price are saved together or not at all. They are
    kept before the EIA fetch, which can be slow or fail: an outage there
    never loses them, and the database is not held while waiting on it.
    """
    price = raw.get("price_per_l")
    if price not in (None, ""):
        manual_price(price)  # a bad price saves nothing
    save_settings(db, {key: raw[key] for key in FUEL_SETTINGS if key in raw})
    if price not in (None, ""):
        set_manual_price(db, price)
    db.commit()
    try:
        refreshed = refresh_prices(db, force=db.settings.get()["fuel_price_source"] == "auto")
    except Exception as exc:  # the EIA being down shouldn't fail the save
        db.rollback()
        refreshed = {"fetched": 0, "reason": "error", "error": str(exc)}
    costs = fuel_cost_windows(db, (7, 30))
    return {**fuel_status(db), "refresh": refreshed, "last_7_days": costs[7], "last_30_days": costs[30]}
