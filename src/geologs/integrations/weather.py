"""Weather from Open-Meteo (free, no key): the sky now and today's range, or a past day's."""
import json
import urllib.parse
import urllib.request

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Reanalysis for days older than the forecast API keeps (about three months).
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset"
CURRENT = "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,is_day,wind_speed_10m,precipitation"


def _get(url, params):
    request = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "GeoLogs/0.1 (personal self-hosted location archive)"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def _day(data):
    daily = data.get("daily") or {}
    first = lambda key: (daily.get(key) or [None])[0]
    return {"date": first("time"), "code": first("weather_code"), "high_c": first("temperature_2m_max"),
            "low_c": first("temperature_2m_min"), "precipitation_mm": first("precipitation_sum"),
            "rain_chance": first("precipitation_probability_max"), "sunrise": first("sunrise"), "sunset": first("sunset")}


def today(lat, lon, timezone):
    """{current: {...}, day: {...}} at a position; temperatures in °C, wind in km/h, local times."""
    data = _get(FORECAST_URL, {"latitude": lat, "longitude": lon, "timezone": timezone, "forecast_days": 1,
                               "current": CURRENT, "daily": DAILY + ",precipitation_probability_max"})
    now = data.get("current") or {}
    return {"current": {"time": now.get("time"), "code": now.get("weather_code"), "is_day": bool(now.get("is_day", 1)),
                        "temperature_c": now.get("temperature_2m"), "feels_like_c": now.get("apparent_temperature"),
                        "humidity": now.get("relative_humidity_2m"), "wind_kmh": now.get("wind_speed_10m"),
                        "precipitation_mm": now.get("precipitation")},
            "day": _day(data)}


def past_day(lat, lon, timezone, date, archived):
    """{current: None, day: {...}} for one past local date; `archived` asks the reanalysis archive."""
    data = _get(ARCHIVE_URL if archived else FORECAST_URL,
                {"latitude": lat, "longitude": lon, "timezone": timezone, "start_date": date, "end_date": date, "daily": DAILY})
    return {"current": None, "day": _day(data)}
