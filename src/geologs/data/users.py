"""Accounts (users), their signed-in browsers (sessions) and personal API tokens (api_tokens).

These are read before anyone is known to be signed in, so they sit outside row-level security;
every query here names the user it is about.
"""
from __future__ import annotations

from .repository import Repository

USER_COLUMNS = "id,email,name,role,created_at,email_verified_at,last_login_at,disabled_at"


class UsersRepository(Repository):
    def ids(self):
        """Every user, disabled or not, oldest first."""
        return [row["id"] for row in self.rows("SELECT id FROM users ORDER BY id")]

    def active_ids(self):
        return [row["id"] for row in self.rows("SELECT id FROM users WHERE disabled_at IS NULL ORDER BY id")]

    def get(self, user_id):
        return self.one(f"SELECT {USER_COLUMNS} FROM users WHERE id=?", (int(user_id),))

    def by_email(self, email):
        """The user with this email (any case), with their password hash, or None."""
        return self.one(f"SELECT {USER_COLUMNS},password_hash FROM users WHERE lower(email)=lower(?)", (email,))

    def all(self):
        return self.rows(f"SELECT {USER_COLUMNS} FROM users ORDER BY id")

    def add(self, email, password_hash, role, name, now):
        return self.execute("""INSERT INTO users(email,password_hash,role,name,created_at)
            VALUES (?,?,?,?,?) RETURNING id""", (email, password_hash, role, name, now)).fetchone()["id"]

    def set_password(self, user_id, password_hash):
        self.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, int(user_id)))

    def has_password(self, user_id):
        return bool(self.execute("SELECT password_hash IS NOT NULL AS has FROM users WHERE id=?", (int(user_id),)).fetchone()["has"])

    def password_hash(self, user_id):
        return self.execute("SELECT password_hash FROM users WHERE id=?", (int(user_id),)).fetchone()["password_hash"]

    def set_name(self, user_id, name):
        self.execute("UPDATE users SET name=? WHERE id=?", (name, int(user_id)))

    def set_email(self, user_id, email):
        self.execute("UPDATE users SET email=?,email_verified_at=NULL WHERE id=?", (email, int(user_id)))

    def set_role(self, user_id, role):
        self.execute("UPDATE users SET role=? WHERE id=?", (role, int(user_id)))

    def admin_count(self, excluding=None):
        """Enabled admins (other than `excluding`)."""
        return self.execute("""SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled_at IS NULL
            AND id IS DISTINCT FROM ?""", (excluding,)).fetchone()["n"]

    def delete(self, user_id):
        """Erase a user: every row of theirs goes with them (each table's user_id cascades)."""
        return bool(self.execute("DELETE FROM users WHERE id=?", (int(user_id),)).rowcount)

    # Two-factor sign-in: a TOTP secret, the last time step used (so a code works once), and
    # hashed one-time recovery codes.

    def two_factor(self, user_id):
        return self.one("""SELECT totp_secret,totp_enabled_at,totp_last_step,recovery_codes FROM users WHERE id=?""",
                        (int(user_id),))

    def set_two_factor(self, user_id, secret, enabled_at, recovery_codes):
        self.execute("""UPDATE users SET totp_secret=?,totp_enabled_at=?,totp_last_step=NULL,recovery_codes=?
            WHERE id=?""", (secret, enabled_at, recovery_codes, int(user_id)))

    def use_totp_step(self, user_id, step):
        """Record a code's time step as used; False when it (or a later one) already was."""
        return bool(self.execute("""UPDATE users SET totp_last_step=? WHERE id=?
            AND (totp_last_step IS NULL OR totp_last_step<?)""", (step, int(user_id), step)).rowcount)

    def set_recovery_codes(self, user_id, recovery_codes):
        self.execute("UPDATE users SET recovery_codes=? WHERE id=?", (recovery_codes, int(user_id)))

    def note_login(self, user_id, now):
        self.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, int(user_id)))

    def set_disabled(self, user_id, now):
        """Disable a user (now) or enable them again (None)."""
        self.execute("UPDATE users SET disabled_at=? WHERE id=?", (now, int(user_id)))

    # Sessions: a signed-in browser. Only the token's hash is kept.

    def add_session(self, token_hash, user_id, now, expires_at, user_agent):
        self.execute("""INSERT INTO sessions(token_hash,user_id,created_at,last_seen_at,expires_at,user_agent)
            VALUES (?,?,?,?,?,?)""", (token_hash, int(user_id), now, now, expires_at, (user_agent or "")[:200] or None))

    def session(self, token_hash, now):
        """The live session with this hash and its enabled user, or None."""
        return self.one(f"""SELECT s.token_hash,s.last_seen_at,s.expires_at,{', '.join('u.' + c for c in USER_COLUMNS.split(','))}
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.disabled_at IS NULL""", (token_hash, now))

    def touch_session(self, token_hash, now, expires_at):
        self.execute("UPDATE sessions SET last_seen_at=?,expires_at=? WHERE token_hash=?", (now, expires_at, token_hash))

    def delete_session(self, token_hash):
        self.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def sessions(self, user_id):
        return self.rows("""SELECT token_hash,created_at,last_seen_at,expires_at,user_agent FROM sessions
            WHERE user_id=? ORDER BY last_seen_at DESC""", (int(user_id),))

    def delete_session_of(self, user_id, prefix):
        """Sign out one of a user's browsers, named by the start of its hash (as sessions() lists it)."""
        return bool(self.execute("DELETE FROM sessions WHERE user_id=? AND left(token_hash,16)=?",
                                 (int(user_id), str(prefix)[:16])).rowcount)

    def delete_sessions(self, user_id, keep=None):
        """Sign a user out everywhere (but in the browser holding `keep`)."""
        self.execute("DELETE FROM sessions WHERE user_id=? AND token_hash IS DISTINCT FROM ?", (int(user_id), keep))

    def delete_expired_sessions(self, now):
        self.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))

    # Personal API tokens (MCP clients, scripts): they read and write as their user.

    def add_api_token(self, user_id, name, token_hash, hint, now):
        return self.execute("""INSERT INTO api_tokens(user_id,name,token_hash,hint,created_at)
            VALUES (?,?,?,?,?) RETURNING id""", (int(user_id), name, token_hash, hint, now)).fetchone()["id"]

    def api_token(self, token_hash):
        """The token with this hash and its enabled user, or None."""
        return self.one(f"""SELECT t.id AS token_id,{', '.join('u.' + c for c in USER_COLUMNS.split(','))}
            FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND u.disabled_at IS NULL""",
                        (token_hash,))

    def note_api_token_use(self, token_id, now):
        self.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, int(token_id)))

    def api_tokens(self, user_id):
        return self.rows("""SELECT id,name,hint,created_at,last_used_at FROM api_tokens
            WHERE user_id=? ORDER BY created_at DESC,id DESC""", (int(user_id),))

    def delete_api_token(self, user_id, token_id):
        return bool(self.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (int(token_id), int(user_id))).rowcount)

    # Links sent by email (invites, password resets, address checks): single-use, kept as hashes.

    def add_email_token(self, token_hash, kind, email, user_id, role, invited_by, now, expires_at):
        self.execute("""INSERT INTO email_tokens(token_hash,kind,email,user_id,role,invited_by,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?)""", (token_hash, kind, email, user_id, role, invited_by, now, expires_at))

    def email_token(self, token_hash, kind, now):
        """An unused, unexpired link of this kind, or None."""
        return self.one("""SELECT token_hash,kind,email,user_id,role,invited_by,created_at,expires_at FROM email_tokens
            WHERE token_hash=? AND kind=? AND used_at IS NULL AND expires_at>?""", (token_hash, kind, now))

    def use_email_token(self, token_hash, now):
        """Mark a link used; False when it already was (two clicks at once)."""
        return bool(self.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                                 (now, token_hash)).rowcount)

    def drop_email_tokens(self, kind, email=None, user_id=None):
        """Withdraw the unused links of a kind for an address or a user (a newer one replaces them)."""
        if user_id is not None:
            self.execute("DELETE FROM email_tokens WHERE kind=? AND user_id=? AND used_at IS NULL", (kind, int(user_id)))
        else:
            self.execute("DELETE FROM email_tokens WHERE kind=? AND lower(email)=lower(?) AND used_at IS NULL", (kind, email))

    def invites(self, now):
        """Invites not yet accepted or expired, newest first."""
        return self.rows("""SELECT email,role,invited_by,created_at,expires_at FROM email_tokens
            WHERE kind='invite' AND used_at IS NULL AND expires_at>? ORDER BY created_at DESC""", (now,))

    def mark_verified(self, user_id, now):
        self.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at,?) WHERE id=?", (now, int(user_id)))

    def add_verified(self, email, password_hash, role, name, now):
        """A user whose address is already proven (they opened an invite sent to it)."""
        return self.execute("""INSERT INTO users(email,password_hash,role,name,created_at,email_verified_at)
            VALUES (?,?,?,?,?,?) RETURNING id""", (email, password_hash, role, name, now, now)).fetchone()["id"]
