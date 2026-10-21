"""Entry point and source adapters."""
import argparse
import json
import os
import sys

from .. import config
from ..services import migrate_sqlite, open_database
from ..services.accounts import (OWNER_ID, add_user as add_user_account, create_api_token, invite as invite_someone,
                                 public_user, set_owner_sign_in, user_by_email)
from ..services.imports import import_csv
from ..services.stays import rebuild_stays
from .http import serve


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


def ask_password():
    """A new password: GEOLOGS_NEW_PASSWORD when set (for scripts), else asked for twice."""
    if os.environ.get("GEOLOGS_NEW_PASSWORD"):
        return os.environ["GEOLOGS_NEW_PASSWORD"]
    import getpass
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise ValueError("the passwords differ")
    return first


def main():
    load_env_file()
    parser = argparse.ArgumentParser(prog="geologs")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="run local browser, REST API and MCP HTTP")
    web.add_argument("--host", default=config.host())
    web.add_argument("--port", type=int, default=config.port())
    # Commands about one person's archive work on the owner's unless --user names someone else.
    one_user = argparse.ArgumentParser(add_help=False)
    one_user.add_argument("--user", metavar="EMAIL", help="the account to work on (the owner by default)")
    sub.add_parser("rebuild-stays", parents=[one_user], help="rebuild observed stays after historical import or algorithm changes")
    sub.add_parser("learn-paths", parents=[one_user], help="learn known paths from every well-recorded trip and report the library")
    imp = sub.add_parser("import-csv", parents=[one_user], help="import CSV with timestamp,lat,lon and optional accuracy_m,speed_mps")
    imp.add_argument("file")
    imp.add_argument("--source", default="csv")
    takeout = sub.add_parser("import-takeout", parents=[one_user], help="stream classic Google Takeout Records.json locations")
    takeout.add_argument("file")
    takeout.add_argument("--source", default="google_takeout")
    any_file = sub.add_parser("import", parents=[one_user],
                              help="import a CSV, Takeout Records.json or Google Timeline export, told apart by its contents")
    any_file.add_argument("file")
    any_file.add_argument("--source", help="the tracker name to file it under (by default csv, google_takeout or google_timeline)")
    add_user = sub.add_parser("add-user", help="create an account (asks for its password)")
    add_user.add_argument("email")
    add_user.add_argument("--admin", action="store_true", help="may run the admin portal")
    add_user.add_argument("--name")
    owner_sign_in = sub.add_parser("owner-sign-in", help="give the archive's first account an email and password")
    owner_sign_in.add_argument("email")
    invite_cmd = sub.add_parser("invite", help="invite someone to make an account (prints the link; emails it when mail is set up)")
    invite_cmd.add_argument("email")
    invite_cmd.add_argument("--admin", action="store_true")
    add_token = sub.add_parser("add-token", parents=[one_user], help="a personal API token for MCP clients and scripts")
    add_token.add_argument("name")
    migrate = sub.add_parser("migrate-sqlite", help="copy an existing SQLite archive to an empty PostGIS database")
    migrate.add_argument("file")
    args = parser.parse_args()

    def archive():
        """The archive, working for --user (when the command takes one) or else the owner."""
        db = open_database()
        email = getattr(args, "user", None)
        if email:
            user = user_by_email(db, email)
            if user is None:
                db.close()
                raise ValueError(f"no account has the email {email}")
            db.as_user(user["id"])
        return db
    try:
        if args.command == "serve": serve(args.host, args.port)
        elif args.command == "rebuild-stays":
            db = archive()
            try:
                print(json.dumps(rebuild_stays(db)))
            finally:
                db.close()
        elif args.command == "learn-paths":
            from ..services.paths import learn_path_history
            db = archive()
            try:
                print(json.dumps(learn_path_history(db), indent=2))
            finally:
                db.close()
        elif args.command == "import-csv":
            db = archive()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(import_csv(db, file, args.source)))
            finally:
                db.close()
        elif args.command == "import-takeout":
            from ..services.imports import import_takeout
            db = archive()
            try:
                with open(args.file, encoding="utf-8-sig") as file:
                    print(json.dumps(import_takeout(db, file, args.source)))
            finally:
                db.close()
        elif args.command == "import":
            from ..services.imports import import_file
            db = archive()
            try:
                print(json.dumps(import_file(db, args.file, args.source)))
            finally:
                db.close()
        elif args.command in ("add-user", "owner-sign-in"):
            password = ask_password()
            db = open_database()
            try:
                user = (add_user_account(db, args.email, password, "admin" if args.admin else "user", args.name)
                        if args.command == "add-user" else set_owner_sign_in(db, args.email, password))
                print(json.dumps(public_user(user)))
            finally:
                db.close()
        elif args.command == "invite":
            db = open_database()
            try:
                made = invite_someone(db, OWNER_ID, args.email, "admin" if args.admin else "user")
                if not made["link"]:
                    print("geologs: set GEOLOGS_PUBLIC_URL to make invite links; the invite token is "
                          + made["token"], file=sys.stderr)
                print(json.dumps({key: made[key] for key in ("email", "role", "link", "emailed", "expires_at")}))
            finally:
                db.close()
        elif args.command == "add-token":
            db = archive()
            try:
                print(json.dumps(create_api_token(db, db.user_id, args.name)))
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
