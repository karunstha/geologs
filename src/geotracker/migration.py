"""One-time, all-or-nothing copy from an existing SQLite archive to PostGIS."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .postgres import PostgresConnection


TABLES = {
    "points": "id source external_id timestamp lat lon accuracy_m speed_mps speed_origin mode",
    "point_observations": "id point_id legacy_point_id source timestamp lat lon accuracy_m speed_mps speed_origin received_at fingerprint",
    "places": "id name lat lon radius_m address category provider_place_id saved",
    "reminders": "id text place_id active repeat cooldown_minutes created_at last_triggered_at cancelled_at destination_name destination_address destination_category destination_provider_place_id match_name",
    "events": "id reminder_id point_id triggered_at webhook_status",
    "source_state": "source point_id",
    "observed_stays": "id source start_point_id end_point_id started_at ended_at lat lon point_count ongoing",
    "stay_processing_state": "source last_point_id last_timestamp candidate",
    "observed_stays_v2": "id source start_observation_id end_observation_id started_at ended_at lat lon point_count ongoing kind activity distance_m radius_m went_elsewhere",
    "stay_observation_state": "source last_observation_id last_timestamp candidate",
    "place_labels": "id name lat lon radius_m address category provider_place_id created_at updated_at source started_at ended_at",
    "app_settings": "key value",
    "analysis_cache": "cache_key watermark value created_at",
    "daily_summaries": "id summary_date timezone source watermark points distance_km trip_count place_count reminder_count summary created_at updated_at",
    "geocode_cache": "cache_key provider value created_at",
    "reverse_regions": "id provider center_lat center_lon radius_m value created_at expires_at",
}
# Columns added after an archive was created take their schema default.
COLUMN_DEFAULTS = {"kind": "'stationary'"}
IDENTITY_TABLES = ("points", "point_observations", "places", "place_labels", "reminders", "events", "observed_stays", "observed_stays_v2", "daily_summaries", "reverse_regions")


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
            if table in ("point_observations", "observed_stays_v2", "stay_observation_state", "app_settings", "analysis_cache", "daily_summaries", "place_labels") and table not in source_tables:
                continue
            columns = names.split()
            source_columns = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
            select = source.execute("SELECT " + ",".join(column if column in source_columns else f"{COLUMN_DEFAULTS.get(column, 'NULL')} AS {column}" for column in columns) + f" FROM {table}")
            placeholders = ",".join(["%s"] * len(columns))
            statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
            count = 0
            with target.connection.cursor() as cursor:
                while batch := select.fetchmany(1000):
                    cursor.executemany(statement, [tuple(row) for row in batch])
                    count += len(batch)
            counts[table] = count
        target.execute("UPDATE reminders SET active=0 WHERE repeat=1 AND last_triggered_at IS NOT NULL")
        target.execute("UPDATE reminders SET repeat=0")
        target.execute("UPDATE reminders SET cancelled_at=created_at WHERE active=0 AND last_triggered_at IS NULL AND cancelled_at IS NULL")
        if "point_observations" not in source_tables:
            target.execute("""INSERT INTO point_observations
                (point_id,legacy_point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,received_at)
                SELECT id,id,source,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,timestamp FROM points""")
            counts["point_observations"] = counts["points"]
        else:
            target.execute("""UPDATE point_observations o SET speed_mps=p.speed_mps,speed_origin=p.speed_origin
                FROM points p WHERE p.id=o.point_id AND (o.legacy_point_id IS NOT NULL OR
                (o.speed_mps IS NULL AND o.timestamp=p.timestamp AND o.lat=p.lat AND o.lon=p.lon
                 AND o.accuracy_m IS NOT DISTINCT FROM p.accuracy_m))""")
            target.execute("""UPDATE point_observations SET speed_origin='reported'
                WHERE speed_mps IS NOT NULL AND speed_origin IS NULL""")
            target.execute("""WITH prior AS (
                SELECT id,lat,lon,timestamp,
                    LAG(lat) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lat,
                    LAG(lon) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lon,
                    LAG(timestamp) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_timestamp
                FROM point_observations)
                UPDATE point_observations o SET speed_mps=0,speed_origin='estimated'
                FROM prior p WHERE o.id=p.id AND o.speed_mps IS NULL
                AND p.lat=p.prev_lat AND p.lon=p.prev_lon
                AND p.timestamp::timestamptz-p.prev_timestamp::timestamptz > interval '0 seconds'
                AND p.timestamp::timestamptz-p.prev_timestamp::timestamptz <= interval '15 minutes'""")
        target.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT id,source,'movement',NULL FROM point_observations WHERE true
            ON CONFLICT(observation_id) DO NOTHING""")
        target.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
            SELECT id FROM (SELECT id,ROW_NUMBER() OVER (
                PARTITION BY source,timestamp ORDER BY COALESCE(accuracy_m,1000000000),id DESC) AS rank
                FROM point_observations) ranked WHERE rank>1)""")
        target.execute("""DELETE FROM timeline_fixes t USING point_observations o
            WHERE t.observation_id=o.id AND EXISTS (
                SELECT 1 FROM observed_stays_v2 s WHERE s.source=o.source AND s.kind='stationary'
                AND o.timestamp>=s.started_at AND o.timestamp<=s.ended_at
                AND o.id<>s.start_observation_id AND o.id<>s.end_observation_id)""")
        target.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT start_observation_id,source,'arrival',id FROM observed_stays_v2 WHERE kind='stationary'
            ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""")
        target.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT end_observation_id,source,CASE WHEN ongoing=1 THEN 'latest' ELSE 'departure' END,id
            FROM observed_stays_v2 WHERE kind='stationary' ON CONFLICT(observation_id) DO UPDATE
            SET role=excluded.role,stay_id=excluded.stay_id""")
        target.execute("""WITH candidates AS (
            SELECT source,candidate::jsonb AS value FROM stay_observation_state
            WHERE (candidate::jsonb->>'count')::integer>=2
            AND (candidate::jsonb->>'end')::timestamptz-(candidate::jsonb->>'start')::timestamptz<interval '15 minutes')
            DELETE FROM timeline_fixes t USING point_observations o,candidates c
            WHERE t.observation_id=o.id AND o.source=c.source
            AND o.timestamp>=(c.value->>'start') AND o.timestamp<=(c.value->>'end')
            AND o.id<>(c.value->>'start_id')::bigint AND o.id<>(c.value->>'end_id')::bigint""")
        target.execute("""WITH candidates AS (
            SELECT source,candidate::jsonb AS value FROM stay_observation_state
            WHERE (candidate::jsonb->>'count')::integer>=2
            AND (candidate::jsonb->>'end')::timestamptz-(candidate::jsonb->>'start')::timestamptz<interval '15 minutes')
            INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT (value->>'start_id')::bigint,source,'arrival',NULL FROM candidates WHERE true
            ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""")
        target.execute("""WITH candidates AS (
            SELECT source,candidate::jsonb AS value FROM stay_observation_state
            WHERE (candidate::jsonb->>'count')::integer>=2
            AND (candidate::jsonb->>'end')::timestamptz-(candidate::jsonb->>'start')::timestamptz<interval '15 minutes')
            INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT (value->>'end_id')::bigint,source,'latest',NULL FROM candidates WHERE true
            ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""")
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
