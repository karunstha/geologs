"""Data retention: keep raw samples for a chosen number of years, and freeze the days before.

Each night (the background job runs every minute; most runs find nothing to do) the freeze line
moves up to local midnight that many years ago. Days before it are frozen: their trips are kept
as found (trips), their stays marked frozen, and their raw samples thinned to the compact
timeline. Frozen days are read from what was kept and take no edits; settings and algorithm
changes apply after the line only. The line never moves back: thinned samples are gone.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..data.database import STAY_REBUILDS
from ..domain.retention import RETENTION_YEARS, freeze_target, next_freeze_line
from .overview import daily_overview
from .paths import fill_trip
from .stays import RETENTION_KEY, anchored_trips
from .use_case import use_case

# Which analysis found a frozen trip: the stay algorithm's version, and the trips' own.
ANALYSIS_VERSION = f"stays-{max(STAY_REBUILDS)}/trips-1"


def _state(db):
    value = db.settings.value(RETENTION_KEY, {}) or {}
    years = value.get("years") if value.get("years") in RETENTION_YEARS else 0
    return {"years": years, "frozen_before": value.get("frozen_before")}


def _zone(db):
    return ZoneInfo(db.settings.get()["timezone"])


@use_case
def retention_status(db, years=None, now=None):
    """The retention setting, the freeze line, and what keeping `years` (default: the setting) would
    remove now: raw samples thinned, and the line it would move to."""
    state = _state(db)
    years = state["years"] if years is None else _years(years)
    zone = _zone(db)
    target = freeze_target(now or datetime.now(timezone.utc), years, zone)
    current = state["frozen_before"]
    first = current or db.points.span()[0]
    moves = bool(target and first and first < target)
    line = target if moves else current

    def local_day(value):  # the first day that is not frozen, in your timezone
        return datetime.fromisoformat(value).astimezone(zone).date().isoformat() if value else None
    return {"years": state["years"], "choices": list(RETENTION_YEARS), "frozen_before": current,
            "frozen_before_date": local_day(current),
            "preview": {"years": years, "freeze_line": line, "freeze_date": local_day(line), "moves": moves,
                        "samples_removed": db.points.thinnable(first, target) if moves else 0}}


def _years(value):
    try:
        years = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("years must be one of " + ", ".join(map(str, RETENTION_YEARS))) from exc
    if years not in RETENTION_YEARS:
        raise ValueError("years must be one of " + ", ".join(map(str, RETENTION_YEARS)))
    return years


@use_case
def set_retention(db, raw, now=None):
    """Keep raw samples for raw["years"] years (0: keep everything). Turning it on, or keeping fewer
    years, thins samples for good, so it needs raw["confirm"]; without it, this is a preview."""
    years = _years(raw.get("years"))
    state = _state(db)
    status = retention_status(db, years, now)
    shortens = bool(years) and (not state["years"] or years < state["years"])
    if shortens and raw.get("confirm") is not True:
        return {**status, "saved": False}
    db.settings.set_value(RETENTION_KEY, {"years": years, "frozen_before": state["frozen_before"]})
    return {**retention_status(db, None, now), "saved": True}


@use_case
def run_retention(db, now=None):
    """Move the freeze line one batch towards where it belongs (see next_freeze_line).

    In one transaction: keep the batch's trips as found (with filled routes), freeze its stays,
    thin its raw samples, then bring its stored daily summaries up to date. Returns what it did,
    or None when there was nothing to do.
    """
    state = _state(db)
    if not state["years"]:
        return None
    zone = _zone(db)
    now = now or datetime.now(timezone.utc)
    current = state["frozen_before"]
    first_sample = db.points.span()[0]
    line = next_freeze_line(current, freeze_target(now, state["years"], zone), first_sample, zone)
    if line is None:
        return None
    start = current or first_sample
    frozen_at = now.isoformat(timespec="seconds")
    kept = 0
    for trip in anchored_trips(db, start, line, lookup_places=False):
        if trip.get("frozen") or not (start <= trip["start"] < line):
            continue
        trip = fill_trip(db, trip)
        # Names come from your labels when the day is read; the rest is kept as found.
        trip = {key: value for key, value in trip.items() if key not in ("from_name", "to_name")}
        db.trips.save(trip, ANALYSIS_VERSION, frozen_at)
        kept += 1
    stays = db.stays.freeze_before(line)
    thinned = db.points.thin(start, line)
    db.stays.forget_checkpoints_before(line)
    db.settings.set_value(RETENTION_KEY, {"years": state["years"], "frozen_before": line})
    db.caches.clear_analysis()
    timezone_name = db.settings.get()["timezone"]
    first_day = datetime.fromisoformat(start).astimezone(zone).date().isoformat()
    last_day = datetime.fromisoformat(line).astimezone(zone).date().isoformat()
    for date in db.summaries.dates_between(timezone_name, first_day, last_day):
        daily_overview(db, date, timezone_name)
    return {"frozen_before": line, "trips": kept, "stays": stays, "samples_removed": thinned}
