"""Settings and their defaults."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math
from .fuel import EIA_AREAS


# How stays and travel modes are read from samples. Changing one rebuilds
# stays and makes stored daily summaries stale. Speeds are median km/h: below
# walking_max is a walk, below running_max a run, below cycling_max a ride, and
# faster is a drive; stretches at driving_min or more count as driving even
# when the median is slower (stop-and-go traffic). quick_stop_minutes finds
# shorter stops on the way (a fuel stop); 0 turns that off.
ANALYSIS_DEFAULTS = {"stay_min_minutes": 15, "quick_stop_minutes": 3, "walking_max_kmh": 6.5,
                     "running_max_kmh": 15.0, "cycling_max_kmh": 25.0, "driving_min_kmh": 35.0}
# Completed days are always saved after midnight; "interval" also saves today so far.
SUMMARY_SCHEDULES = ("nightly", "interval")
# An empty places_provider means the first configured provider (see places_provider()).
# source_priority lists trackers, most trusted first; when several report the
# same time, the first one with data wins. Unlisted sources follow, busiest first.
DEFAULT_SETTINGS = {"speed_strategy": "reported", "reported_speed_offset_kmh": 0.0, "timezone": "UTC",
                    "places_provider": "", **ANALYSIS_DEFAULTS,
                    "summary_schedule": "nightly", "summary_interval_hours": 3, "source_priority": [],
                    **{"units": "metric", "currency": "USD", "fuel_economy_l100km": None, "fuel_grade": "regular",
                       "fuel_price_source": "auto", "fuel_area": ""}}
# Display units; everything is stored metric. Fuel economy is kept in L/100 km and
# prices per litre, whatever you entered them in.
UNIT_SYSTEMS = ("metric", "imperial")
FUEL_GRADES = ("regular", "midgrade", "premium", "diesel")


def validate(raw, current, providers):
    """Settings from a request (`raw`) over the `current` ones, checked; raises ValueError for a bad value.

    `providers` maps each place provider to its label, env variable, and
    whether it is configured.
    """
    strategy = str(raw.get("speed_strategy", current["speed_strategy"]))
    if strategy not in ("reported", "calculated"):
        raise ValueError("speed_strategy must be reported or calculated")
    offset = float(raw.get("reported_speed_offset_kmh", current["reported_speed_offset_kmh"]))
    if not math.isfinite(offset) or not -100 <= offset <= 100:
        raise ValueError("reported_speed_offset_kmh must be between -100 and 100")
    timezone_name = str(raw.get("timezone", current["timezone"])).strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone, such as America/Chicago") from exc
    provider = str(raw.get("places_provider", current["places_provider"]) or "").strip()
    if provider and provider not in providers:
        raise ValueError("places_provider must be one of: " + ", ".join(providers))
    if provider and provider != current["places_provider"] and not providers[provider]["configured"]:
        raise ValueError(f"Set {providers[provider]['env']} to use {providers[provider]['label']}")
    stay_minutes = float(raw.get("stay_min_minutes", current["stay_min_minutes"]))
    if not stay_minutes.is_integer() or not 5 <= stay_minutes <= 120:
        raise ValueError("stay_min_minutes must be a whole number from 5 to 120")
    quick_minutes = float(raw.get("quick_stop_minutes", current["quick_stop_minutes"]))
    if not quick_minutes.is_integer() or not (quick_minutes == 0 or 2 <= quick_minutes < stay_minutes):
        raise ValueError("quick_stop_minutes must be 0 (off) or a whole number from 2 to below the minimum stay")
    speeds = {}
    for key in ("walking_max_kmh", "running_max_kmh", "cycling_max_kmh", "driving_min_kmh"):
        speeds[key] = float(raw.get(key, current[key]))
        if not math.isfinite(speeds[key]) or not 1 <= speeds[key] <= 200:
            raise ValueError(f"{key} must be between 1 and 200 km/h")
    if not speeds["walking_max_kmh"] < speeds["running_max_kmh"] < speeds["cycling_max_kmh"] <= speeds["driving_min_kmh"]:
        raise ValueError("speeds must rise from walking to running to cycling, and driving must be at least the cycling limit")
    schedule = str(raw.get("summary_schedule", current["summary_schedule"]))
    if schedule not in SUMMARY_SCHEDULES:
        raise ValueError("summary_schedule must be nightly or interval")
    interval = float(raw.get("summary_interval_hours", current["summary_interval_hours"]))
    if not interval.is_integer() or not 1 <= interval <= 12:
        raise ValueError("summary_interval_hours must be a whole number from 1 to 12")
    priority = raw.get("source_priority", current["source_priority"])
    if not isinstance(priority, list) or len(priority) > 100:
        raise ValueError("source_priority must be a list of at most 100 source names")
    priority = list(dict.fromkeys(str(name).strip()[:80] for name in priority if str(name).strip()))
    units = str(raw.get("units", current["units"]))
    if units not in UNIT_SYSTEMS:
        raise ValueError("units must be metric or imperial")
    currency = str(raw.get("currency", current["currency"]) or "").strip()
    if not currency or len(currency) > 5:
        raise ValueError("currency must be a symbol or code of 1 to 5 characters, such as $ or EUR")
    economy = raw.get("fuel_economy_l100km", current["fuel_economy_l100km"])
    if economy not in (None, ""):
        economy = float(economy)
        if not math.isfinite(economy) or not 1 <= economy <= 50:
            raise ValueError("fuel economy must be between 1 and 50 L/100 km (about 5 to 235 mpg)")
        economy = round(economy, 3)
    else:
        economy = None
    grade = str(raw.get("fuel_grade", current["fuel_grade"]))
    if grade not in FUEL_GRADES:
        raise ValueError("fuel_grade must be one of: " + ", ".join(FUEL_GRADES))
    price_source = str(raw.get("fuel_price_source", current["fuel_price_source"]))
    if price_source not in ("auto", "manual"):
        raise ValueError("fuel_price_source must be auto or manual")
    area = str(raw.get("fuel_area", current["fuel_area"]) or "").strip().upper()
    if area and area not in EIA_AREAS:
        raise ValueError("fuel_area must be one of: " + ", ".join(EIA_AREAS))
    values = {"speed_strategy": strategy, "reported_speed_offset_kmh": round(offset, 2),
              "timezone": timezone_name, "places_provider": provider, "stay_min_minutes": int(stay_minutes),
              "quick_stop_minutes": int(quick_minutes),
              **{key: round(value, 1) for key, value in speeds.items()},
              "summary_schedule": schedule, "summary_interval_hours": int(interval),
              "source_priority": priority, "units": units, "currency": currency,
              "fuel_economy_l100km": economy, "fuel_grade": grade, "fuel_price_source": price_source, "fuel_area": area}
    return values
