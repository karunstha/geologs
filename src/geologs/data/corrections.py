"""Your corrections: travel modes (mode_overrides) and stays you added or removed (stay_edits)."""
from __future__ import annotations

from ..domain.timing import parse_time
from .repository import Repository


class CorrectionsRepository(Repository):
    def mode_overrides(self):
        """Travel mode corrections, each for one trip or area stay identified by source and time."""
        return self.rows("SELECT id,source,target,started_at,ended_at,mode,created_at,updated_at FROM mode_overrides ORDER BY started_at,id")

    def add_mode_override(self, source, target, started_at, ended_at, mode, now):
        return self.execute("""INSERT INTO mode_overrides(source,target,started_at,ended_at,mode,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?) RETURNING id""", (source, target, started_at, ended_at, mode, now, now)).fetchone()["id"]

    def update_mode_override(self, override_id, started_at, ended_at, mode, now):
        self.execute("UPDATE mode_overrides SET started_at=?,ended_at=?,mode=?,updated_at=? WHERE id=?",
                     (started_at, ended_at, mode, now, override_id))

    def delete_mode_override(self, override_id):
        self.execute("DELETE FROM mode_overrides WHERE id=?", (override_id,))

    def stay_edits(self, start=None, end=None, source=None):
        """Stays you added or removed, each kept by source and time window.

        Detected stays are rebuilt from raw samples, so an edit names a window
        rather than a stay id and is applied whenever stays are read.
        """
        clauses, args = [], []
        if start:
            clauses.append("ended_at>=?"); args.append(parse_time(start))
        if end:
            clauses.append("started_at<=?"); args.append(parse_time(end))
        if source:
            clauses.append("source=?"); args.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.rows("SELECT id,source,action,started_at,ended_at,lat,lon,created_at FROM stay_edits"
                         + where + " ORDER BY started_at,id", args)

    def add_stay_edit(self, source, action, started_at, ended_at, lat, lon, now):
        return self.execute("""INSERT INTO stay_edits(source,action,started_at,ended_at,lat,lon,created_at)
            VALUES (?,?,?,?,?,?,?) RETURNING id""", (source, action, started_at, ended_at, lat, lon, now)).fetchone()["id"]

    def delete_source(self, source):
        """Forget a tracker's corrections: they name its trips and stays."""
        self.execute("DELETE FROM stay_edits WHERE source=?", (source,))
        self.execute("DELETE FROM mode_overrides WHERE source=?", (source,))

    def for_watermark(self):
        """Every correction as it bears on a day's record, to tell when a stored one went stale."""
        return (self.rows("SELECT id,source,target,started_at,ended_at,mode FROM mode_overrides ORDER BY id"),
                self.rows("SELECT id,source,action,started_at,ended_at FROM stay_edits ORDER BY id"))

    def delete_stay_edit(self, edit_id):
        """False when there is no such edit."""
        return bool(self.execute("DELETE FROM stay_edits WHERE id=?", (int(edit_id),)).rowcount)
