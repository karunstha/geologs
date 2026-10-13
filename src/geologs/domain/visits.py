"""Visits made of stays: joining split ones and finding unobserved time."""
from datetime import datetime


# A stay may bridge this much time without samples; longer gaps split it.
MAX_SAMPLE_GAP_SECONDS = 1800


def _gap_seconds(first, second):
    """Time from the end of one stay to the start of the next."""
    return (datetime.fromisoformat(second["started_at"]) - datetime.fromisoformat(first["ended_at"])).total_seconds()


def count_visits(entries):
    """Visits per group from (stay_id, previous_stay_id, went_elsewhere, group) entries.

    A stay continues the visit of the stay just before it when both are in
    the same group and the travel between them went nowhere else (went_elsewhere == 0).
    """
    group_of = {stay_id: group for stay_id, _previous, _left, group in entries}
    visits = {}
    for _stay_id, previous_id, went_elsewhere, group in entries:
        if not (went_elsewhere == 0 and previous_id is not None and group_of.get(previous_id) == group):
            visits[group] = visits.get(group, 0) + 1
    return visits


def merge_split_visits(rows, place_of, moves_between=None):
    """Join chronological stays at one place when the travel between them went nowhere else.

    Walking between two buildings of a campus, moving the car to another lot,
    or an indoor GPS jump does not end a visit. `rows` must hold every stay of
    each source in order, so a stay's went_elsewhere flag refers to the stay just
    before it (rows from stays() carry previous_stay_id to check that).
    `moves_between` maps (from_stay_id, to_stay_id) to the trip
    that crossed; it stays on the visit as an internal move.
    """
    merged, last_by_source = [], {}
    for stay in rows:
        previous = last_by_source.get(stay["source"])
        place = place_of(stay)
        continues = (previous is not None and place is not None and place_of(previous) == place
                     and stay.get("went_elsewhere") == 0
                     and stay.get("previous_stay_id", previous["_last_id"]) == previous["_last_id"])
        if continues:
            move = (moves_between or {}).get((previous["_last_id"], stay["id"]))
            gap_minutes = max(0.0, _gap_seconds(previous, stay)) / 60
            previous.update(ended_at=stay["ended_at"], end_observation_id=stay["end_observation_id"],
                            ongoing=stay["ongoing"], _last_id=stay["id"],
                            point_count=previous["point_count"] + stay["point_count"],
                            observed_minutes=round(previous["observed_minutes"] + gap_minutes + stay["observed_minutes"], 1),
                            internal_moves=previous["internal_moves"] + ([move] if move else []))
            if stay["kind"] == "area":
                previous.update(kind="area", activity=previous.get("activity") or stay.get("activity"),
                                distance_m=(previous.get("distance_m") or 0) + (stay.get("distance_m") or 0),
                                radius_m=max(previous.get("radius_m") or 0, stay.get("radius_m") or 0))
            continue
        item = {**stay, "_last_id": stay["id"], "internal_moves": []}
        merged.append(item)
        last_by_source[stay["source"]] = item
    for item in merged:
        item.pop("_last_id")
    return merged


def coverage_gaps(points, trips, day_stays=()):
    """Unobserved stretches over 30 minutes, less what a trip that already notes
    them or a stay covers (an app that reports only while moving is quiet while you stay)."""
    times = sorted({datetime.fromisoformat(point["timestamp"]) for point in points})
    spans = [(datetime.fromisoformat(trip["start"]), datetime.fromisoformat(trip["end"])) for trip in trips]
    spans += [(datetime.fromisoformat(stay["started_at"]), datetime.fromisoformat(stay["ended_at"])) for stay in day_stays]
    spans.sort()
    gaps = []
    for earlier, later in zip(times, times[1:]):
        if (later - earlier).total_seconds() <= MAX_SAMPLE_GAP_SECONDS:
            continue
        # Only the parts no stay or trip covers are unknown: a stay that ran until
        # you left leaves just the minute or so it took to reach the next fix.
        uncovered, reached = [], earlier
        for start, end in spans:
            if end <= reached or start >= later:
                continue
            if start > reached:
                uncovered.append((reached, start))
            reached = end
        if reached < later:
            uncovered.append((reached, later))
        gaps += [{"start": start.isoformat(), "end": end.isoformat(),
                  "minutes": round((end - start).total_seconds() / 60, 1)}
                 for start, end in uncovered if (end - start).total_seconds() > MAX_SAMPLE_GAP_SECONDS]
    return gaps
