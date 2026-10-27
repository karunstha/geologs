"""Shared replays: a day frozen as it was when its link was made, for anyone holding the link.

The rows are their owner's (row-level security). A link's holder reads one through shared(), which
names the link's token hash for that transaction alone; the shared_link policy shows that one row.
"""
from __future__ import annotations

import json

from .repository import Repository

COLUMNS = "id,hint,title,share_date,options,created_at,expires_at"


def _listed(row):
    return {**row, "options": json.loads(row["options"])}


class SharesRepository(Repository):
    def add(self, token_hash, hint, title, share_date, options, payload, now, expires_at):
        return _listed(self.one(f"""INSERT INTO shares(token_hash,hint,title,share_date,options,payload,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?) RETURNING {COLUMNS}""",
            (token_hash, hint, title, share_date, json.dumps(options), json.dumps(payload, separators=(",", ":")),
             now, expires_at)))

    def mine(self):
        """The current user's links, newest first, expired ones too."""
        return [_listed(row) for row in self.rows(f"SELECT {COLUMNS} FROM shares ORDER BY created_at DESC,id DESC")]

    def delete(self, share_id):
        return bool(self.execute("DELETE FROM shares WHERE id=?", (int(share_id),)).rowcount)

    def shared(self, token_hash, now):
        """The link with this hash, unless it has expired: its title, frozen replay, and whether it has a
        picture (`pictured`), or None."""
        self.db.conn.execute("SELECT set_config('geologs.share', ?, true)", (token_hash,))
        row = self.one("""SELECT title,payload,expires_at,preview IS NOT NULL AS pictured FROM shares
            WHERE token_hash=? AND (expires_at IS NULL OR expires_at>?)""", (token_hash, now))
        return row and {"title": row["title"], "expires_at": row["expires_at"], "pictured": row["pictured"],
                        **json.loads(row["payload"])}

    def set_preview(self, share_id, image):
        """The picture a chat app shows for the link (a JPEG its owner's browser drew; see save_preview)."""
        return bool(self.execute("UPDATE shares SET preview=? WHERE id=?", (image, int(share_id))).rowcount)

    def preview(self, token_hash, now):
        """The link's picture, unless it has expired or has none: JPEG bytes, or None."""
        self.db.conn.execute("SELECT set_config('geologs.share', ?, true)", (token_hash,))
        row = self.one("""SELECT preview FROM shares WHERE token_hash=? AND preview IS NOT NULL
            AND (expires_at IS NULL OR expires_at>?)""", (token_hash, now))
        return bytes(row["preview"]) if row else None
