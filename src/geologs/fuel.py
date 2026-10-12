"""Fuel cost of your drives: your car's economy times a price for each day.

Prices come from you (each change dated, so earlier drives keep their price)
or, in the US, from the EIA's weekly retail averages for your region, which
is worked out from where you live. Everything is stored per litre; the
browser shows gallons when you use imperial units.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

LITRES_PER_GALLON = 3.785411784
# EIA weekly retail price areas: a state or city series where one exists, else the PADD region.
EIA_AREAS = {
    "NUS": "U.S. average", "R1X": "New England", "R1Y": "Central Atlantic", "R1Z": "Lower Atlantic",
    "R20": "Midwest", "R30": "Gulf Coast", "R40": "Rocky Mountain", "R50": "West Coast",
    "R5XCA": "West Coast except California", "SCA": "California", "SCO": "Colorado", "SFL": "Florida",
    "SMA": "Massachusetts", "SMN": "Minnesota", "SNY": "New York", "SOH": "Ohio", "STX": "Texas", "SWA": "Washington",
}
EIA_PRODUCTS = {"regular": "EPMR", "midgrade": "EPMM", "premium": "EPMP", "diesel": "EPD2D"}
_PADD = {
    "R1X": "CT ME MA NH RI VT", "R1Y": "DE DC MD NJ NY PA", "R1Z": "FL GA NC SC VA WV",
    "R20": "IL IN IA KS KY MI MN MO NE ND SD OH OK TN WI", "R30": "AL AR LA MS NM TX",
    "R40": "CO ID MT UT WY", "R5XCA": "AK AZ HI NV OR WA",
}
STATE_AREAS = {state: area for area, states in _PADD.items() for state in states.split()}
STATE_AREAS.update({"CA": "SCA", "CO": "SCO", "FL": "SFL", "MA": "SMA", "MN": "SMN", "NY": "SNY", "OH": "SOH",
                    "TX": "STX", "WA": "SWA"})
STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
# Weekly data: check for a new week at most this often.
EIA_REFRESH = timedelta(hours=12)
EIA_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"


def state_from_address(address):
    """The US state code in a formatted address ("Little Rock, AR 72223" or "…, Arkansas 72223, United States")."""
    text = str(address or "")
    if not re.search(r"United States|USA|\bUS\b|, [A-Z]{2} \d{5}", text):
        return None
    for name in sorted(STATE_NAMES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return STATE_NAMES[name]
    match = re.search(r",\s*([A-Z]{2})\s+\d{5}", text)
    return match.group(1) if match and match.group(1) in STATE_AREAS else None


def eia_key():
    return os.environ.get("EIA_API_KEY", "").strip()


def price_key(settings, area):
    """Which price series applies: yours, or the EIA's for an area and grade."""
    if settings["fuel_price_source"] == "manual":
        return "manual"
    return f"eia:{area}:{EIA_PRODUCTS[settings['fuel_grade']]}" if area else None


def _today(store):
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo(store.settings()["timezone"])).date().isoformat()


