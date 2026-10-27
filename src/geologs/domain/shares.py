"""What a shared replay shows: a day's replay (see services.replay) with what you chose to keep private taken out."""
import secrets
from datetime import datetime, timedelta

from .geo import metres
from .motion import MOVEMENT_MODES

# How far around the day's start and end, and around Home, nothing is shown (0: all of it).
HIDE_CHOICES_M = (0, 200, 500, 1000)
DEFAULT_HIDE_M = 500
EXPIRY_CHOICES_DAYS = (1, 7, 30, None)
DEFAULT_EXPIRY_DAYS = 7
TITLE_MAX = 80


def share_token():
    """A link's secret part: 128 random bits, short enough to read out of a URL."""
    return secrets.token_urlsafe(16)


def share_options(payload):
    """The choices for a new link, checked, with the private choice wherever one is left out:
    `hide_m` around the start, end and Home, `times` ("clock" or "elapsed"), `names` (show the
    places' names or not), `expires_days` and `title`."""
    hide = payload.get("hide_m", DEFAULT_HIDE_M)
    if hide not in HIDE_CHOICES_M:
        raise ValueError(f"hide_m must be one of {', '.join(str(choice) for choice in HIDE_CHOICES_M)}")
    times = payload.get("times", "elapsed")
    if times not in ("clock", "elapsed"):
        raise ValueError("times must be clock or elapsed")
    expires = payload.get("expires_days", DEFAULT_EXPIRY_DAYS)
    if expires not in EXPIRY_CHOICES_DAYS:
        raise ValueError("expires_days must be 1, 7, 30 or null (never)")
    title = " ".join(str(payload.get("title") or "").split())
    if len(title) > TITLE_MAX:
        raise ValueError(f"title is at most {TITLE_MAX} characters")
    return {"hide_m": hide, "times": times, "names": payload.get("names", True) is not False,
            "expires_days": expires, "title": title}


def expiry(now, days):
    """When a link made at ISO time `now` stops working, or None for never."""
    return None if days is None else (datetime.fromisoformat(now) + timedelta(days=days)).isoformat()


def _outside(point, zones, radius):
    return all(metres(lat, lon, point[2], point[1]) > radius for lat, lon in zones)


def _length(track):
    return sum(metres(a[2], a[1], b[2], b[1]) for a, b in zip(track, track[1:]))


def shared_replay(replay, options, units="metric"):
    """The replay a link shows. Within hide_m of the day's first and last positions and of each
    stay at Home, the ways are cut and the stops left out, so a viewer cannot follow the day to
    your door; a way cut in the middle becomes two. Times are kept as the day's clock, or start
    from 0 at the first way shown; place names are kept or dropped. Nothing else of the archive
    goes with it: no tracker, no timezone (without the clock), no date (but in the title)."""
    hide = options["hide_m"]
    moves = [move for move in replay["moves"] if move["track"]]
    zones = []
    if hide and moves:
        first, last = moves[0]["track"][0], moves[-1]["track"][-1]
        zones = [(first[2], first[1]), (last[2], last[1])]
        zones += [(stay["lat"], stay["lon"]) for stay in replay["stays"] if stay.get("home")]
    kept = []
    for move in moves:
        whole = _length(move["track"]) or 1
        runs, run = [], []
        for point in move["track"]:
            if not zones or _outside(point, zones, hide):
                run.append(point)
            elif run:
                runs.append(run)
                run = []
        runs.append(run)
        for run in (run for run in runs if len(run) > 1):
            share = _length(run) / whole if len(run) < len(move["track"]) else 1
            kept.append({**move, "start": run[0][0], "end": run[-1][0], "track": run,
                         "distance_km": round((move.get("distance_km") or 0) * share, 2)})
    stays = [stay for stay in replay["stays"] if not zones or _outside([0, stay["lon"], stay["lat"]], zones, hide)]
    names = options["names"]
    shift = 0 if options["times"] == "clock" else (kept[0]["start"] if kept else 0)

    def at(seconds):
        return round(seconds - shift, 1)

    shown_moves = [{"kind": move["kind"], "mode": move["mode"], "start": at(move["start"]), "end": at(move["end"]),
                    "from_name": move.get("from_name") if names else None,
                    "to_name": move.get("to_name") if names else None,
                    "distance_km": move["distance_km"], "recorded": move.get("recorded", True),
                    "track": [[at(point[0]), point[1], point[2]] for point in move["track"]]} for move in kept]
    shown_stays = [{"name": stay["name"] if names else "", "lat": stay["lat"], "lon": stay["lon"],
                    "kind": stay["kind"], "activity": stay.get("activity"),
                    "start": at(stay["start"]), "end": at(stay["end"])} for stay in stays]
    counted = [move for move in kept if move["mode"] in MOVEMENT_MODES]
    result = {"moves": shown_moves, "stays": shown_stays, "units": units, "elapsed": options["times"] == "elapsed",
              "distance_km": round(sum(move["distance_km"] for move in counted), 2),
              "moving_minutes": round(sum(move["end"] - move["start"] for move in counted) / 60, 1)}
    if options["times"] == "clock":
        result.update(start=replay["start"], timezone=replay["timezone"])
    return result
