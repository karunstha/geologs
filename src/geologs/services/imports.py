"""Importing history in bulk: CSV files, Google Takeout's Records.json, and Google Timeline exports."""
import csv
import io

from ..integrations.google_takeout import iter_takeout_locations, iter_timeline_fixes, timeline_kind
from .ingest import ingest
from .paths import learn_path_history
from .stays import process_stays
from .use_case import use_case

# Where each kind of file's fixes go unless the person names a tracker.
DEFAULT_SOURCES = {"csv": "csv", "takeout": "google_takeout", "timeline": "google_timeline"}


def _import_fixes(db, fixes, source, totals, progress=None):
    """Record fixes in batches of 1000, keeping each batch as it is read (an import can hold millions
    of fixes), then rebuild the source's stays and paths once. Historical imports never fire
    reminders or ask a geocoder."""
    batch = []

    def save():
        result = ingest(db, batch, trigger_reminders=False, process_analysis=False)
        totals["inserted"] += result["inserted"]
        totals["duplicates"] += result["duplicates"]
        db.commit()
        batch.clear()
        if progress:
            progress(dict(totals, phase="reading"))

    for fix in fixes:
        batch.append({**fix, "source": source})
        if len(batch) == 1000:
            save()
    if batch:
        save()
    if totals["inserted"]:
        if progress:
            progress(dict(totals, phase="rebuilding"))
        process_stays(db, {source}, rebuild=True)
        learn_path_history(db, source=source)
    return totals


def _csv_fixes(reader):
    for row in reader:
        for optional in ("accuracy_m", "speed_mps", "external_id"):
            if row.get(optional) == "":
                row.pop(optional)
        row.pop("source", None)  # an exported file's column: the import names its own tracker
        yield row


@use_case
def import_csv(db, content, source="csv", progress=None):
    """Import CSV with timestamp, lat, lon and optional accuracy_m, speed_mps, external_id columns."""
    reader = csv.DictReader(io.StringIO(content) if isinstance(content, str) else content)
    return _import_fixes(db, _csv_fixes(reader), source, {"inserted": 0, "duplicates": 0, "events": []}, progress)


def _takeout_fixes(stream, totals):
    for record in iter_takeout_locations(stream):
        if "latitudeE7" not in record or "longitudeE7" not in record:
            totals["skipped"] += 1
            continue
        timestamp = record.get("timestamp")
        if not timestamp and record.get("timestampMs"):
            timestamp = int(record["timestampMs"]) / 1000
        if not timestamp:
            totals["skipped"] += 1
            continue
        point = {"timestamp": timestamp, "lat": int(record["latitudeE7"]) / 10_000_000,
                 "lon": int(record["longitudeE7"]) / 10_000_000}
        if record.get("accuracy") is not None:
            point["accuracy_m"] = record["accuracy"]
        yield point


@use_case
def import_takeout(db, stream, source="google_takeout", progress=None):
    totals = {"inserted": 0, "duplicates": 0, "skipped": 0}
    return _import_fixes(db, _takeout_fixes(stream, totals), source, totals, progress)


@use_case
def import_timeline(db, open_text, kind, source="google_timeline", progress=None):
    """Import a Google Timeline export: Takeout's Semantic Location History, or a phone's own export.
    `open_text()` opens the file afresh (the phone export is read in two passes)."""
    return _import_fixes(db, iter_timeline_fixes(open_text, kind), source, {"inserted": 0, "duplicates": 0}, progress)


def file_format(head):
    """What a file is, from its first part: ("csv" | "takeout" | "timeline", detail), or None."""
    if '"locations"' in head and ("latitudeE7" in head or head.lstrip().startswith("{")):
        return "takeout", None
    kind = timeline_kind(head)
    if kind:
        return "timeline", kind
    first = head.lstrip("﻿").split("\n", 1)[0].lower()
    columns = {column.strip().strip('"') for column in first.split(",")}
    if {"timestamp", "lat", "lon"} <= columns:
        return "csv", None
    return None


@use_case
def import_file(db, path, source=None, progress=None):
    """Import a file of any kind above, told apart by its contents; returns the totals with its format."""
    def open_text():
        return open(path, encoding="utf-8-sig", newline="")

    with open_text() as stream:
        found = file_format(stream.read(1_000_000))
    if not found:
        raise ValueError("This file isn't a CSV with timestamp, lat and lon columns, a Google Takeout "
                         "Records.json, or a Google Timeline export")
    kind, detail = found
    source = (source or "").strip()[:80] or DEFAULT_SOURCES[kind]
    if kind == "csv":
        with open_text() as stream:
            totals = import_csv(db, stream, source, progress)
        totals.pop("events", None)
    elif kind == "takeout":
        with open_text() as stream:
            totals = import_takeout(db, stream, source, progress)
    else:
        totals = import_timeline(db, open_text, detail, source, progress)
    return {**totals, "format": kind if kind != "timeline" else f"timeline ({detail})", "source": source}
