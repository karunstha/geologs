"""The archive's database: one connection (SQLite, or PostgreSQL/PostGIS), its schema, and its repositories.

All SQL in GeoLogs lives in this package. Repositories never commit work:
each use case runs in transaction() and decides when its work is done. Only
cached values are kept as soon as they are written (see CachesRepository).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .. import config
from .caches import CachesRepository
from .corrections import CorrectionsRepository
from .devices import DevicesRepository
from .fuel import FuelRepository
from .paths import PathsRepository
from .places import PlacesRepository
from .points import PointsRepository
from .postgres import PostgresConnection
from .reminders import RemindersRepository
from .settings import SettingsRepository
from .stays import StaysRepository
from .summaries import SummariesRepository
from .timeline import TimelineRepository

SQLITE_REMINDERS_TABLE = """CREATE TABLE {name} (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER REFERENCES places(id),
        active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
        cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
        created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT,
        destination_name TEXT, destination_address TEXT,
        destination_category TEXT, destination_provider_place_id TEXT, match_name TEXT
    )"""
REMINDER_COLUMNS = ("id,text,place_id,active,repeat,cooldown_minutes,created_at,last_triggered_at,cancelled_at,"
                    "destination_name,destination_address,destination_category,destination_provider_place_id")


def database_path() -> str:
    """The SQLite archive's file (see config.database_file)."""
    return config.database_file()


