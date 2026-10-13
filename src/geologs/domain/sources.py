"""Combining several trackers' accounts of one day into one story."""
from datetime import datetime

from .visits import MAX_SAMPLE_GAP_SECONDS


# A higher-priority tracker's latest fix stands for where you are while it is
# this close to the newest fix from any tracker.
PRIORITY_FRESH_SECONDS = 900


def fuse_sources(day_stays, trips, samples_by_source, priority=()):
    """Tell one story when several sources recorded the same day.

    One device can sit at the university while another drives. The source you
    ranked highest in Settings (else the one with the most samples) is taken
    whole; each next source fills only the times those before it have nothing
    to say, so the timeline never places you in two spots.
    """
    rank = {name: index for index, name in enumerate(priority)}
    sources = sorted({stay["source"] for stay in day_stays} | {trip["source"] for trip in trips},
                     key=lambda name: (rank.get(name, len(rank)), -samples_by_source.get(name, 0), name))
    if len(sources) < 2:
        return day_stays, trips, sources
    taken, kept_stays, kept_trips, used = [], [], [], []
    for name in sources:
        items = ([("stay", stay, stay["started_at"], stay["ended_at"]) for stay in day_stays if stay["source"] == name]
                 + [("trip", trip, trip["start"], trip["end"]) for trip in trips if trip["source"] == name])
        accepted = []
        for kind, item, begin, finish in items:
            begin, finish = datetime.fromisoformat(begin), datetime.fromisoformat(finish)
            if not any(begin < taken_end and taken_begin < finish for taken_begin, taken_end in taken):
                accepted.append((begin, finish))
                (kept_stays if kind == "stay" else kept_trips).append(item)
        taken.extend(accepted)
        if accepted:
            used.append(name)
    kept_stays.sort(key=lambda stay: (stay["started_at"], stay["id"]))
    kept_trips.sort(key=lambda trip: trip["start"], reverse=True)
    return kept_stays, kept_trips, used


def join_source_handoffs(day_stays, trips):
    """One visit when the device recording it changed partway through.

    Switching trackers (Home Assistant's app to OwnTracks, say) ends one
    source's stay and starts another's at the same place minutes later. Stays
    from different sources at one place, no more than MAX_SAMPLE_GAP_SECONDS
    apart with no trip between them, are one visit. `day_stays` is chronological.
    """
    joined = []
    for stay in day_stays:
        previous = joined[-1] if joined else None
        if (previous is not None and previous["source"] != stay["source"]
                and previous.get("cluster_id") is not None and previous.get("cluster_id") == stay.get("cluster_id")):
            gap = (datetime.fromisoformat(stay["started_at"]) - datetime.fromisoformat(previous["ended_at"])).total_seconds()
            travelled = any(previous["ended_at"] <= trip["start"] and trip["end"] <= stay["started_at"] for trip in trips)
            if 0 <= gap <= MAX_SAMPLE_GAP_SECONDS and not travelled:
                previous.update(ended_at=stay["ended_at"], ongoing=stay["ongoing"],
                                point_count=previous["point_count"] + stay["point_count"],
                                observed_minutes=round(previous["observed_minutes"] + gap / 60 + stay["observed_minutes"], 1),
                                internal_moves=previous.get("internal_moves", []) + stay.get("internal_moves", []),
                                sources=sorted(set(previous.get("sources") or [previous["source"]]) | {stay["source"]}))
                continue
        joined.append(stay)
    return joined