def set_manual_price(store, price_per_litre, effective_date=None):
    """Your price from a date on (today by default); drives before it keep the earlier price."""
    price = float(price_per_litre)
    if not 0.01 <= price <= 100:
        raise ValueError("price must be between 0.01 and 100 per litre")
    day = effective_date or _today(store)
    store.db.execute("""INSERT INTO fuel_prices(source,effective_date,price_per_l,fetched_at) VALUES ('manual',?,?,?)
        ON CONFLICT(source,effective_date) DO UPDATE SET price_per_l=excluded.price_per_l,fetched_at=excluded.fetched_at""",
                     (day, round(price, 4), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    store.clear_analysis_cache(commit=False)
    store.db.commit()


def price_table(store, key):
    """Every price of one series, oldest first, for looking up many drives at once."""
    if not key:
        return []
    return [(row["effective_date"], row["price_per_l"]) for row in store._rows(
        "SELECT effective_date,price_per_l FROM fuel_prices WHERE source=? ORDER BY effective_date", (key,))]


def price_on(table, day):
    """The price in effect on a day: the latest from on or before it, else the earliest known."""
    found = None
    for effective, price in table:
        if effective > day:
            break
        found = (effective, price)
    return found or (table[0] if table else None)


def detect_area(store):
    """Your EIA price area, from where you live (Home, else your latest fix)."""
    from .core import place_for_coordinate
    home = next((place for place in store.places() if place["name"].strip().lower() in ("home", "my home")), None)
    spot = (home["lat"], home["lon"]) if home else None
    if spot is None:
        latest = store.latest()
        spot = (latest["lat"], latest["lon"]) if latest else None
    if spot is None:
        return None, None
    state = state_from_address((home or {}).get("address")) or state_from_address(place_for_coordinate(store, *spot)["address"])
    return (STATE_AREAS.get(state), state) if state else (None, None)


def _area(store, settings):
    if settings["fuel_area"]:
        return settings["fuel_area"], None
    cached = _state(store).get("detected")
    if cached and cached.get("area"):
        return cached["area"], cached.get("state")
    area, state = detect_area(store)
    if area:
        _save_state(store, {**_state(store), "detected": {"area": area, "state": state}})
    return area, state


def _state(store):
    row = store.db.execute("SELECT value FROM app_settings WHERE key='fuel_state'").fetchone()
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def _save_state(store, value):
    store.db.execute("""INSERT INTO app_settings(key,value) VALUES ('fuel_state',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (json.dumps(value),))
    store.db.commit()


def refresh_prices(store, force=False):
    """Fetch the EIA's weekly prices for your area and grade (five years the first time).

    Does nothing for your own price, without EIA_API_KEY, outside the US, or
    within EIA_REFRESH of the last fetch unless forced. Returns what happened.
    """
    settings = store.settings()
    if settings["fuel_price_source"] != "auto":
        return {"fetched": 0, "reason": "manual"}
    if not eia_key():
        return {"fetched": 0, "reason": "no_key"}
    area, _state_code = _area(store, settings)
    if not area:
        return {"fetched": 0, "reason": "no_area"}
    key = price_key(settings, area)
    state = _state(store)
    last = state.get("fetched", {}).get(key)
    if not force and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < EIA_REFRESH:
        return {"fetched": 0, "reason": "fresh"}
    known = store.db.execute("SELECT COUNT(*) AS n FROM fuel_prices WHERE source=?", (key,)).fetchone()["n"]
    params = [("api_key", eia_key()), ("frequency", "weekly"), ("data[0]", "value"),
              ("facets[duoarea][]", area), ("facets[product][]", EIA_PRODUCTS[settings["fuel_grade"]]),
              ("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", "8" if known else "260")]
    with urllib.request.urlopen(EIA_URL + "?" + urllib.parse.urlencode(params), timeout=20) as response:
        rows = json.loads(response.read().decode())["response"]["data"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        if row.get("value") in (None, ""):
            continue
        store.db.execute("""INSERT INTO fuel_prices(source,effective_date,price_per_l,fetched_at) VALUES (?,?,?,?)
            ON CONFLICT(source,effective_date) DO UPDATE SET price_per_l=excluded.price_per_l,fetched_at=excluded.fetched_at""",
                         (key, row["period"], round(float(row["value"]) / LITRES_PER_GALLON, 4), now))
    _save_state(store, {**state, "fetched": {**state.get("fetched", {}), key: now}})
    store.clear_analysis_cache(commit=False)
    store.db.commit()
    return {"fetched": len(rows), "reason": "ok"}


def fuel_context(store):
    """What costing a drive needs: economy, the price series, and a signature for caches."""
    settings = store.settings()
    economy = settings["fuel_economy_l100km"]
    if not economy:
        return None
    area = settings["fuel_area"] or (_state(store).get("detected") or {}).get("area")
    key = price_key(settings, area)
    table = price_table(store, key)
    if not table:
        return None
    return {"economy": economy, "table": table, "currency": settings["currency"], "key": key}


def fuel_signature(store):
    """Changes whenever a drive's cost could: economy, currency, the series, and its prices."""
    settings = store.settings()
    area = settings["fuel_area"] or (_state(store).get("detected") or {}).get("area")
    key = price_key(settings, area)
    row = store.db.execute("SELECT COUNT(*) AS n,COALESCE(MAX(fetched_at),'') AS at FROM fuel_prices WHERE source=?",
                           (key or "",)).fetchone()
    return [settings["fuel_economy_l100km"], settings["currency"], key, row["n"], row["at"]]


def trip_fuel(context, trip, day):
    """Litres and cost of one drive on a local day, or None when it is not a drive or cannot be costed."""
    if context is None or trip.get("mode") != "driving":
        return None
    found = price_on(context["table"], day)
    if not found:
        return None
    litres = (trip.get("distance_km") or 0) * context["economy"] / 100
    return {"litres": round(litres, 3), "cost": round(litres * found[1], 2), "price_per_l": found[1],
            "price_date": found[0]}


def fuel_status(store):
    """Everything the Car & fuel settings show."""
    settings = store.settings()
    area, state = (settings["fuel_area"], None) if settings["fuel_area"] else (
        (_state(store).get("detected") or {}).get("area"), (_state(store).get("detected") or {}).get("state"))
    key = price_key(settings, area)
    table = price_table(store, key)
    today = _today(store)
    current = price_on(table, today) if table else None
    return {"eia_key": bool(eia_key()), "area": area, "area_label": EIA_AREAS.get(area), "state": state,
            "areas": EIA_AREAS, "price_per_l": current[1] if current else None, "price_date": current[0] if current else None,
            "fetched_at": (_state(store).get("fetched") or {}).get(key), "currency": settings["currency"]}
