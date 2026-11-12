"""Data retention: raw samples older than a chosen age are thinned, and the days they recorded freeze.

A frozen day keeps the stays and trips found for it, as they were found. Its raw samples are
thinned to the compact timeline (movement fixes, and each stay's arrival and departure), so the
day is never analysed again: settings and algorithm changes apply after the freeze line only,
and frozen days take no edits.
"""
from datetime import datetime, time, timedelta, timezone

# How many years of raw samples to keep; 0 keeps everything (retention off).
RETENTION_YEARS = (0, 1, 2, 3, 5, 10)
# Days frozen in one run of the background job, so a first run over years of history is spread out.
FREEZE_BATCH_DAYS = 31


def years_ago(day, years):
    """The same date `years` earlier (29 February becomes the 28th)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def local_midnight(day, zone):
    """The UTC timestamp (as stored) of local midnight starting `day`."""
    return datetime.combine(day, time(), zone).astimezone(timezone.utc).isoformat(timespec="seconds")


def freeze_target(now, years, zone):
    """Where the freeze line belongs at `now`: local midnight `years` ago, or None with retention off."""
    if not years:
        return None
    return local_midnight(years_ago(now.astimezone(zone).date(), years), zone)


def next_freeze_line(current, target, first_sample, zone, batch_days=FREEZE_BATCH_DAYS):
    """The freeze line after one run: at most `batch_days` local days past the current line (or
    the first sample), never past the target. None when there is nothing to freeze."""
    if target is None or (current is not None and current >= target):
        return None
    start = current or first_sample
    if start is None or start >= target:
        return None
    first_day = datetime.fromisoformat(start).astimezone(zone).date()
    line = local_midnight(first_day + timedelta(days=batch_days), zone)
    return min(line, target)


def is_frozen(frozen_before, timestamp):
    """Whether a moment lies before the freeze line (on a frozen day)."""
    return bool(frozen_before) and timestamp is not None and str(timestamp) < frozen_before


def without_live_doubles(stays, frozen_before):
    """Stays with no live stay that a frozen one already covers.

    After a rebuild, stay detection starts again at the freeze line, so the visit that crosses it
    (a night at home) is found twice: frozen, as it was, and live, from the line on. The frozen
    one wins; a live stay before the line cannot be right either.
    """
    if not frozen_before:
        return stays
    frozen = [stay for stay in stays if stay.get("frozen")]

    def doubled(stay):
        if stay["started_at"] < frozen_before:
            return True
        return any(other["source"] == stay["source"] and other["started_at"] <= stay["ended_at"]
                   and other["ended_at"] >= stay["started_at"] for other in frozen)
    return [stay for stay in stays if stay.get("frozen") or not doubled(stay)]

