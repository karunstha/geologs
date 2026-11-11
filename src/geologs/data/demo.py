"""The demo archive: the one fictional account every visitor of a demo server (GEOLOGS_DEMO) looks at.

Like the accounts themselves, these are read before anyone is signed in, so they sit outside
row-level security. Erasing a demo's user erases everything in it (each table's user_id cascades).
"""
from __future__ import annotations

from .repository import Repository


class DemoRepository(Repository):
    def add(self, name, now):
        """A new demo account, disabled until it is filled (so visitors and background work skip it)."""
        user_id = self.execute("""INSERT INTO users(name,role,created_at,disabled_at)
            VALUES (?,'user',?,?) RETURNING id""", (name, now, now)).fetchone()["id"]
        self.execute("INSERT INTO demo_archives(user_id,created_at) VALUES (?,?)", (user_id, now))
        return user_id

    def mark_ready(self, user_id, now):
        self.execute("UPDATE demo_archives SET ready_at=? WHERE user_id=?", (now, int(user_id)))
        self.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (int(user_id),))

    def current(self):
        """The newest filled demo ({user_id, ready_at}), or None while the first is being filled."""
        return self.one("""SELECT user_id,ready_at FROM demo_archives WHERE ready_at IS NOT NULL
            ORDER BY ready_at DESC, user_id DESC LIMIT 1""")

    def is_demo(self, user_id):
        return self.one("SELECT 1 AS yes FROM demo_archives WHERE user_id=?", (int(user_id),)) is not None

    def replace(self, user_id):
        """Make `user_id` the demo: browsers looking at an older filled one move over to it, without
        being signed out. Returns the older ones, to erase."""
        older = [row["user_id"] for row in self.rows(
            "SELECT user_id FROM demo_archives WHERE ready_at IS NOT NULL AND user_id<>?", (int(user_id),))]
        if older:
            self.execute("UPDATE sessions SET user_id=? WHERE user_id = ANY(?)", (int(user_id), older))
        return older

    def abandoned(self, before):
        """Demos still being filled since before `before`: the server stopped while filling them."""
        return [row["user_id"] for row in self.rows(
            "SELECT user_id FROM demo_archives WHERE ready_at IS NULL AND created_at<?", (before,))]
