"""Your samples: reading them, measuring them, and deleting them (with everything derived from them)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..domain.trips import stats_from_points
from .overview import daily_overview
from .paths import learn_path_history
from .place_routes import rebuild_routes, regroup
from .stays import process_stays
from .use_case import use_case


# One delete request covers at most a page of raw samples.
MAX_SAMPLE_DELETE = 500
# Place names are looked up at stays and places, which lie near the samples
# they came from: a stop's centre within metres, an area stay's within its
# extent. Lookups this close to a deleted sample go with it.
FORGET_LOOKUPS_WITHIN_M = 1000


@use_case
def delete_samples(db, ids, confirm=False):
    """Preview, or with confirm=True permanently delete, raw samples.

    The affected sources' stays are rebuilt and the stored summaries of the
    affected days refreshed, so history never refers to a deleted sample.
    """
    plan = deletion_plan(db, ids)
    timezone_name = db.settings.get()["timezone"]
    zone = ZoneInfo(timezone_name)
    dates = sorted({datetime.fromisoformat(value).astimezone(zone).date().isoformat() for value in plan["timestamps"]})
    preview = {"samples": plan["samples"], "sources": plan["sources"], "first": plan["first"], "last": plan["last"],
               "dates": dates, "reminder_triggers": [{"reminder": item["reminder"], "triggered_at": item["triggered_at"]}
                                                     for item in plan["reminder_triggers"]]}
    if not confirm:
        return {**preview, "deleted": False}
    # Stays and timelines refer to raw samples: they go, and are found again
    # below. Points no other sample records go with the reminder events they fired.
    db.timeline.delete_sources(plan["sources"])
    for source in plan["sources"]:
        db.stays.delete_source(source)
    db.points.delete_observations(plan["ids"])
    if plan["points"]:
        db.reminders.delete_events_of_points(plan["points"])
        db.points.delete_points(plan["points"])
    # Nothing derived from them may outlive the samples: routes learned from
    # trips under way at the time, and place lookups made around them.
    forgotten = []
    for fix in plan["fixes"]:
        db.paths.delete_through(fix["source"], fix["timestamp"])
        forgotten += [(track["from"], track["to"]) for track in db.place_routes.delete_tracks_through(fix["source"], fix["timestamp"])]
        db.caches.forget_reverse_regions_near(fix["lat"], fix["lon"], FORGET_LOOKUPS_WITHIN_M)
    process_stays(db, plan["sources"], rebuild=True)
    # Trips around the deleted samples changed: learn their paths again from what is left.
    span = (datetime.fromisoformat(plan["first"]) - timedelta(days=1)).isoformat(), \
        (datetime.fromisoformat(plan["last"]) + timedelta(days=1)).isoformat()
    for source in plan["sources"]:
        learn_path_history(db, source=source, start=span[0], end=span[1])
    if forgotten:
        regroup(db, forgotten)  # routes those trips took, even when nothing took their place
    for date in dates:
        # Rebuild (or drop, when nothing is left) each stored record for those days.
        if db.summaries.get(date, timezone_name) is not None:
            daily_overview(db, date, timezone_name)
    return {**preview, "deleted": True}


@use_case
def delete_source(db, source, confirm=False):
    """Preview, or with confirm=True permanently remove, a tracker and all its samples.

    Stored summaries of the days it recorded are rebuilt (or dropped when
    nothing is left), as for deleted samples.
    """
    plan = source_deletion(db, str(source or "").strip())
    timezone_name = db.settings.get()["timezone"]
    zone = ZoneInfo(timezone_name)
    first = datetime.fromisoformat(plan["first"]).astimezone(zone).date()
    last = datetime.fromisoformat(plan["last"]).astimezone(zone).date()
    dates = db.summaries.dates_between(timezone_name, first.isoformat(), last.isoformat())
    preview = {**plan, "days": (last - first).days + 1, "stored_summaries": len(dates)}
    if not confirm:
        return {**preview, "deleted": False}
    remove_source(db, plan["source"])
    for date in dates:
        daily_overview(db, date, timezone_name)
    return {**preview, "deleted": True}


@use_case
def deletion_plan(db, ids):
    """What deleting these raw samples (point_observations ids) would remove.

    A canonical point goes too once none of its samples are left, and with
    it any reminder trigger it caused.
    """
    try:
        ids = sorted({int(value) for value in ids})
    except (TypeError, ValueError) as exc:
        raise ValueError("ids must be a list of sample ids") from exc
    if not 1 <= len(ids) <= MAX_SAMPLE_DELETE:
        raise ValueError(f"choose between 1 and {MAX_SAMPLE_DELETE} samples")
    plan = db.points.deletion_plan(ids)
    if plan is None:
        raise ValueError("those samples no longer exist")
    return {**plan, "reminder_triggers": db.reminders.triggers_of_points(plan["points"])}


@use_case
def source_deletion(db, source):
    """What removing a tracker would remove: all its samples, and the reminder triggers they caused."""
    plan = db.points.source_summary(source)
    if plan is None:
        raise ValueError("that tracker has no samples")
    return {**plan, "reminder_triggers": db.reminders.triggers_of_source(source)}


@use_case
def remove_source(db, source):
    """Remove a tracker and everything derived from it.

    Cached reverse lookups do not say which tracker's stay they named, so all
    of them go; names are looked up again as days are viewed.
    """
    # In this order: timelines and stays refer to samples, events to points.
    db.timeline.delete_sources([source])
    db.stays.delete_source(source)
    db.corrections.delete_source(source)
    db.places.delete_visit_labels(source)
    db.reminders.delete_events_of_source(source)
    db.points.delete_source(source)
    db.paths.delete_source(source)
    db.place_routes.delete_source(source)
    rebuild_routes(db)
    db.caches.forget_reverse_regions()
    priority = db.settings.get()["source_priority"]
    if source in priority:
        db.settings.set_value("source_priority", [name for name in priority if name != source])
    db.caches.clear_analysis()


@use_case
def stats(db, start=None, end=None, source=None):
    raw = list(reversed(db.points.observations(start, end, source=source, limit=None)))
    selected = list(reversed(db.points.timeline(start, end, source=source, limit=None)))
    result = stats_from_points(selected)
    result["points"] = len(raw)
    result["unique_points"] = len(selected)
    result["timeline_points"] = len(selected)
    return result


@use_case
def near(db, lat, lon, radius_m=250, start=None, end=None, limit=100, source=None):
    """Points within radius_m of a position, newest first."""
    lat, lon, radius_m = float(lat), float(lon), float(radius_m)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 1 <= radius_m <= 100000):
        raise ValueError("invalid coordinates or radius")
    return db.points.near(lat, lon, radius_m, start, end, limit, source)


@use_case
def export_geojson(db, start=None, end=None):
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": {k: v for k, v in p.items() if k not in ("lat", "lon")}} for p in db.points.observations(start, end, limit=None)]}
