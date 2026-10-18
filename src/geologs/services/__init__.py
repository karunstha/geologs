"""Use cases: each joins the domain rules to storage and outside services, and owns its transaction."""
from .. import config
from ..data import INTEGRITY_ERRORS

__all__ = ["INTEGRITY_ERRORS", "migrate_sqlite", "open_database", "open_pool"]


def _rebuild_stays(db, full):
    from .stays import process_stays
    return process_stays(db, rebuild=full)


def open_database(url=None, user_id=None):
    """The archive (`url`, else GEOLOGS_DATABASE_URL) on a connection of its own, for the command
    line and tests, working for `user_id` (the archive's owner unless given). It is created or
    upgraded first if this process has not done so."""
    from ..data import Database, OWNER_ID
    return Database.connect(url, rebuild_stays=_rebuild_stays, user_id=OWNER_ID if user_id is None else user_id)


def open_pool(url=None, size=None, timeout=10):
    """Connections for a server: each request borrows one with pool.open() and gives it back with
    close(); a request waits up to `timeout` seconds for one. The archive is created or upgraded
    once, when the pool starts."""
    from ..data import DatabasePool
    return DatabasePool(url, size=size or config.database_pool_size(), rebuild_stays=_rebuild_stays,
                        timeout=timeout)


def migrate_sqlite(sqlite_path, postgres_url):
    """Copy a SQLite archive into an empty PostGIS database."""
    from ..data.migration import migrate_sqlite as copy
    return copy(sqlite_path, postgres_url)
