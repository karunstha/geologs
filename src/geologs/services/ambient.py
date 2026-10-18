"""The setting for a day on History: the town you were in, its weather, and a photo of it in that weather.

Each part is looked up on its own and left out when it cannot be had, so a
weather outage never hides the photo, nor a missing Unsplash key the weather.
Positions are rounded to about a kilometre before anything is asked of an
outside service.
"""
import random
import threading
import time
from collections import deque
from datetime import date as Date, datetime, timedelta, timezone as UTC
from zoneinfo import ZoneInfo

from ..domain.weather import condition, time_of_day
from ..integrations import locality as localities
from ..integrations import unsplash
from ..integrations import weather as forecasts
from .use_case import use_case

# Weather is kept in memory: ten minutes for today, six hours for a past day.
_weather = {}
_weather_lock = threading.Lock()
# Photos already reported to Unsplash by this server, so each is reported once.
_reported = set()
# The last photos shown, which the next pick avoids so the backdrop does not repeat itself.
_recent = deque(maxlen=12)
PHOTO_CHOICES = 10
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


def _photo_searches(place, sky, moment):
    """Most to least particular: "Little Rock Arkansas rain night", "Little Rock rain night",
    "Little Rock rain", "Little Rock night", "Little Rock", "Arkansas rain", …"""
    city, region = place.get("city") if place else None, place.get("region") if place else None
    if region and city and region.lower() == city.lower():
        region = None
    word = sky["photo_word"] if sky else None
    join = lambda *parts: " ".join(parts) if all(parts) else None
    candidates = []
    if city:
        candidates += [join(city, region, word, moment), join(city, word, moment), join(city, word),
                       join(city, moment), city]
    if region:
        candidates += [join(region, word, moment), join(region, word), region]
    if word:
        candidates += [join(word, moment, "landscape"), join(word, "landscape")]
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
    photos = unsplash.search(query, PHOTO_CHOICES)
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


def _photo(db, place, sky, moment):
    """A random one of the top ten photos for the first search that finds any, avoiding those just shown."""
    for query in _photo_searches(place, sky, moment):
        photos = _photos(db, query)
        if photos:
            choices = photos[:PHOTO_CHOICES]
            pick = random.choice([photo for photo in choices if photo["id"] not in _recent] or choices)
            _recent.append(pick["id"])
            _report(pick)
            return {key: value for key, value in pick.items() if key != "download_location"} | {"query": query}
    return None


def place_of(db, lat, lon):
    """The town at a position (rounded to about a kilometre), from cache or Nominatim; None if unknown."""
    return _place(db, round(float(lat), 2), round(float(lon), 2))


def city_photo(db, place, seed):
    """A photo of a town for a summary: the same one each time for the same seed, unlike the backdrop's."""
    if not unsplash.configured() or not place:
        return None
    for query in _photo_searches(place, None, None):
        photos = _photos(db, query)[:PHOTO_CHOICES]
        if photos:
            pick = photos[sum(map(ord, seed)) % len(photos)]
            _report(pick)
            return {key: value for key, value in pick.items() if key != "download_location"} | {"query": query}
    return None


def _local_now(zone):
    return datetime.now(ZoneInfo(zone))


@use_case
def ambient(db, lat, lon, date=None, timezone=None):
    """{date, today, time_of_day, place, weather, photo, photos} for a position on a local date (today by default).

    Today's weather has the sky now and the day's range; a past day's has its
    range and the code that summed it up. `photos` says whether Unsplash is set up.
    """
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    zone = timezone or db.settings.get()["timezone"] or "UTC"
    try:
        now = _local_now(zone)
    except Exception:
        raise ValueError("unknown timezone")
    today = now.date().isoformat()
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
    # Today's photo is of the town at this time of day; a past day's, of the town all day.
    moment = None
    if day == today:
        sun = sky["day"] if sky else {}
        moment = time_of_day(now.strftime("%H:%M"), sun.get("sunrise"), sun.get("sunset"))
    if unsplash.configured():
        try:
            photo = _photo(db, place, sky, moment)
        except Exception as exc:
            _note("Unsplash", exc)
    return {"date": day, "today": day == today, "time_of_day": moment, "place": place, "weather": sky,
            "photo": photo, "photos": unsplash.configured()}
