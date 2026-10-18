"""What admins see of the server: its settings, a log of what admins did, each user's counts
(never their locations), and tracker requests that belonged to no one."""
from __future__ import annotations

import json

from .repository import Repository


class AdminRepository(Repository):
    # Server settings (such as who may sign up), apart from each user's own settings.

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM server_settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.execute("""INSERT INTO server_settings(key,value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, json.dumps(value)))

    # The audit log: every admin action, with who did it and to whom.

    def log(self, at, actor, action, target=None, details=None, client=None):
        self.execute("""INSERT INTO audit_log(at,actor_id,actor_email,action,target_id,target_email,details,client)
            VALUES (?,?,?,?,?,?,?,?)""",
                     (at, (actor or {}).get("id"), (actor or {}).get("email"), action, (target or {}).get("id"),
                      (target or {}).get("email"), json.dumps(details) if details else None, client))

    def audit(self, limit=200):
        rows = self.rows("""SELECT id,at,actor_id,actor_email,action,target_id,target_email,details,client
            FROM audit_log ORDER BY at DESC,id DESC LIMIT ?""", (int(limit),))
        for row in rows:
            row["details"] = json.loads(row["details"]) if row["details"] else None
        return rows

    # Each user's counts, written by the background worker while it works as that user.

    def save_stats(self, user_id, stats, now):
        self.execute("""INSERT INTO user_stats(user_id,fixes,sources,first_fix,last_fix,stays,places,updated_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET fixes=excluded.fixes,
            sources=excluded.sources,first_fix=excluded.first_fix,last_fix=excluded.last_fix,stays=excluded.stays,
            places=excluded.places,updated_at=excluded.updated_at""",
                     (int(user_id), stats["fixes"], stats["sources"], stats["first_fix"], stats["last_fix"],
                      stats["stays"], stats["places"], now))

    def stats(self, user_id):
        return self.one("SELECT fixes,last_fix,updated_at FROM user_stats WHERE user_id=?", (int(user_id),))

    def users_with_stats(self, now):
        """Every account, its sessions and tokens counted, and the counts the worker last wrote."""
        return self.rows("""SELECT u.id,u.email,u.name,u.role,u.created_at,u.email_verified_at,u.last_login_at,
                u.disabled_at,u.password_hash IS NOT NULL AS has_password,u.totp_enabled_at IS NOT NULL AS two_factor,
                s.fixes,s.sources,s.first_fix,s.last_fix,s.stays,s.places,s.updated_at AS counted_at,
                (SELECT COUNT(*) FROM sessions x WHERE x.user_id=u.id AND x.expires_at>?) AS sessions,
                (SELECT COUNT(*) FROM ingest_tokens t WHERE t.user_id=u.id) AS device_tokens,
                (SELECT COUNT(*) FROM api_tokens a WHERE a.user_id=u.id) AS api_tokens
            FROM users u LEFT JOIN user_stats s ON s.user_id=u.id ORDER BY u.id""", (now,))

    # Tracker requests whose token matched no one: shown to admins, never in a user's device list.

    def note_failure(self, kind, device, status, message, client, now):
        self.execute("""INSERT INTO ingest_failures(kind,device,status,message,first_at,last_at,client)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,device) DO UPDATE SET status=excluded.status,
            message=excluded.message,last_at=excluded.last_at,client=excluded.client,
            attempts=ingest_failures.attempts+1""",
                     (kind, (device or "")[:80], int(status), (message or "")[:200] or None, now, now, client))

    def failures(self, limit=100):
        return self.rows("SELECT * FROM ingest_failures ORDER BY last_at DESC LIMIT ?", (int(limit),))

    def clear_failures(self):
        self.execute("DELETE FROM ingest_failures")

    def database_size(self):
        """The database's size, and its largest tables, in bytes."""
        total = self.execute("SELECT pg_database_size(current_database()) AS size").fetchone()["size"]
        tables = self.rows("""SELECT c.relname AS name,pg_total_relation_size(c.oid) AS size FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r'
            ORDER BY size DESC LIMIT 8""")
        return {"total": total, "tables": tables}
