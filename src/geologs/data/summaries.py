"""Stored daily summaries (daily_summaries): one record per local date, timezone and source."""
from __future__ import annotations

import json

from ..domain.timing import parse_time
from .repository import Repository


class SummariesRepository(Repository):
    def get(self, date, timezone_name, source=None, watermark=None):
        args = [date, timezone_name, source or ""]
        where = "summary_date=? AND timezone=? AND source=?"
        if watermark is not None:
            where += " AND watermark=?"
            args.append(watermark)
        row = self.execute("SELECT summary FROM daily_summaries WHERE " + where, args).fetchone()
        return json.loads(row["summary"]) if row else None

    def save(self, value, watermark, brief=None):
        now = parse_time(None)
        totals = value["totals"]
        self.execute("""INSERT INTO daily_summaries
            (summary_date,timezone,source,watermark,points,distance_km,trip_count,place_count,
             reminder_count,summary,brief,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(summary_date,timezone,source) DO UPDATE SET
            watermark=excluded.watermark,points=excluded.points,distance_km=excluded.distance_km,
            trip_count=excluded.trip_count,place_count=excluded.place_count,
            reminder_count=excluded.reminder_count,summary=excluded.summary,brief=excluded.brief,
            updated_at=excluded.updated_at""",
            (value["date"], value["timezone"], value.get("source") or "", watermark,
             totals["raw_fixes"], totals["distance_km"], totals["trips"], totals["places"],
             totals["reminders_triggered"], json.dumps(value, separators=(",", ":")),
             json.dumps(brief, separators=(",", ":"), ensure_ascii=False) if brief is not None else None, now, now))
        return value

    def delete(self, date, timezone_name):
        self.execute("DELETE FROM daily_summaries WHERE summary_date=? AND timezone=? AND source=''", (date, timezone_name))

    def catalog(self, timezone_name, start=None, end=None, limit=90):
        """The stored record of each date with data, newest first: one timezone, all sources."""
        clauses, args = ["timezone=?", "source=''", "points>0"], [timezone_name]
        if start:
            clauses.append("summary_date>=?"); args.append(str(start))
        if end:
            clauses.append("summary_date<=?"); args.append(str(end))
        where = " WHERE " + " AND ".join(clauses)
        args.append(min(max(int(limit), 1), 1000))
        return self.rows("""SELECT summary_date AS date,timezone,NULLIF(source,'') AS source,
            points,distance_km,trip_count,place_count,reminder_count,created_at,updated_at
            FROM daily_summaries""" + where + " ORDER BY summary_date DESC,source LIMIT ?", args)

    def keep_canonical(self, timezone_name):
        """Drop stored summaries for another timezone or a single source."""
        self.execute("DELETE FROM daily_summaries WHERE timezone<>? OR source<>''", (timezone_name,))

    def record(self, date, timezone_name):
        """The stored record of a date with data, as stored (summary and brief still JSON text), or None."""
        row = self.execute("""SELECT summary_date,timezone,watermark,summary,brief,created_at,updated_at FROM daily_summaries
            WHERE summary_date=? AND timezone=? AND source='' AND points>0""", (date, timezone_name)).fetchone()
        return dict(row) if row else None

    def updated_at(self, date, timezone_name):
        row = self.execute("""SELECT updated_at FROM daily_summaries
            WHERE summary_date=? AND timezone=? AND source=''""", (date, timezone_name)).fetchone()
        return row["updated_at"] if row else None

    def dates_between(self, timezone_name, first, last):
        return [row["summary_date"] for row in self.rows("""SELECT summary_date FROM daily_summaries
            WHERE timezone=? AND source='' AND summary_date>=? AND summary_date<=? ORDER BY summary_date""",
            (timezone_name, first, last))]
