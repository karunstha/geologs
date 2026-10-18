"""Device tokens (ingest_tokens) and the latest request from each tracker app's device (ingest_devices)."""
from __future__ import annotations

from .repository import Repository


class DevicesRepository(Repository):
    def tokens(self):
        """Device tokens, newest first. The tokens themselves are never stored, only their hashes."""
        return self.rows("""SELECT id,name,hint,source,created_at,last_used_at,last_source
            FROM ingest_tokens WHERE user_id=? ORDER BY created_at DESC,id DESC""", (self.db.user_id,))

    def add_token(self, name, token_hash, hint, source, now):
        return self.execute("""INSERT INTO ingest_tokens(user_id,name,token_hash,hint,source,created_at)
            VALUES (?,?,?,?,?,?) RETURNING id""", (self.db.user_id, name, token_hash, hint, source, now)).fetchone()["id"]

    def delete_token(self, token_id):
        """False when the token was already revoked."""
        return bool(self.execute("DELETE FROM ingest_tokens WHERE id=? AND user_id=?",
                                 (int(token_id), self.db.user_id)).rowcount)

    def token_by_hash(self, token_hash):
        """The device token with this hash, whoever's it is (it says whose: user_id), or None."""
        return self.one("""SELECT t.id,t.name,t.source,t.user_id FROM ingest_tokens t JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND u.disabled_at IS NULL""", (token_hash,))

    def has_tokens(self):
        """Whether anyone has made a device token (with none anywhere, a loopback server takes any sender)."""
        return self.execute("SELECT 1 FROM ingest_tokens LIMIT 1").fetchone() is not None

    def note_token_use(self, token_id, source, now):
        self.execute("UPDATE ingest_tokens SET last_used_at=?,last_source=? WHERE id=? AND user_id=?",
                     (now, source, int(token_id), self.db.user_id))

    def record_attempt(self, kind, device, status, message, accepted, now):
        ok = 200 <= status < 300
        self.execute("""INSERT INTO ingest_devices
            (kind,device,last_attempt_at,last_status,last_message,last_success_at,received)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,kind,device) DO UPDATE SET
            last_attempt_at=excluded.last_attempt_at,last_status=excluded.last_status,
            last_message=excluded.last_message,
            last_success_at=COALESCE(excluded.last_success_at,ingest_devices.last_success_at),
            received=ingest_devices.received+excluded.received""",
            (kind, (device or "")[:80], now, int(status), (message or "")[:300] or None, now if ok else None, int(accepted)))

    def devices(self):
        return self.rows("SELECT * FROM ingest_devices ORDER BY last_attempt_at DESC")

    def delete_device(self, kind, device):
        """False when that device is already gone."""
        return bool(self.execute("DELETE FROM ingest_devices WHERE kind=? AND device=?",
                                 (str(kind), str(device or ""))).rowcount)