class Database:
    def __init__(self, path=None, *, rebuild_stays):
        """Open (and create or upgrade) the archive.

        An upgrade that changes how stays are found, or an archive with samples
        but no stays yet, needs stays recalculated: `rebuild_stays(db, full)`
        does that. Finding stays is not this package's job, so the services
        pass it in (see services.open_database).
        """
        url = config.database_url() if path is None else None
        self.postgres = bool(url)
        # Whether repositories wrote anything since the last commit or rollback,
        # and how many transaction() blocks are open.
        self.pending_writes = False
        self._depth = 0
        self.path = str(url or path or database_path())
        if self.postgres:
            self.conn = PostgresConnection(url)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.settings = SettingsRepository(self)
        self.points = PointsRepository(self)
        self.timeline = TimelineRepository(self)
        self.stays = StaysRepository(self)
        self.places = PlacesRepository(self)
        self.reminders = RemindersRepository(self)
        self.corrections = CorrectionsRepository(self)
        self.devices = DevicesRepository(self)
        self.caches = CachesRepository(self)
        self.summaries = SummariesRepository(self)
        self.paths = PathsRepository(self)
        self.fuel = FuelRepository(self)
        if not self.postgres:
            self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT,
            timestamp TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
            accuracy_m REAL, speed_mps REAL, speed_origin TEXT, mode TEXT NOT NULL,
            UNIQUE(source, external_id), UNIQUE(source, timestamp, lat, lon)
        );
        CREATE INDEX IF NOT EXISTS points_time ON points(timestamp);
        CREATE INDEX IF NOT EXISTS points_source_time ON points(source,timestamp);
        CREATE TABLE IF NOT EXISTS point_observations (
            id INTEGER PRIMARY KEY, point_id INTEGER NOT NULL REFERENCES points(id),
            legacy_point_id INTEGER UNIQUE REFERENCES points(id),
            source TEXT NOT NULL, timestamp TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, accuracy_m REAL, speed_mps REAL,
            speed_origin TEXT,
            received_at TEXT NOT NULL, fingerprint TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS point_observations_time ON point_observations(timestamp);
        CREATE INDEX IF NOT EXISTS point_observations_source_time ON point_observations(source,timestamp);
        CREATE INDEX IF NOT EXISTS point_observations_source_id ON point_observations(source,id);
        CREATE TABLE IF NOT EXISTS timeline_fixes (
            observation_id INTEGER PRIMARY KEY REFERENCES point_observations(id) ON DELETE CASCADE,
            source TEXT NOT NULL, role TEXT NOT NULL,
            stay_id INTEGER REFERENCES observed_stays_v2(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS timeline_fixes_source ON timeline_fixes(source);
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_cache (
            cache_key TEXT PRIMARY KEY, watermark TEXT NOT NULL,
            value TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY, summary_date TEXT NOT NULL, timezone TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '', watermark TEXT NOT NULL,
            points INTEGER NOT NULL, distance_km REAL NOT NULL,
            trip_count INTEGER NOT NULL, place_count INTEGER NOT NULL,
            reminder_count INTEGER NOT NULL, summary TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, brief TEXT,
            UNIQUE(summary_date,timezone,source)
        );
        CREATE INDEX IF NOT EXISTS daily_summaries_date ON daily_summaries(summary_date DESC,timezone);
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 150,
            address TEXT, category TEXT, provider_place_id TEXT, saved INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER REFERENCES places(id),
            active INTEGER NOT NULL DEFAULT 1, repeat INTEGER NOT NULL DEFAULT 0,
            cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
            created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT,
            destination_name TEXT, destination_address TEXT,
            destination_category TEXT, destination_provider_place_id TEXT, match_name TEXT
        );
        CREATE INDEX IF NOT EXISTS reminders_active_created ON reminders(active,created_at);
        CREATE TABLE IF NOT EXISTS mode_overrides (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, mode TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fuel_prices (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, effective_date TEXT NOT NULL, price_per_l REAL NOT NULL,
            fetched_at TEXT NOT NULL, UNIQUE(source, effective_date)
        );
        CREATE TABLE IF NOT EXISTS ingest_tokens (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, hint TEXT NOT NULL,
            source TEXT, created_at TEXT NOT NULL, last_used_at TEXT, last_source TEXT
        );
        CREATE TABLE IF NOT EXISTS stay_edits (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, action TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL, lat REAL, lon REAL,
            created_at TEXT NOT NULL
        );
        DROP TABLE IF EXISTS ingest_status;
        CREATE TABLE IF NOT EXISTS ingest_devices (
            kind TEXT NOT NULL, device TEXT NOT NULL DEFAULT '', last_attempt_at TEXT NOT NULL,
            last_status INTEGER NOT NULL, last_message TEXT, last_success_at TEXT,
            received INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, device)
        );
        CREATE TABLE IF NOT EXISTS place_labels (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
            radius_m REAL NOT NULL, address TEXT, category TEXT, provider_place_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            source TEXT, started_at TEXT, ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL REFERENCES reminders(id),
            point_id INTEGER NOT NULL REFERENCES points(id), triggered_at TEXT NOT NULL,
            webhook_status TEXT DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS events_triggered ON events(triggered_at);
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY, point_id INTEGER NOT NULL REFERENCES points(id)
        );
        CREATE TABLE IF NOT EXISTS observed_stays (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            start_point_id INTEGER NOT NULL UNIQUE REFERENCES points(id),
            end_point_id INTEGER NOT NULL REFERENCES points(id),
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            point_count INTEGER NOT NULL, ongoing INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS observed_stays_time ON observed_stays(started_at);
        CREATE TABLE IF NOT EXISTS stay_processing_state (
            source TEXT PRIMARY KEY, last_point_id INTEGER NOT NULL,
            last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observed_stays_v2 (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL,
            start_observation_id INTEGER NOT NULL UNIQUE REFERENCES point_observations(id),
            end_observation_id INTEGER NOT NULL REFERENCES point_observations(id),
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            point_count INTEGER NOT NULL, ongoing INTEGER NOT NULL DEFAULT 1,
            kind TEXT NOT NULL DEFAULT 'stationary', activity TEXT, distance_m REAL, radius_m REAL,
            went_elsewhere INTEGER
        );
        CREATE INDEX IF NOT EXISTS observed_stays_v2_time ON observed_stays_v2(started_at);
        CREATE INDEX IF NOT EXISTS observed_stays_v2_source_time ON observed_stays_v2(source,started_at,ended_at);
        CREATE TABLE IF NOT EXISTS stay_observation_state (
            source TEXT PRIMARY KEY, last_observation_id INTEGER NOT NULL,
            last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL, nearby TEXT
        );
        CREATE TABLE IF NOT EXISTS known_paths (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            mode TEXT NOT NULL, distance_m REAL NOT NULL,
            min_lat REAL NOT NULL, min_lon REAL NOT NULL, max_lat REAL NOT NULL, max_lon REAL NOT NULL,
            coordinates TEXT NOT NULL, UNIQUE(source, started_at)
        );
        CREATE INDEX IF NOT EXISTS known_paths_bounds ON known_paths(min_lat,max_lat);
        CREATE TABLE IF NOT EXISTS geocode_cache (
            cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, value TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reverse_regions (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
            center_lat REAL NOT NULL, center_lon REAL NOT NULL, radius_m REAL NOT NULL,
            value TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reverse_regions_provider_expiry
            ON reverse_regions(provider, expires_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS reverse_regions_spatial
            USING rtree(id, min_lat, max_lat, min_lon, max_lon);
        CREATE TRIGGER IF NOT EXISTS reverse_regions_delete AFTER DELETE ON reverse_regions
        BEGIN DELETE FROM reverse_regions_spatial WHERE id=old.id; END;
            """)
        if not self.postgres:
            label_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(place_labels)")}
            for column in ("source", "started_at", "ended_at"):
                if column not in label_columns:
                    self.conn.execute(f"ALTER TABLE place_labels ADD COLUMN {column} TEXT")
            # Places named around the open stop, for name-only reminders (kept apart from the stop itself).
            if "nearby" not in {row[1] for row in self.conn.execute("PRAGMA table_info(stay_observation_state)")}:
                self.conn.execute("ALTER TABLE stay_observation_state ADD COLUMN nearby TEXT")
            if "brief" not in {row[1] for row in self.conn.execute("PRAGMA table_info(daily_summaries)")}:
                self.conn.execute("ALTER TABLE daily_summaries ADD COLUMN brief TEXT")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            rebuild_analysis = version < 18
            if version < 1:
                self.conn.execute("UPDATE points SET mode='running_cycling' WHERE mode='cycling'")
            if version < 2:
                self.conn.execute("""INSERT OR IGNORE INTO point_observations
                    (point_id,legacy_point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,received_at)
                    SELECT id,id,source,timestamp,lat,lon,accuracy_m,speed_mps,timestamp FROM points""")
                self.conn.execute("PRAGMA user_version=2")
            if version < 3:
                columns = {row[1] for row in self.conn.execute("PRAGMA table_info(point_observations)")}
                if "speed_origin" not in columns:
                    self.conn.execute("ALTER TABLE point_observations ADD COLUMN speed_origin TEXT")
                self.conn.execute("""UPDATE point_observations SET speed_mps=(SELECT p.speed_mps FROM points p WHERE p.id=point_id),
                    speed_origin=(SELECT p.speed_origin FROM points p WHERE p.id=point_id)
                    WHERE legacy_point_id IS NOT NULL OR
                    (speed_mps IS NULL AND EXISTS (SELECT 1 FROM points p WHERE p.id=point_id
                        AND p.timestamp=point_observations.timestamp AND p.lat=point_observations.lat
                        AND p.lon=point_observations.lon AND p.accuracy_m IS point_observations.accuracy_m))""")
                self.conn.execute("UPDATE point_observations SET speed_origin='reported' WHERE speed_mps IS NOT NULL AND speed_origin IS NULL")
                self.conn.execute("""WITH prior AS (
                    SELECT id,lat,lon,timestamp,
                        LAG(lat) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lat,
                        LAG(lon) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_lon,
                        LAG(timestamp) OVER (PARTITION BY source ORDER BY timestamp,id) AS prev_timestamp
                    FROM point_observations)
                    UPDATE point_observations SET speed_mps=0,speed_origin='estimated'
                    WHERE speed_mps IS NULL AND id IN (
                        SELECT id FROM prior WHERE lat=prev_lat AND lon=prev_lon
                        AND (julianday(timestamp)-julianday(prev_timestamp))*86400 > 0
                        AND (julianday(timestamp)-julianday(prev_timestamp))*86400 <= 900)""")
                self.conn.execute("PRAGMA user_version=3")
            if version < 6:
                # Location reminders are one-shot. Preserve already-triggered
                # repeating reminders as history instead of rearming them.
                self.conn.execute("UPDATE reminders SET active=0 WHERE repeat=1 AND last_triggered_at IS NOT NULL")
                self.conn.execute("UPDATE reminders SET repeat=0")
            if version < 7:
                reminder_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(reminders)")}
                if "cancelled_at" not in reminder_columns:
                    self.conn.execute("ALTER TABLE reminders ADD COLUMN cancelled_at TEXT")
                self.conn.execute("UPDATE reminders SET cancelled_at=created_at WHERE active=0 AND last_triggered_at IS NULL AND cancelled_at IS NULL")
            if version < 9:
                place_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(places)")}
                if "category" not in place_columns:
                    self.conn.execute("ALTER TABLE places ADD COLUMN category TEXT")
                if "provider_place_id" not in place_columns:
                    self.conn.execute("ALTER TABLE places ADD COLUMN provider_place_id TEXT")
            if version < 10:
                place_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(places)")}
                if "saved" not in place_columns:
                    self.conn.execute("ALTER TABLE places ADD COLUMN saved INTEGER DEFAULT 1")
                reminder_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(reminders)")}
                for column in ("destination_name", "destination_address", "destination_category", "destination_provider_place_id"):
                    if column not in reminder_columns:
                        self.conn.execute(f"ALTER TABLE reminders ADD COLUMN {column} TEXT")
                self.conn.execute("""UPDATE reminders SET destination_name=(SELECT name FROM places WHERE id=place_id),
                    destination_address=(SELECT address FROM places WHERE id=place_id),
                    destination_category=(SELECT category FROM places WHERE id=place_id),
                    destination_provider_place_id=(SELECT provider_place_id FROM places WHERE id=place_id)
                    WHERE destination_name IS NULL""")
                event_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(events)")}
                if "acknowledged_at" in event_columns:
                    self.conn.execute("ALTER TABLE events DROP COLUMN acknowledged_at")
            if version < 11:
                stay_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(observed_stays_v2)")}
                for column, definition in (("kind", "TEXT NOT NULL DEFAULT 'stationary'"), ("activity", "TEXT"),
                                           ("distance_m", "REAL"), ("radius_m", "REAL")):
                    if column not in stay_columns:
                        self.conn.execute(f"ALTER TABLE observed_stays_v2 ADD COLUMN {column} {definition}")
            if version < 12:
                stay_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(observed_stays_v2)")}
                if "went_elsewhere" not in stay_columns:
                    self.conn.execute("ALTER TABLE observed_stays_v2 ADD COLUMN went_elsewhere INTEGER")
            if version < 13:
                reminder_columns = {row[1]: row for row in self.conn.execute("PRAGMA table_info(reminders)")}
                if reminder_columns["place_id"][3]:
                    self._rebuild_sqlite_reminders()
                elif "match_name" not in reminder_columns:
                    self.conn.execute("ALTER TABLE reminders ADD COLUMN match_name TEXT")
                self.summaries.keep_canonical(self.settings.get()["timezone"])
        # Stay algorithm versions: 5 and 8 changed stationary stays; 12 adds area
        # stays (moving around one place) and stray-fix tolerance; 14 records
        # whether travel between stays went somewhere else; 17 adds quick stops
        # on the way (SQLite user_version 14); 18 reads quiet stretches from apps
        # that report only while moving as time spent still (SQLite 15); 19 ends
        # a stop at a sharp fix far away instead of holding it as a stray (SQLite
        # 16); 20 measures a quick stop's spread from its centre and sets creeping
        # traffic apart (SQLite 17); 21 allows for accuracy at a stay's first fix,
        # so rolling in does not split it (SQLite 18). One rebuild covers every
        # missing version.
        rebuild_versions = (5, 8, 12, 14, 17, 18, 19, 20, 21)
        if self.postgres and self._missing_migrations(rebuild_versions):
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('geologs:stays:rebuild'))")
            missing = self._missing_migrations(rebuild_versions)
            if missing:
                rebuild_stays(self, True)
                for version_number in missing:
                    self.conn.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version_number,))
        elif not self.postgres and rebuild_analysis:
            rebuild_stays(self, True)
            self.conn.execute("PRAGMA user_version=12")
        elif not self.conn.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone() and self.conn.execute("SELECT 1 FROM point_observations LIMIT 1").fetchone():
            if self.postgres:
                self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('geologs:stays:bootstrap'))")
            if not self.conn.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone():
                rebuild_stays(self, False)
        if not self.postgres and version < 8:
            self.conn.execute("PRAGMA user_version=8")
        if not self.postgres and version < 9:
            self.conn.execute("DELETE FROM analysis_cache")
            self.conn.execute("PRAGMA user_version=9")
        if not self.postgres and version < 18:
            self.conn.execute("PRAGMA user_version=18")
        if self.postgres and self._missing_migrations((16,)):
            # Keep one stored daily summary per date: the configured timezone, all sources.
            self.summaries.keep_canonical(self.settings.get()["timezone"])
            self.conn.execute("INSERT INTO schema_migrations(version) VALUES(16) ON CONFLICT DO NOTHING")
        self.commit()

    def _rebuild_sqlite_reminders(self):
        """Let place_id be empty (name-only reminders): SQLite needs a table rebuild for that."""
        self.commit()
        # Compare against violations that already existed, so an old orphan row
        # elsewhere cannot block the upgrade; only new breakage aborts it.
        existing = {tuple(row) for row in self.conn.execute("PRAGMA foreign_key_check").fetchall()}
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(SQLITE_REMINDERS_TABLE.format(name="reminders_v13"))
            self.conn.execute(f"INSERT INTO reminders_v13 ({REMINDER_COLUMNS}) SELECT {REMINDER_COLUMNS} FROM reminders")
            self.conn.execute("DROP TABLE reminders")
            self.conn.execute("ALTER TABLE reminders_v13 RENAME TO reminders")
            self.conn.execute("CREATE INDEX IF NOT EXISTS reminders_active_created ON reminders(active,created_at)")
            if {tuple(row) for row in self.conn.execute("PRAGMA foreign_key_check").fetchall()} - existing:
                raise RuntimeError("reminder rebuild would break foreign keys")
            self.commit()
        except Exception:
            self.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.conn.close()

    def commit(self):
        self.conn.commit()
        self.pending_writes = False

    def rollback(self):
        self.conn.rollback()
        self.pending_writes = False

    @contextmanager
    def transaction(self):
        """Group work into one transaction: committed when the block ends, rolled back if it raises.

        Blocks nest: an inner block joins the outermost one, which alone
        commits or rolls back, so no part can save half of the whole.
        """
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if not self._depth:
                self.rollback()
            raise
        self._depth -= 1
        if not self._depth:
            self.commit()

    def _missing_migrations(self, versions):
        applied = {row["version"] for row in self.rows("SELECT version FROM schema_migrations")}
        return [version for version in versions if version not in applied]

    def rows(self, query, args=()):
        """Every row of a query, as dicts."""
        return [dict(row) for row in self.conn.execute(query, args).fetchall()]
