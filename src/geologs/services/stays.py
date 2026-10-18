"""Finding stays as samples arrive, and reading them back with your corrections applied."""
from datetime import datetime, timedelta

from . import places as naming
from ..domain.areas import area_windows, returns_to_origin, went_elsewhere_flag
from ..domain.corrections import apply_stay_edits, mode_override_for
from ..domain.places import is_home_place, label_at, POSSIBLE_STOP_PLACE_M, zone_at
from ..domain.stay_detection import accurate_enough, advance, candidate_duration, min_stay_seconds
from ..domain.timing import parse_time
from ..domain.trips import trips_between_stays, went_elsewhere_between
from .use_case import use_case


def sync_timeline(db, sources=None, rebuild=False, since_ids=None, from_times=None):
    """Materialize an efficient timeline from raw observations and confirmed stays.

    Samples while travelling remain available. A confirmed stationary run keeps
    only its arrival and latest/departure observations in the timeline; raw
    observations remain untouched in point_observations.
    """
    if sources is None:
        sources = db.points.sources()
    since_ids, from_times = since_ids or {}, from_times or {}
    stay_seconds = min_stay_seconds(db.settings.get())
    for source in sources:
        state = db.stays.state(source)
        candidate = state["candidate"] if state else None
        # A stop that is still open but not yet long enough to be a stay is compacted too.
        open_stop = candidate if candidate and candidate["count"] >= 2 and candidate_duration(candidate) < stay_seconds else None
        db.timeline.sync(source, 0 if rebuild else since_ids.get(source, 0), rebuild, open_stop, from_times.get(source))


class _DetectedStays:
    """Where stay detection keeps what it finds: one source's stays in the database."""

    def __init__(self, db, source):
        self.db, self.source = db, source

    def keep(self, record):
        self.db.stays.save_detected(self.source, record["start_id"], record["end_id"], record["started_at"],
                                    record["ended_at"], record["lat"], record["lon"], record["count"],
                                    record["ongoing"], record["kind"])

    def withdraw(self, start_id):
        self.db.stays.withdraw(start_id)


def _refresh_area_stays(db, source, since=None, rules=None):
    """Detect area stays in travel after `since` (all history when None).

    Travel intervals lie before the first stationary stay, between stationary
    stays, and after the last one; stationary fixes are never part of an area.
    Every stay also records went_elsewhere: whether the travel since the stay
    before it detoured somewhere off the direct path (1), went straight
    between the two (0), or is unknown because of a coverage gap (NULL).
    """
    anchors = db.stays.stationary(source, since)
    # The stay that ended at `since` (of either kind) precedes the recomputed travel.
    # Quick stops are not anchors here, as in a rebuild, so live and rebuilt flags agree.
    head = db.stays.last_anchor_before(source, since) if since is not None else None
    intervals = [(head, anchors[0] if anchors else None)] + list(zip(anchors, anchors[1:]))
    if anchors:
        intervals.append((anchors[-1], None))
    for previous, following in intervals:
        after = previous["ended_at"] if previous else since
        before = following["started_at"] if following else None
        if after and before and before <= after:
            continue
        points = db.points.best_fixes_between(source, after, before)
        windows = []
        long_enough = not (after and before) or (datetime.fromisoformat(before) - datetime.fromisoformat(after)).total_seconds() >= min_stay_seconds(rules)
        if long_enough:
            last_id = next((point["id"] for point in reversed(points) if accurate_enough(point)), None)
            for window in area_windows(points, rules):
                if not returns_to_origin(window, previous, following):
                    window["ongoing"] = following is None and window["end_id"] == last_id
                    windows.append(window)
        sequence = ([previous] if previous else []) + windows + ([following] if following else [])
        flags = {}
        for earlier, later in zip(sequence, sequence[1:]):
            between = [point for point in points if earlier["ended_at"] < point["timestamp"] < later["started_at"]]
            flags[id(later)] = went_elsewhere_flag(went_elsewhere_between(between, earlier, later))
        for window in windows:
            # A quick stop inside an area (a pause on a lake loop) is part of that visit.
            db.stays.delete_quick_stops_within(source, window["started_at"], window["ended_at"])
            db.stays.add_area(source, window, flags.get(id(window)))
        if following:
            db.stays.set_went_elsewhere(following["id"], flags.get(id(following)))


