"""Streaming import for classic Google Takeout raw location records."""
from __future__ import annotations

import json
import re

from .analysis import process_stays


def iter_takeout_locations(stream):
    """Yield objects from a top-level `locations` array without loading the export."""
    decoder = json.JSONDecoder()
    buffer = ""
    marker = re.compile(r'"locations"\s*:\s*\[')
    while True:
        chunk = stream.read(65536)
        if not chunk:
            raise ValueError("Takeout file has no locations array")
        buffer += chunk
        match = marker.search(buffer)
        if match:
            buffer = buffer[match.end():]
            break
        if len(buffer) > 1_000_000:
            raise ValueError("Takeout locations array not found near start of file")
    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        if buffer.startswith("]"):
            return
        try:
            item, end = decoder.raw_decode(buffer)
        except json.JSONDecodeError:
            chunk = stream.read(65536)
            if not chunk:
                raise ValueError("incomplete Takeout locations array")
            buffer += chunk
            continue
        if not isinstance(item, dict):
            raise ValueError("Takeout location record must be an object")
        yield item
        buffer = buffer[end:]


def import_takeout(store, stream, source="google_takeout"):
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
            result = store.ingest(batch, trigger_reminders=False, process_analysis=False)
            totals["inserted"] += result["inserted"]
            totals["duplicates"] += result["duplicates"]
            batch = []
    if batch:
        result = store.ingest(batch, trigger_reminders=False, process_analysis=False)
        totals["inserted"] += result["inserted"]
        totals["duplicates"] += result["duplicates"]
    if totals["inserted"]:
        process_stays(store, {source}, rebuild=True)
        store.db.commit()
    return totals
