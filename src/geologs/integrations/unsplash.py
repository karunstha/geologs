"""Photos from Unsplash (UNSPLASH_ACCESS_KEY), shown the way its guidelines ask.

Images are hotlinked from Unsplash, credited to the photographer with links
back, and each photo shown is reported to its download endpoint.
"""
import json
import urllib.parse
import urllib.request

from .. import config

API = "https://api.unsplash.com"
# Unsplash asks that links back carry the app's name.
REFERRAL = "utm_source=geologs&utm_medium=referral"


def configured():
    return bool(config.unsplash_key())


def _get(path, params):
    request = urllib.request.Request(API + path + "?" + urllib.parse.urlencode(params), headers={
        "Authorization": "Client-ID " + config.unsplash_key(), "Accept-Version": "v1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def _referred(url):
    return url + ("&" if "?" in url else "?") + REFERRAL if url else None


def search(query, count=12):
    """Landscape photos for a query, best first, in the shape the page shows."""
    data = _get("/search/photos", {"query": query, "orientation": "landscape", "content_filter": "high",
                                   "per_page": count, "order_by": "relevant"})
    photos = []
    for item in data.get("results") or []:
        urls, user, links = item.get("urls") or {}, item.get("user") or {}, item.get("links") or {}
        if not urls.get("raw"):
            continue
        photos.append({
            "id": item.get("id"),
            "url": urls["raw"] + "&w=2400&q=75&auto=format&fit=max",
            "thumb": urls.get("small"),
            "color": item.get("color"),
            "description": item.get("alt_description") or item.get("description"),
            "photographer": user.get("name") or user.get("username") or "Unknown",
            "photographer_url": _referred((user.get("links") or {}).get("html")),
            "link": _referred(links.get("html")),
            "download_location": links.get("download_location"),
        })
    return photos


def report_download(download_location):
    """Tell Unsplash a photo was shown (its guidelines count this as a download)."""
    parsed = urllib.parse.urlparse(download_location)
    if parsed.scheme == "https" and parsed.netloc == "api.unsplash.com":
        _get(parsed.path, dict(urllib.parse.parse_qsl(parsed.query)))
