"""Entry point and source adapters."""
import argparse
import json
import os
import sys

from . import home_assistant
from .. import config
from ..services import migrate_sqlite, open_database
from ..services.imports import import_csv
from ..services.ingest import record_locations
from ..services.stays import rebuild_stays
from .http import serve


LEGACY_PREFIX = "GEOTRACKER_"


def adopt_legacy_names():
    """Honour settings still named for the old project name, Geotracker.

    Without this, a cron poll with an un-renamed .env would miss GEOLOGS_URL
    and quietly write to a local database instead of the server.
    """
    adopted = []
    for key in sorted(os.environ):
        if key.startswith(LEGACY_PREFIX):
            current = "GEOLOGS_" + key[len(LEGACY_PREFIX):]
            if current not in os.environ:
                os.environ[current] = os.environ[key]
                adopted.append(key)
    return adopted


def load_env_file():
    """Use local .env values when the process environment has no override."""
    path = config.env_file()
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.isidentifier() or not key.isupper():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def poll_home_assistant():
    """Record the Home Assistant entity's position once: on the server at GEOLOGS_URL, else locally."""
    point = home_assistant.read_point()
    endpoint, token = config.remote_server()
    if endpoint:
        return home_assistant.push(endpoint, token, point)
    db = open_database()
    try:
        return record_locations(db, [point])
    finally:
        db.close()


def main():
    legacy = adopt_legacy_names()
    load_env_file()
    legacy += adopt_legacy_names()
    if legacy:
        print("geologs: rename " + ", ".join(legacy) + " to GEOLOGS_*; the old names still work for now",
              file=sys.stderr)
    parser = argparse.ArgumentParser(prog="geologs")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="run local browser, REST API and MCP HTTP")
    web.add_argument("--host", default=config.host())
    web.add_argument("--port", type=int, default=config.port())
    sub.add_parser("poll-ha", help="poll a Home Assistant device tracker once (run from cron)")
    sub.add_parser("rebuild-stays", help="rebuild observed stays after historical import or algorithm changes")
    sub.add_parser("learn-paths", help="learn known paths from every well-recorded trip and report the library")
    imp = sub.add_parser("import-csv", help="import CSV with timestamp,lat,lon and optional accuracy_m,speed_mps")
    imp.add_argument("file")
    imp.add_argument("--source", default="csv")
    takeout = sub.add_parser("import-takeout", help="stream classic Google Takeout Records.json locations")
    takeout.add_argument("file")
    takeout.add_argument("--source", default="google_takeout")
    migrate = sub.add_parser("migrate-sqlite", help="copy an existing SQLite archive to an empty PostGIS database")
    migrate.add_argument("file")
    args = parser.parse_args()
    try:
        if args.command == "serve": serve(args.host, args.port)
        elif args.command == "poll-ha": print(json.dumps(poll_home_assistant()))
        elif args.command == "rebuild-stays":
            db = open_database()
            try:
                print(json.dumps(rebuild_stays(db)))
            finally:
                db.close()
        elif args.command == "learn-paths":
            from ..services.paths import learn_path_history
            db = open_database()
            try:
                print(json.dumps(learn_path_history(db), indent=2))
            finally:
                db.close()
        elif args.command == "import-csv":
            db = open_database()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(import_csv(db, file, args.source)))
            finally:
                db.close()
        elif args.command == "import-takeout":
            from ..services.imports import import_takeout
            db = open_database()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(import_takeout(db, file, args.source)))
            finally:
                db.close()
        elif args.command == "migrate-sqlite":
            url = config.database_url()
            if not url:
                raise ValueError("GEOLOGS_DATABASE_URL is required for migration")
            print(json.dumps(migrate_sqlite(args.file, url)))
    except (ValueError, OSError) as exc:
        print("geologs: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
