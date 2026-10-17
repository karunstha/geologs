"""The setting for a day on History: the town you were in, its weather, and a photo of it in that weather.

Each part is looked up on its own and left out when it cannot be had, so a
weather outage never hides the photo, nor a missing Unsplash key the weather.
Positions are rounded to about a kilometre before anything is asked of an
outside service.
"""
import hashlib
import threading
import time
from datetime import date as Date, datetime, timedelta, timezone as UTC
from zoneinfo import ZoneInfo

from ..domain.weather import condition
from ..integrations import locality as localities
from ..integrations import unsplash
from ..integrations import weather as forecasts
from .use_case import use_case

# Weather is kept in memory: ten minutes for today, six hours for a past day.
_weather = {}
_weather_lock = threading.Lock()
# Photos already reported to Unsplash by this server, so each is reported once.
_reported = set()
PHOTO_SEARCH_DAYS = 7
# The forecast API keeps about three months; older days come from the archive.
FORECAST_HISTORY_DAYS = 80


def _note(part, exc):
    print(f"GeoLogs {part}: {exc}", flush=True)


def _place(db, lat, lon):
    key = f"locality:{lat:.2f},{lon:.2f}"
    cached = db.caches.lookup(key, "nominatim")
    if cached is not None:
        return cached
    value = localities.locality(lat, lon)
    db.caches.save_lookup(key, "nominatim", value)
    return value


def _forecast(lat, lon, day, zone, today):
    key = (lat, lon, day, zone)
    now = time.monotonic()
    with _weather_lock:
        hit = _weather.get(key)
        if hit and hit[0] > now:
            return hit[1]
    if day == today:
        value, ttl = forecasts.today(lat, lon, zone), 600
    else:
        archived = (Date.fromisoformat(today) - Date.fromisoformat(day)).days > FORECAST_HISTORY_DAYS
        value, ttl = forecasts.past_day(lat, lon, zone, day, archived), 6 * 3600
    with _weather_lock:
        if len(_weather) > 256:
            _weather.clear()
        _weather[key] = (now + ttl, value)
    return value


def _sky(forecast):
    current, day = forecast["current"], forecast["day"]
    code = current["code"] if current and current.get("code") is not None else day.get("code")
    if code is None:
        return None
    return {**condition(code, current["is_day"] if current else True), "code": code,
            "current": current, "day": day}


def _photo_searches(place, sky):
    """Most to least particular: "Little Rock Arkansas rain", "Little Rock rain", "Little Rock Arkansas", …"""
    city, region = place.get("city") if place else None, place.get("region") if place else None
    if region and city and region.lower() == city.lower():
        region = None
    word = sky["photo_word"] if sky else None
    candidates = []
    if city:
        candidates += [f"{city} {region} {word}" if region and word else None, f"{city} {word}" if word else None,
                       f"{city} {region}" if region else None, city]
    if region:
        candidates += [f"{region} {word}" if word else None, region]
    if word:
        candidates.append(f"{word} landscape")
    seen, searches = set(), []
    for query in candidates:
        if query and query.lower() not in seen:
            seen.add(query.lower())
            searches.append(query)
    return searches


def _photos(db, query):
    key = "unsplash:" + query.lower()
    cached = db.caches.lookup(key, "unsplash")
    fresh_after = (datetime.now(UTC.utc) - timedelta(days=PHOTO_SEARCH_DAYS)).isoformat()
    if cached is not None and cached.get("fetched_at", "") > fresh_after:
        return cached["photos"]
    photos = unsplash.search(query)
    db.caches.save_lookup(key, "unsplash", {"fetched_at": datetime.now(UTC.utc).isoformat(timespec="seconds"),
                                            "photos": photos})
    return photos


def _report(photo):
    if not photo.get("download_location") or photo["id"] in _reported:
        return
    _reported.add(photo["id"])

    def send():
        try:
            unsplash.report_download(photo["download_location"])
        except Exception as exc:
            _note("Unsplash download report", exc)
    threading.Thread(target=send, daemon=True).start()


def _photo(db, place, sky, day):
    """A photo for the first search that finds any; the same one all day, a different one the next."""
    for query in _photo_searches(place, sky):
        photos = _photos(db, query)
        if photos:
            choices = photos[:8]
            pick = choices[int(hashlib.sha256(f"{day}|{query}".encode()).hexdigest(), 16) % len(choices)]
            _report(pick)
            return {key: value for key, value in pick.items() if key != "download_location"} | {"query": query}
    return None


@use_case
def ambient(db, lat, lon, date=None, timezone=None):
    """{date, today, place, weather, photo, photos} for a position on a local date (today by default).

    Today's weather has the sky now and the day's range; a past day's has its
    range and the code that summed it up. `photos` says whether Unsplash is set up.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    zone = timezone or db.settings.get()["timezone"] or "UTC"
    try:
        today = datetime.now(ZoneInfo(zone)).date().isoformat()
    except Exception:
        raise ValueError("unknown timezone")
    day = Date.fromisoformat(date).isoformat() if date else today
    if day > today:
        raise ValueError("date is in the future")
    lat, lon = round(lat, 2), round(lon, 2)
    place = sky = photo = None
    try:
        place = _place(db, lat, lon)
    except Exception as exc:
        _note("locality", exc)
    try:
        sky = _sky(_forecast(lat, lon, day, zone, today))
    except Exception as exc:
        _note("weather", exc)
    if unsplash.configured():
        try:
            photo = _photo(db, place, sky, day)
        except Exception as exc:
            _note("Unsplash", exc)
    return {"date": day, "today": day == today, "place": place, "weather": sky, "photo": photo,
            "photos": unsplash.configured()}
