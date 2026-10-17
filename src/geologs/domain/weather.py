"""What the sky is doing, from a WMO weather code: a label, an icon, and words to find a photo by."""

# WMO code → (condition, label). Open-Meteo reports these codes.
_CODES = {
    0: ("clear", "Clear"), 1: ("clear", "Mostly clear"), 2: ("partly", "Partly cloudy"), 3: ("cloudy", "Overcast"),
    45: ("fog", "Fog"), 48: ("fog", "Freezing fog"),
    51: ("drizzle", "Light drizzle"), 53: ("drizzle", "Drizzle"), 55: ("drizzle", "Heavy drizzle"),
    56: ("drizzle", "Freezing drizzle"), 57: ("drizzle", "Freezing drizzle"),
    61: ("rain", "Light rain"), 63: ("rain", "Rain"), 65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"), 67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"), 73: ("snow", "Snow"), 75: ("snow", "Heavy snow"), 77: ("snow", "Snow grains"),
    80: ("rain", "Light showers"), 81: ("rain", "Showers"), 82: ("rain", "Heavy showers"),
    85: ("snow", "Snow showers"), 86: ("snow", "Heavy snow showers"),
    95: ("storm", "Thunderstorm"), 96: ("storm", "Thunderstorm with hail"), 99: ("storm", "Thunderstorm with hail"),
}

# What to add to a city's name when looking for a photo in this weather.
_PHOTO_WORDS = {"clear": "skyline", "partly": "skyline", "cloudy": "cloudy", "fog": "fog", "drizzle": "rain",
                "rain": "rain", "snow": "snow", "storm": "storm"}


def condition(code, is_day=True):
    """{condition, label, icon, photo_word} for a WMO code; an unknown code reads as cloudy.

    The icon is the condition, with "-night" for clear and partly cloudy skies after dark.
    """
    kind, label = _CODES.get(int(code) if code is not None else -1, ("cloudy", "Cloudy"))
    night = not is_day and kind in ("clear", "partly")
    return {"condition": kind, "label": label, "icon": kind + ("-night" if night else ""),
            "photo_word": _PHOTO_WORDS[kind]}


def _minutes(clock):
    hours, minutes = str(clock)[-5:].split(":")
    return int(hours) * 60 + int(minutes)


def time_of_day(now, sunrise=None, sunset=None):
    """"morning", "day", "evening" or "night" at a local time ("HH:MM", or ISO with the time last).

    Morning runs from sunrise to 11:00, evening from 90 minutes before sunset to
    45 minutes after it; without sun times, 06:30 and 19:00 stand in.
    """
    now, rise, set_ = _minutes(now), _minutes(sunrise or "06:30"), _minutes(sunset or "19:00")
    if now < rise or now >= set_ + 45:
        return "night"
    if now < max(rise + 60, 11 * 60):
        return "morning"
    if now < set_ - 90:
        return "day"
    return "evening"
