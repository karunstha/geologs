"""One-time, all-or-nothing copy from an existing SQLite archive to PostGIS."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .postgres import PostgresConnection


TABLES = {
    "points": "id source external_id timestamp lat lon accuracy_m speed_mps speed_origin mode",
    "point_observations": "id point_id legacy_point_id source timestamp lat lon accuracy_m speed_mps received_at fingerprint",
    "places": "id name lat lon radius_m address",
    "reminders": "id text place_id active repeat cooldown_minutes created_at last_triggered_at",
    "events": "id reminder_id point_id triggered_at acknowledged_at webhook_status",
    "source_state": "source point_id",
    "observed_stays": "id source start_point_id end_point_id started_at ended_at lat lon point_count ongoing",
    "stay_processing_state": "source last_point_id last_timestamp candidate",
    "geocode_cache": "cache_key provider value created_at",
    "reverse_regions": "id provider center_lat center_lon radius_m value created_at expires_at",
}
IDENTITY_TABLES = ("points", "point_observations", "places", "reminders", "events", "observed_stays", "reverse_regions")


def migrate_sqlite(sqlite_path, postgres_url):
    source_path = Path(sqlite_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"SQLite archive not found: {source_path}")
    # The source must be stopped and checkpointed. immutable avoids sidecar writes
    # when the archive is bind-mounted read-only into the migration container.
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    target = PostgresConnection(postgres_url)
    counts = {}
    try:
        for table in TABLES:
            if target.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("PostGIS target must be empty before migration")
        for table, names in TABLES.items():
            if table == "point_observations" and table not in source_tables:
                continue
            columns = names.split()
            select = source.execute(f"SELECT {','.join(columns)} FROM {table}")
            placeholders = ",".join(["%s"] * len(columns))
            statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
            count = 0
            with target.connection.cursor() as cursor:
                while batch := select.fetchmany(1000):
                    cursor.executemany(statement, [tuple(row) for row in batch])
                    count += len(batch)
            counts[table] = count
        if "point_observations" not in source_tables:
            target.execute("""INSERT INTO point_observations
                (point_id,legacy_point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,received_at)
                SELECT id,id,source,timestamp,lat,lon,accuracy_m,speed_mps,timestamp FROM points""")
            counts["point_observations"] = counts["points"]
        for table in IDENTITY_TABLES:
            target.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), COALESCE(MAX(id),1), COUNT(*)>0) FROM {table}")
        for table, expected in counts.items():
            actual = target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if actual != expected:
                raise RuntimeError(f"count mismatch in {table}: {expected} vs {actual}")
        target.commit()
        return counts
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