# How often the stay candidate is saved as fixes go by, and how long every one is kept: a fix that
# arrives late resumes from the checkpoint before it rather than finding every stay again.
CHECKPOINT_EVERY = timedelta(hours=1)
CHECKPOINTS_KEPT_HOURLY = timedelta(days=7)


def _after(timestamp, step):
    return (datetime.fromisoformat(timestamp) + step).isoformat()


def process_stays(db, sources=None, rebuild=False):
    """Find stays in a source's new fixes, picking up where the last call left off.

    A fix older than ones already read reshapes what came after it. Detection then resumes from
    the newest checkpoint before that fix, the same work as reading every fix in time order, but
    from there on only. Without such a checkpoint (or with `rebuild`), the source starts over.
    """
    if rebuild:
        db.caches.clear_analysis()
    rules = db.settings.get()
    if sources is None:
        sources = db.points.sources()
    processed = 0
    processed_sources = set()
    rebuilt_sources = set()
    since_ids, from_times = {}, {}
    for source in sources:
        state = db.stays.state(source)
        last_id = state["last_observation_id"] if state else 0
        since_ids[source] = 0 if rebuild else last_id
        first_new = db.points.first_new(source, last_id)
        late = bool(state and first_new and first_new <= state["last_timestamp"])
        resume = db.stays.checkpoint_before(source, first_new) if late and not rebuild else None
        area_since = None
        if rebuild or (late and resume is None):
            rebuilt_sources.add(source)
            db.stays.delete_source(source)
            state = None
            last_id = 0
        elif resume is not None:
            # Forget what was found after the checkpoint and go on from it. Its open candidate began
            # earlier, so the timeline and area stays are made again from where that began.
            db.stays.rewind(source, resume["last_timestamp"])
            state, last_id = resume, resume["last_observation_id"]
            begun = (resume["candidate"] or {}).get("start") or resume["last_timestamp"]
            from_times[source] = min(begun, resume["last_timestamp"])
            area_since = db.stays.last_end(source, "stationary", before=from_times[source])
            if area_since is None:
                area_since = db.stays.last_end(source, "area", closed=True, before=from_times[source])
            db.stays.delete_areas(source, area_since)
        elif first_new:
            # New fixes can reshape area stays after the last settled stationary
            # stay. Remove those before stationary stays grow, so the two kinds
            # never claim the same starting fix.
            area_since = db.stays.last_end(source, "stationary", before=first_new)
            if area_since is None:
                # A source without stationary stays (a tracker that reports only
                # while moving) resumes after its last closed area stay rather
                # than rescanning its whole history on every fix.
                area_since = db.stays.last_end(source, "area", closed=True)
            db.stays.delete_areas(source, area_since)
        source_max_id = db.points.max_id(source, last_id)
        candidate = state["candidate"] if state else None
        if candidate:
            candidate.pop("nearby_names", None)  # kept beside the candidate since the name lookups moved out
        max_id, last_time = source_max_id, state["last_timestamp"] if state else None
        detected = _DetectedStays(db, source)
        newest_checkpoint = None if state is None else (resume["last_timestamp"] if resume else db.stays.last_checkpoint(source))
        next_checkpoint = _after(newest_checkpoint, CHECKPOINT_EVERY) if newest_checkpoint else None
        wrote_checkpoint = False
        fixes = db.points.best_fixes_after_time(source, resume["last_timestamp"]) if resume else \
            db.points.best_fixes_after(source, last_id)
        for batch in fixes:
            for point in batch:
                processed += 1
                last_time = point["timestamp"]
                # A low-accuracy fix neither confirms nor ends a stay.
                if accurate_enough(point):
                    candidate = advance(candidate, point, rules, detected)
                if next_checkpoint is None or last_time >= next_checkpoint:
                    db.stays.save_checkpoint(source, point["id"], last_time, candidate)
                    next_checkpoint = _after(last_time, CHECKPOINT_EVERY)
                    newest_checkpoint, wrote_checkpoint = last_time, True
        if max_id == last_id:
            continue
        processed_sources.add(source)
        db.stays.save_state(source, max_id, last_time, candidate)
        if wrote_checkpoint:
            db.stays.thin_checkpoints(source, _after(newest_checkpoint, -CHECKPOINTS_KEPT_HOURLY))
        _refresh_area_stays(db, source, area_since, rules)
    incremental_sources = processed_sources - rebuilt_sources - set(from_times)
    if incremental_sources:
        sync_timeline(db, incremental_sources, since_ids=since_ids)
    rewound_sources = (processed_sources - rebuilt_sources) & set(from_times)
    if rewound_sources:
        sync_timeline(db, rewound_sources, from_times=from_times)
    if rebuilt_sources:
        sync_timeline(db, rebuilt_sources, rebuild=True)
    return {"processed_points": processed}


