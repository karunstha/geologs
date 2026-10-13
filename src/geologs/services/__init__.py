"""Use cases: each joins the domain rules to storage and outside services, and owns its transaction."""
from ..data import INTEGRITY_ERRORS

__all__ = ["INTEGRITY_ERRORS", "migrate_sqlite", "open_database"]


def open_database(path=None):
    """Open the archive (GEOLOGS_DATABASE_URL, else GEOLOGS_DB, else the default file), upgrading it as needed."""
    from ..data import Database
    from .stays import process_stays
    return Database(path, rebuild_stays=lambda db, full: process_stays(db, rebuild=full))


def migrate_sqlite(sqlite_path, postgres_url):
    """Copy a SQLite archive into an empty PostGIS database."""
    from ..data.migration import migrate_sqlite as copy
    return copy(sqlite_path, postgres_url)
