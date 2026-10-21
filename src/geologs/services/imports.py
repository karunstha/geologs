"""Importing history in bulk: CSV and GeoJSON files, Google Takeout's Records.json, Google Timeline
exports, and GeoLogs' own full backups."""
import csv
import gzip
import io
import re

from ..integrations.google_takeout import iter_json_array, iter_takeout_locations, iter_timeline_fixes, timeline_kind
from .backup import is_backup, restore_backup
from .ingest import ingest
from .paths import learn_path_history
from ..data.settings import IMPORT_STATUS_KEY
from .stays import process_stays
from .use_case import use_case

# Where each kind of file's fixes go unless the person names a tracker.
DEFAULT_SOURCES = {"csv": "csv", "geojson": "geojson", "takeout": "google_takeout", "timeline": "google_timeline"}


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
    else:
        # Nothing new, but an import of this file cut short earlier (a restart) may have left fixes
        # whose stays were never found; this finds them, and costs nothing when there are none.
        process_stays(db, {source})
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


def _geojson_fixes(stream, totals):
    """Point features with a `timestamp` (or `time`) property, as GeoLogs exports them, and lines
    with `coordTimes`, as tools converting GPX or KML write them. Anything else is skipped."""
    for feature in iter_json_array(stream, r'"features"\s*:\s*\[', missing="GeoJSON file has no features array"):
        geometry = (feature.get("geometry") or {}) if isinstance(feature, dict) else {}
        properties = (feature.get("properties") or {}) if isinstance(feature, dict) else {}
        if geometry.get("type") == "Point":
            positions, times = [geometry.get("coordinates")], [properties.get("timestamp") or properties.get("time")]
        elif geometry.get("type") in ("LineString", "MultiPoint") and isinstance(properties.get("coordTimes"), list):
            positions, times = geometry.get("coordinates") or [], properties["coordTimes"]
        else:
            totals["skipped"] += 1
            continue
        for position, timestamp in zip(positions, times):
            if not timestamp or not isinstance(position, list) or len(position) < 2:
                totals["skipped"] += 1
                continue
            point = {"timestamp": timestamp, "lat": position[1], "lon": position[0]}
            for optional in ("accuracy_m", "speed_mps", "external_id"):
                if len(positions) == 1 and properties.get(optional) is not None:
                    point[optional] = properties[optional]
            yield point


@use_case
def import_geojson(db, stream, source="geojson", progress=None):
    totals = {"inserted": 0, "duplicates": 0, "skipped": 0}
    return _import_fixes(db, _geojson_fixes(stream, totals), source, totals, progress)


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
    """What a file is, from its first part: ("backup" | "csv" | "geojson" | "takeout" | "timeline", detail), or None."""
    if is_backup(head):
        return "backup", None
    if re.search(r'"type"\s*:\s*"FeatureCollection"', head):
        return "geojson", None
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
    """Import a file of any kind above, gzipped or not, told apart by its contents; returns the
    totals with its format. A backup keeps its own tracker names; `source` is for the other kinds."""
    with open(path, "rb") as raw:
        gzipped = raw.read(2) == b"\x1f\x8b"

    def open_text():
        if gzipped:
            return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
        return open(path, encoding="utf-8-sig", newline="")

    with open_text() as stream:
        found = file_format(stream.read(1_000_000))
    if not found:
        raise ValueError("This file isn't a CSV with timestamp, lat and lon columns, a GeoJSON "
                         "FeatureCollection, a Google Takeout Records.json, a Google Timeline export, "
                         "or a GeoLogs backup")
    kind, detail = found
    if kind == "backup":
        totals = restore_backup(db, open_text, progress)
        return {**totals, "format": "backup", "overlaps": db.points.overlaps(totals.pop("sources"))}
    source = (source or "").strip()[:80] or DEFAULT_SOURCES[kind]
    if kind == "csv":
        with open_text() as stream:
            totals = import_csv(db, stream, source, progress)
        totals.pop("events", None)
    elif kind == "geojson":
        with open_text() as stream:
            totals = import_geojson(db, stream, source, progress)
    elif kind == "takeout":
        with open_text() as stream:
            totals = import_takeout(db, stream, source, progress)
    else:
        totals = import_timeline(db, open_text, detail, source, progress)
    return {**totals, "format": kind if kind != "timeline" else f"timeline ({detail})", "source": source,
            "overlaps": db.points.overlaps([source])}


# What the page says of an import the server was still running when it stopped.
CUT_SHORT = ("The server restarted before this import finished. Import the file again: what already "
             "came in is recognised and kept, and the rest is added.")


@use_case
def note_import(db, status):
    """Keep how the latest import is going, so it is known after a restart."""
    db.settings.set_value(IMPORT_STATUS_KEY, status)


@use_case
def last_import(db):
    """How the latest import went, as this server last noted it; one still running in a server that
    stopped is reported as cut short."""
    status = db.settings.value(IMPORT_STATUS_KEY) or {"state": "idle"}
    if status.get("state") == "running":
        status = {**status, "state": "failed", "phase": None, "error": CUT_SHORT}
    return status
