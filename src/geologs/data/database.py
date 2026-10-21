"""The archive's database: one PostgreSQL/PostGIS connection, its schema, and its repositories.

All SQL in GeoLogs lives in this package. Repositories never commit work:
each use case runs in transaction() and decides when its work is done. Only
cached values are kept as soon as they are written (see CachesRepository).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from .. import config
from .admin import AdminRepository
from .backup import BackupRepository
from .caches import CachesRepository
from .corrections import CorrectionsRepository
from .devices import DevicesRepository
from .fuel import FuelRepository
from .mail import OutboxRepository
from .paths import PathsRepository
from .place_routes import PlaceRoutesRepository
from .places import PlacesRepository
from .points import PointsRepository
from .postgres import OWNER_ID, PostgresConnection, PostgresPool, create_schema
from .reminders import RemindersRepository
from .settings import SettingsRepository
from .stays import StaysRepository
from .summaries import SummariesRepository
from .timeline import TimelineRepository
from .users import UsersRepository

# Stay algorithm versions: 5 and 8 changed stationary stays; 12 adds area stays (moving around one
# place) and stray-fix tolerance; 14 records whether travel between stays went somewhere else; 17
# adds quick stops on the way; 18 reads quiet stretches from apps that report only while moving as
# time spent still; 19 ends a stop at a sharp fix far away instead of holding it as a stray; 20
# measures a quick stop's spread from its centre and sets creeping traffic apart; 21 allows for
# accuracy at a stay's first fix, so rolling in does not split it. One rebuild covers every missing
# version.
STAY_REBUILDS = (5, 8, 12, 14, 17, 18, 19, 20, 21)


# Archives this process has created or upgraded (schema, then stays), so it happens once each.
_upgraded = set()
_upgrade_lock = threading.Lock()


def _archive_url(url):
    url = url or config.database_url()
    if not url:
        raise RuntimeError("Set GEOLOGS_DATABASE_URL to a PostgreSQL database with PostGIS")
    return url


def upgrade_again(url):
    """Upgrade the archive at `url` again on its next open: its contents changed under this process
    (an import), or it was started over."""
    with _upgrade_lock:
        _upgraded.discard(url)


def _upgrade_once(url, open_database, rebuild_stays):
    with _upgrade_lock:
        if url in _upgraded:
            return
        create_schema(url)
        db = open_database()
        try:
            db.upgrade(rebuild_stays)
        finally:
            db.close()
        _upgraded.add(url)


class Database:
    """The archive on one connection, working for one user (see as_user), with a repository per table group."""

    def __init__(self, conn, user_id=None):
        # Whether repositories wrote anything since the last commit or rollback,
        # and how many transaction() blocks are open.
        self.pending_writes = False
        self._depth = 0
        self.conn = conn
        self.user_id = None
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
        self.place_routes = PlaceRoutesRepository(self)
        self.fuel = FuelRepository(self)
        self.users = UsersRepository(self)
        self.outbox = OutboxRepository(self)
        self.admin = AdminRepository(self)
        self.backup = BackupRepository(self)
        self.as_user(user_id)

    def as_user(self, user_id):
        """Work for this user from now on: every user table shows and accepts only their rows
        (row-level security), and new rows are theirs. None means no user, and no such rows."""
        if self.pending_writes or self._depth:
            raise RuntimeError("finish the open transaction before switching users")
        # Kept for the session (not just this transaction), so a rollback later keeps the user.
        self.conn.execute("SELECT set_config('geologs.user_id', ?, false)", ("" if user_id is None else str(int(user_id)),))
        self.conn.commit()
        self.user_id = None if user_id is None else int(user_id)
        return self

    @classmethod
    def connect(cls, url=None, *, rebuild_stays, user_id=OWNER_ID):
        """The archive at `url` (else GEOLOGS_DATABASE_URL) on a connection of its own, for
        `user_id` (the archive's owner unless given). It is created or upgraded first if this
        process has not done so yet."""
        url = _archive_url(url)
        _upgrade_once(url, lambda: cls(PostgresConnection.connect(url)), rebuild_stays)
        return cls(PostgresConnection.connect(url), user_id)

    def upgrade(self, rebuild_stays):
        """Recalculate stays when an upgrade changed how they are found, or when an archive has
        samples but no stays yet, for every user. Finding stays is not this package's job, so the
        services pass `rebuild_stays(db, full)` in (see services.open_database)."""
        # A session lock: each user's work commits on its own, and another process waits for all of it.
        self.conn.execute("SELECT pg_advisory_lock(hashtext('geologs:upgrade'))")
        self.commit()
        try:
            missing = self._missing_migrations(STAY_REBUILDS)
            canonical = bool(self._missing_migrations((16,)))
            for user_id in self.users.ids():
                self.as_user(user_id)
                with self.transaction():
                    if missing:
                        rebuild_stays(self, True)
                    elif not self.conn.execute("SELECT 1 FROM stay_observation_state LIMIT 1").fetchone() \
                            and self.conn.execute("SELECT 1 FROM point_observations LIMIT 1").fetchone():
                        rebuild_stays(self, False)
                    if canonical:
                        # Keep one stored daily summary per date: the configured timezone, all sources.
                        self.summaries.keep_canonical(self.settings.get()["timezone"])
            self.as_user(None)
            with self.transaction():
                for version in missing + ([16] if canonical else []):
                    self.conn.execute("INSERT INTO schema_migrations(version) VALUES(?) ON CONFLICT DO NOTHING", (version,))
        finally:
            self.conn.execute("SELECT pg_advisory_unlock(hashtext('geologs:upgrade'))")
            self.commit()

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


class DatabasePool:
    """A server's connections: each request borrows one (open()) and gives it back (close()).
    The archive is created or upgraded once, when the pool starts."""

    def __init__(self, url=None, *, size, rebuild_stays, timeout=10):
        url = _archive_url(url)
        create_schema(url)
        self._pool = PostgresPool(url, size, timeout)
        _upgrade_once(url, self.open, rebuild_stays)

    def open(self, user_id=None):
        """A connection working for `user_id` (None: no user, for signing in and the like)."""
        return Database(self._pool.connection(), user_id)

    def close(self):
        self._pool.close()
