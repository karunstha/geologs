"""A fresh PostgreSQL database for each test, cloned from one migrated template.

Tests run against the server at GEOLOGS_TEST_DATABASE_URL (with PostGIS, and a role that may
create databases); see README → Development. Cloning a template takes a fraction of a second, so
every test starts from an empty, fully migrated archive of its own.
"""
import os
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg

# Tests use the app's own login, as production does with GEOLOGS_APP_DB_PASSWORD set.
os.environ.setdefault("GEOLOGS_APP_DB_PASSWORD", "geologs-test-app")

TEMPLATE = "geologs_test_template"
PREFIX = "geologs_test_"
_lock = threading.Lock()
_template_ready = False


def server_url():
    url = os.environ.get("GEOLOGS_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("Set GEOLOGS_TEST_DATABASE_URL to a PostgreSQL server with PostGIS that tests may "
                           "create databases on (see README → Development)")
    return url


def database_url(name):
    return urlunsplit(urlsplit(server_url())._replace(path="/" + name))


def _admin():
    return psycopg.connect(server_url(), autocommit=True)


def _ensure_template():
    global _template_ready
    with _lock:
        if _template_ready:
            return
        with _admin() as conn:
            # Databases a crashed run left behind, then a template migrated by this checkout's code.
            for row in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (PREFIX + "%",)).fetchall():
                conn.execute(f'DROP DATABASE "{row[0]}" WITH (FORCE)')
            conn.execute(f"CREATE DATABASE {TEMPLATE}")
        from geologs.services import open_database
        open_database(database_url(TEMPLATE)).close()
        _template_ready = True


def _drop(name):
    with _admin() as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def scratch_database(test):
    """The URL of a new, empty, migrated database for one test; it is dropped when the test ends."""
    _ensure_template()
    name = PREFIX + uuid.uuid4().hex[:16]
    with _admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE {TEMPLATE}')
    test.addCleanup(_drop, name)
    url = database_url(name)
    # A clone of the template is already migrated: skip upgrading it again on first open.
    from geologs.data import database
    database._upgraded.add(url)
    return url
