"""Importing history in bulk: CSV files and Google Takeout's Records.json."""
import csv
import io

from ..integrations.google_takeout import iter_takeout_locations
from .ingest import ingest
from .paths import learn_path_history
from .stays import process_stays
from .use_case import use_case


@use_case
def import_csv(db, content, source="csv"):
    """Import CSV with timestamp, lat, lon and optional accuracy_m, speed_mps, external_id columns."""
    reader = csv.DictReader(io.StringIO(content) if isinstance(content, str) else content)
    batch = []
    totals = {"inserted": 0, "duplicates": 0, "events": []}
    for row in reader:
        row["source"] = source
        for optional in ("accuracy_m", "speed_mps", "external_id"):
            if row.get(optional) == "":
                row.pop(optional)
        batch.append(row)
        if len(batch) == 1000:
            result = ingest(db, batch, trigger_reminders=False, process_analysis=False)
            totals["inserted"] += result["inserted"]
            totals["duplicates"] += result["duplicates"]
            # Keep each batch as it is read: an import can hold millions of fixes.
            db.commit()
            batch = []
    if batch:
        result = ingest(db, batch, trigger_reminders=False, process_analysis=False)
        totals["inserted"] += result["inserted"]
        totals["duplicates"] += result["duplicates"]
    if totals["inserted"]:
        process_stays(db, {source}, rebuild=True)
        learn_path_history(db, source=source)
    return totals


@use_case
def import_takeout(db, stream, source="google_takeout"):
    totals = {"inserted": 0, "duplicates": 0, "skipped": 0}
    batch = []
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
        point = {"source": source, "timestamp": timestamp,
                 "lat": int(record["latitudeE7"]) / 10_000_000,
                 "lon": int(record["longitudeE7"]) / 10_000_000}
        if record.get("accuracy") is not None:
            point["accuracy_m"] = record["accuracy"]
        batch.append(point)
        if len(batch) == 1000:
            result = ingest(db, batch, trigger_reminders=False, process_analysis=False)
            totals["inserted"] += result["inserted"]
            totals["duplicates"] += result["duplicates"]
            # Keep each batch as it is read: an import can hold millions of fixes.
            db.commit()
            batch = []
    if batch:
        result = ingest(db, batch, trigger_reminders=False, process_analysis=False)
        totals["inserted"] += result["inserted"]
        totals["duplicates"] += result["duplicates"]
    if totals["inserted"]:
        process_stays(db, {source}, rebuild=True)
        learn_path_history(db, source=source)
    return totals