def _confirm_possible_stops(db, rows, places, labels, lookup, rejected=None):
    """Keep a sparse 'pause' only where there is a place to have stopped at.

    A saved place, a name you gave the spot, or a named place within
    POSSIBLE_STOP_PLACE_M makes it a quick stop; otherwise it was probably the
    road or a red light, and it goes to `rejected` (when given) so the day view
    can offer to keep it. lookup=False reads only the lookup cache.
    """
    kept = []
    for row in rows:
        if row["kind"] == "pause":
            place = (label_at(labels, row["lat"], row["lon"], row["source"], row["started_at"], row["ended_at"])
                     or zone_at(places, row["lat"], row["lon"])
                     or naming.named_place_near(db, row["lat"], row["lon"], POSSIBLE_STOP_PLACE_M, lookup))
            if not place:
                if rejected is not None:
                    rejected.append(row)
                continue
            row = {**row, "kind": "stop", "confirmed_by_place": True}
        kept.append(row)
    return kept


@use_case
def stays(db, start=None, end=None, source=None, limit=500, lookup_places=False, rejected=None):
    """Stays overlapping a window, newest first, as you corrected them, named where a label or Home names them.

    lookup_places confirms sparse quick stops by asking the place provider
    (else its cache only); stops nothing confirms go to `rejected`.
    """
    rows = db.stays.between(start, end, source, limit)
    places, labels, overrides = db.places.saved(), db.places.labels(), db.corrections.mode_overrides()
    rows = _confirm_possible_stops(db, rows, places, labels, lookup_places, rejected)
    rows = apply_stay_edits(rows, db.corrections.stay_edits(start, end, source))
    if limit is not None:
        rows = rows[:min(max(int(limit), 1), 5000)]
    for row in rows:
        row["observed_minutes"] = round((datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds() / 60, 1)
        # A saved place is a zone around the stay, not its name: a university
        # holds many buildings. Only Home names what is inside it, and a name you
        # pinned to this spot wins over that too.
        label = label_at(labels, row["lat"], row["lon"], row["source"], row["started_at"], row["ended_at"])
        zone = zone_at(places, row["lat"], row["lon"])
        home = zone if zone and not label and is_home_place(zone) else None
        row["label_id"] = label["id"] if label else None
        row["zone_id"], row["zone_name"] = (zone["id"], zone["name"]) if zone else (None, None)
        row["place_id"] = home["id"] if home else None
        row["place_name"] = label["name"] if label else home["name"] if home else None
        if row.get("kind") == "area":
            override = mode_override_for(overrides, row["source"], "stay", row["started_at"], row["ended_at"])
            if override:
                row.update(detected_activity=row.get("activity"), activity=override["mode"], activity_corrected=True)
    return rows


@use_case
def anchored_trips(db, start=None, end=None, source=None, lookup_places=True):
    """Return travel intervals bounded by consecutive stays (of the length set in Settings).

    lookup_places=False confirms quick stops from the lookup cache only, for history sweeps.
    """
    context_start = (datetime.fromisoformat(parse_time(start)) - timedelta(days=1)).isoformat() if start else None
    context_end = (datetime.fromisoformat(parse_time(end)) + timedelta(days=1)).isoformat() if end else None
    anchors = stays(db, context_start, context_end, source=source, limit=None, lookup_places=lookup_places)
    points = list(reversed(db.points.observations(context_start, context_end, source=source, limit=None)))
    trips = trips_between_stays(points, anchors, start, end, db.settings.get())
    overrides = db.corrections.mode_overrides()
    for trip in trips:
        override = mode_override_for(overrides, trip["source"], "trip", trip["start"], trip["end"])
        if override:
            # Your correction beats the speed-based guess (kept as detected_mode).
            trip.update(detected_mode=trip["mode"], mode=override["mode"], mode_corrected=True,
                        modes={override["mode"]: sum(trip["modes"].values())})
    return trips


@use_case
def rebuild_stays(db):
    """Find every stay again from the raw samples (after an import or an algorithm change)."""
    return process_stays(db, rebuild=True)

