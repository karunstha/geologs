"""A full backup: everything in one person's archive, to clone it onto another GeoLogs server.

The file is gzipped JSON lines: a header, then for each table a line naming it and its columns,
followed by its rows as arrays. Fixes, places, corrections, reminders, learned routes, daily
summaries, settings and the tokens devices and apps sign in with go in; the account itself
(password, two-step codes, sessions) does not, nor stays, which are found again from the fixes.
"""
import collections
import gzip
import json
from datetime import datetime, timezone

from ..data.backup import TABLES
from .stays import process_stays
from .use_case import use_case

FORMAT = 1
# How often a restore says how far it has read.
PROGRESS_EVERY = 10_000


def is_backup(head):
    return head.lstrip().startswith('{"geologs_backup"')


@use_case
def export_backup(db, out):
    """Write the backup to the binary file `out`."""
    with gzip.open(out, "wt", encoding="utf-8") as text:
        text.write(json.dumps({"geologs_backup": FORMAT,
                               "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
        for table, names in TABLES.items():
            text.write(json.dumps({"table": table, "columns": names.split()}) + "\n")
            rows = db.backup.table_rows(table)
            for row in _with_timezone(db, rows) if table == "app_settings" else rows:
                text.write(json.dumps(row, separators=(",", ":")) + "\n")


def _with_timezone(db, rows):
    """The settings, with the timezone in use even when it was never saved (the server's
    GEOLOGS_TIMEZONE): another server's default would move every day's bounds."""
    saved = False
    for key, value in rows:
        saved = saved or key == "timezone"
        yield [key, value]
    if not saved:
        yield ["timezone", json.dumps(db.settings.get()["timezone"])]


def _sections(stream):
    """Each table in a backup as (table, columns, rows); a section's rows are read before the next."""
    header = json.loads(stream.readline() or "{}")
    if header.get("geologs_backup") != FORMAT:
        raise ValueError("This backup was made by a newer GeoLogs; update this server first")
    line = stream.readline()
    while line:
        section = json.loads(line)
        names = section.get("columns") if isinstance(section, dict) else None
        if not isinstance(names, list):
            raise ValueError("The backup is damaged: a table has no columns")
        ahead = []

        def rows():
            while True:
                row_line = stream.readline()
                if not row_line.startswith("["):
                    ahead.append(row_line)
                    return
                row = json.loads(row_line)
                if len(row) != len(names):
                    raise ValueError(f"The backup is damaged: a row of {section.get('table')} has the wrong columns")
                yield row

        yield section.get("table"), names, rows()
        line = ahead[0] if ahead else ""


@use_case
def restore_backup(db, open_text, progress=None):
    """Add a backup's contents to this archive, then find its stays. Fixes, places and the rest
    already here are kept, so restoring twice adds nothing. Returns the totals with what was added."""
    read = 0

    def counted(rows):
        nonlocal read
        for row in rows:
            read += 1
            if progress and read % PROGRESS_EVERY == 0:
                progress({"phase": "restoring", "read": read})
            yield row

    staged = {}
    with open_text() as stream:
        for table, names, rows in _sections(stream):
            if table in TABLES:
                staged[table] = db.backup.stage(table, names, counted(rows))
            else:
                collections.deque(rows, maxlen=0)  # a newer server's table: skipped
    added = db.backup.restore()
    sources = db.backup.staged_sources()
    # Keep the restore before finding stays, which can take a while for years of fixes.
    db.commit()
    if progress:
        progress({"phase": "rebuilding", "read": read, "inserted": added["points"]})
    # With nothing new, only what a restore cut short earlier is left to find (at no cost otherwise).
    process_stays(db, set(sources), rebuild=bool(added["point_observations"]))
    points = staged.get("points", 0)
    return {"inserted": added["points"], "duplicates": points - added["points"], "restored": added,
            "sources": sources}
