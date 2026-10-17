"""The town or city at a position, from OpenStreetMap's Nominatim (free, no key).

Used to describe where you are and to find a photo of it; callers round the
position (about a kilometre) and cache the answer, so few lookups are made.
"""
import json
import urllib.parse
import urllib.request

URL = "https://nominatim.openstreetmap.org/reverse"


def locality(lat, lon):
    """{city, region, country, country_code} at a position; parts it cannot name are None."""
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1, "accept-language": "en"}
    request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "GeoLogs/0.1 (personal self-hosted location archive)"})
    with urllib.request.urlopen(request, timeout=10) as response:
        address = json.loads(response.read().decode()).get("address") or {}
    city = next((address[key] for key in ("city", "town", "village", "municipality", "hamlet", "county") if address.get(key)), None)
    return {"city": city, "region": address.get("state") or address.get("region"),
            "country": address.get("country"), "country_code": (address.get("country_code") or "").upper() or None}
