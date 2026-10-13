"""Fuel price areas and series, and the cost of a drive."""
import re


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


def eia_series(settings, area):
    """The EIA series for an area and your grade."""
    return f"eia:{area}:{EIA_PRODUCTS[settings['fuel_grade']]}" if area else None


def price_key(settings, area):
    """Which price series applies: yours, or the EIA's for an area and grade."""
    if settings["fuel_price_source"] == "manual":
        return "manual"
    return eia_series(settings, area)


def price_on(table, day):
    """The price in effect on a day: the latest from on or before it, else the earliest known."""
    found = None
    for effective, price in table:
        if effective > day:
            break
        found = (effective, price)
    return found or (table[0] if table else None)


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


def manual_price(value):
    """A price per litre you entered, checked: between 0.01 and 100."""
    price = float(value)
    if not 0.01 <= price <= 100:
        raise ValueError("price must be between 0.01 and 100 per litre")
    return round(price, 4)
