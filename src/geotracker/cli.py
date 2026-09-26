"""Entry point and source adapters."""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .core import Store, deliver_webhook
from .http import serve


def load_env_file():
    """Use local .env values when the process environment has no override."""
    path = Path(os.environ.get("GEOTRACKER_ENV_FILE", ".env"))
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
    base = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    entity = os.environ.get("HA_ENTITY", "")
    if not base or not token or not entity:
        raise ValueError("HA_URL, HA_TOKEN and HA_ENTITY are required")
    req = urllib.request.Request(base + "/api/states/" + entity, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=12) as response:
        state = json.load(response)
    attrs = state.get("attributes") or {}
    if attrs.get("latitude") is None or attrs.get("longitude") is None:
        raise ValueError("Home Assistant entity has no latitude/longitude attributes")
    # Every poll is a received sample. Keep the Home Assistant update as the
    # canonical event ID, but date each sample at the time it was received.
    polled_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    point = {"source": "home_assistant:" + entity, "external_id": state.get("last_updated"),
             "timestamp": polled_at,
             "lat": attrs["latitude"], "lon": attrs["longitude"],
             "accuracy_m": attrs.get("gps_accuracy")}
    endpoint = os.environ.get("GEOTRACKER_URL")
    if endpoint:
        headers = {"Content-Type": "application/json"}
        api_token = os.environ.get("GEOTRACKER_TOKEN")
        if api_token:
            headers["Authorization"] = "Bearer " + api_token
        request = urllib.request.Request(endpoint.rstrip("/") + "/api/points", json.dumps(point).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)
    store = Store()
    try:
        result = store.ingest(point)
        deliver_webhook(store, result["events"])
        return result
    finally:
        store.close()


def main():
    load_env_file()
    parser = argparse.ArgumentParser(prog="geotracker")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="run local browser, REST API and MCP HTTP")
    web.add_argument("--host", default=os.environ.get("GEOTRACKER_HOST", "127.0.0.1"))
    web.add_argument("--port", type=int, default=int(os.environ.get("GEOTRACKER_PORT", "8765")))
    sub.add_parser("poll-ha", help="poll a Home Assistant device tracker once (run from cron)")
    sub.add_parser("rebuild-stays", help="rebuild observed stays after historical import or algorithm changes")
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
            from .analysis import process_stays
            store = Store()
            try:
                result = process_stays(store, rebuild=True)
                store.db.commit()
                print(json.dumps(result))
            finally:
                store.close()
        elif args.command == "import-csv":
            store = Store()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(store.import_csv(file, args.source)))
            finally:
                store.close()
        elif args.command == "import-takeout":
            from .imports import import_takeout
            store = Store()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(import_takeout(store, file, args.source)))
            finally:
                store.close()
        elif args.command == "migrate-sqlite":
            from .migration import migrate_sqlite
            url = os.environ.get("GEOTRACKER_DATABASE_URL")
            if not url:
                raise ValueError("GEOTRACKER_DATABASE_URL is required for migration")
            print(json.dumps(migrate_sqlite(args.file, url)))
    except (ValueError, OSError) as exc:
        print("geotracker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
