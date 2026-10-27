"""Accounts: adding users, signing in and out, and the tokens that stand for a user."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from ..data import OWNER_ID
from ..domain.accounts import (API_TOKEN_PREFIX, SESSION_PREFIX, check_password, check_role, hash_password,
                               new_token, normal_email, password_matches, token_hash)
from ..domain.timing import parse_time
from .use_case import use_case

SESSION_DAYS = 30
# A session's expiry moves on at most this often, so each request is not a write.
SESSION_TOUCH = timedelta(hours=1)
# Sign-in attempts per address (email and client) within the window, before it waits.
SIGN_IN_ATTEMPTS, SIGN_IN_WINDOW = 10, 15 * 60
_attempts = {}
_attempts_lock = threading.Lock()


class TooManyAttempts(ValueError):
    """Sign-in refused for a while after too many tries."""


class CodeNeeded(ValueError):
    """The password was right; the account also needs its authenticator code."""


def _now():
    return datetime.now(timezone.utc)


def _stamp(at):
    return at.isoformat(timespec="seconds")


@use_case
def add_user(db, email, password, role="user", name=None):
    """A new account; the email is its sign-in name and must be unused."""
    email = normal_email(email)
    check_password(password)
    check_role(role)
    if db.users.by_email(email):
        raise ValueError("that email already has an account")
    user_id = db.users.add(email, hash_password(password), role, (name or "").strip()[:80] or None, parse_time(None))
    return db.users.get(user_id)


@use_case
def set_owner_sign_in(db, email, password):
    """Give the archive's first user (who signed in with GEOLOGS_TOKEN until now) an email and password."""
    email = normal_email(email)
    check_password(password)
    other = db.users.by_email(email)
    if other and other["id"] != OWNER_ID:
        raise ValueError("that email already has an account")
    db.users.set_email(OWNER_ID, email)
    db.users.set_password(OWNER_ID, hash_password(password))
    return db.users.get(OWNER_ID)


def _count_attempt(key, limit=SIGN_IN_ATTEMPTS, window=SIGN_IN_WINDOW):
    """Record an attempt (to sign in, or to have a reset link sent); TooManyAttempts once there
    were `limit` within `window` seconds."""
    now = time.monotonic()
    with _attempts_lock:
        recent = [at for at in _attempts.get(key, []) if now - at < window]
        if len(recent) >= limit:
            _attempts[key] = recent
            raise TooManyAttempts("too many attempts; try again in a few minutes")
        _attempts[key] = recent + [now]
        if len(_attempts) > 10_000:  # forget the quietest when an address list grows without end
            for stale in [k for k, times in _attempts.items() if all(now - at >= 3600 for at in times)]:
                del _attempts[stale]


@use_case
def sign_in(db, email, password, client="", code=None):
    """The user an email and password belong to, or ValueError; a wrong email and a wrong password
    read the same, and both count towards the limit. An account with two-factor sign-in also needs
    its authenticator's code (or a recovery code): CodeNeeded asks for it."""
    _count_attempt((str(email or "").strip().lower(), client))
    user = db.users.by_email(str(email or "").strip()) if email else None
    hashed = user["password_hash"] if user else None
    # Check a password even for an unknown email, so a response's timing does not tell them apart.
    matches = password_matches(str(password or ""), hashed or _DUMMY_HASH)
    if not user or not hashed or not matches or user["disabled_at"]:
        raise ValueError("wrong email or password")
    if not second_factor_passes(db, user["id"], code):
        raise CodeNeeded("enter the code from your authenticator app" if not code else "that code is wrong or was already used")
    db.users.note_login(user["id"], parse_time(None))
    return {key: value for key, value in user.items() if key != "password_hash"}


_DUMMY_HASH = hash_password("geologs-no-such-user")


@use_case
def start_session(db, user_id, user_agent=None):
    """A new signed-in browser for this user: the token goes in its cookie, shown only now."""
    token = new_token(SESSION_PREFIX)
    now = _now()
    db.users.add_session(token_hash(token), user_id, _stamp(now), _stamp(now + timedelta(days=SESSION_DAYS)), user_agent)
    return token


@use_case
def session_user(db, token):
    """The user a session cookie belongs to, or None; a session in use stays signed in."""
    if not token or not token.startswith(SESSION_PREFIX):
        return None
    now = _now()
    row = db.users.session(token_hash(token), _stamp(now))
    if row is None:
        return None
    if now - datetime.fromisoformat(row["last_seen_at"]) >= SESSION_TOUCH:
        db.users.touch_session(row["token_hash"], _stamp(now), _stamp(now + timedelta(days=SESSION_DAYS)))
    return {key: value for key, value in row.items() if key not in ("token_hash", "last_seen_at", "expires_at")}


@use_case
def end_session(db, token):
    if token and token.startswith(SESSION_PREFIX):
        db.users.delete_session(token_hash(token))


@use_case
def create_api_token(db, user_id, name):
    """A personal token for MCP clients and scripts, shown once."""
    name = str(name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("name is required (max 60 characters)")
    token = new_token(API_TOKEN_PREFIX)
    token_id = db.users.add_api_token(user_id, name, token_hash(token), token[:8] + "…" + token[-4:], parse_time(None))
    return {"id": token_id, "name": name, "token": token}


@use_case
def api_token_user(db, token):
    """The user a personal API token belongs to, or None."""
    if not token or not token.startswith(API_TOKEN_PREFIX):
        return None
    row = db.users.api_token(token_hash(token))
    if row is None:
        return None
    db.users.note_api_token_use(row["token_id"], parse_time(None))
    return {key: value for key, value in row.items() if key != "token_id"}


@use_case
def forget_expired_sessions(db):
    db.users.delete_expired_sessions(_stamp(_now()))


@use_case
def user_by_email(db, email):
    user = db.users.by_email(str(email or "").strip())
    return {key: value for key, value in user.items() if key != "password_hash"} if user else None


def owner(db):
    """The archive's first user: who GEOLOGS_TOKEN and the .env ingest token stand for."""
    return db.users.get(OWNER_ID)


def public_user(user):
    """What a client is told about a user: never a hash or a token."""
    if not user:
        return None
    return {key: user.get(key) for key in ("id", "email", "name", "role")}


# Links sent by email. Each is single-use and kept only as a hash; the raw link exists only in the
# message (and, for an invite, in the admin's answer, so it can be shared by hand without email).

RESET_HOURS, INVITE_DAYS, VERIFY_DAYS = 1, 7, 2
# "Forgot password" requests per address (email and client) within an hour.
RESET_REQUESTS = 5


def _expires(delta):
    return _stamp(_now() + delta)


@use_case
def request_password_reset(db, email, client=""):
    """Email a reset link to this address if it has an account. The answer is the same either way,
    so it cannot be used to find out who has one."""
    from . import mail
    address = str(email or "").strip()
    _count_attempt(("reset", address.lower(), client), RESET_REQUESTS, 3600)
    user = db.users.by_email(address) if address else None
    if not user or user["disabled_at"] or not mail.can_send():
        return {"ok": True}
    make_reset(db, user)
    return {"ok": True}


def make_reset(db, user):
    """A new reset link for a user (any earlier one is withdrawn), emailed when this server can."""
    from . import mail
    token = new_token("glr_")
    db.users.drop_email_tokens("reset", user_id=user["id"])
    db.users.add_email_token(token_hash(token), "reset", user["email"], user["id"], None, None, parse_time(None),
                             _expires(timedelta(hours=RESET_HOURS)))
    url = mail.link("reset=" + token)
    emailed = bool(url and mail.can_send() and mail.queue(
        db, "reset", user["email"], *mail.reset_message(user["name"], url, RESET_HOURS)))
    return {"link": url, "token": token, "emailed": emailed, "expires_at": _expires(timedelta(hours=RESET_HOURS))}


@use_case
def reset_password(db, token, password):
    """Set a new password from a reset link; every session of the account ends."""
    check_password(password)
    row = db.users.email_token(token_hash(str(token or "")), "reset", _stamp(_now()))
    if row is None or not db.users.use_email_token(row["token_hash"], parse_time(None)):
        raise ValueError("this link has expired or was already used; ask for a new one")
    db.users.set_password(row["user_id"], hash_password(password))
    db.users.mark_verified(row["user_id"], parse_time(None))  # the link reached that address
    db.users.delete_sessions(row["user_id"])
    return public_user(db.users.get(row["user_id"]))


@use_case
def invite(db, inviter_id, email, role="user"):
    """Invite someone to make an account: a link valid for INVITE_DAYS, emailed when this server
    can, and returned for the admin to share either way. A newer invite replaces an older one."""
    from . import mail
    email = normal_email(email)
    check_role(role)
    if db.users.by_email(email):
        raise ValueError("that email already has an account")
    token = new_token("gli_")
    db.users.drop_email_tokens("invite", email=email)
    db.users.add_email_token(token_hash(token), "invite", email, None, role, inviter_id, parse_time(None),
                             _expires(timedelta(days=INVITE_DAYS)))
    inviter = db.users.get(inviter_id) if inviter_id else None
    url = mail.link("invite=" + token)
    emailed = bool(url and mail.can_send() and mail.queue(
        db, "invite", email, *mail.invite_message((inviter or {}).get("name") or (inviter or {}).get("email"), url, INVITE_DAYS)))
    return {"email": email, "role": role, "token": token, "link": url, "emailed": emailed,
            "expires_at": _expires(timedelta(days=INVITE_DAYS))}


@use_case
def invite_details(db, token):
    """Who an invite is for (to show before they choose a password), or None."""
    row = db.users.email_token(token_hash(str(token or "")), "invite", _stamp(_now()))
    return {"email": row["email"], "role": row["role"], "expires_at": row["expires_at"]} if row else None


@use_case
def accept_invite(db, token, password, name=None):
    """Create the invited account, its address proven by the link; returns the new user."""
    check_password(password)
    row = db.users.email_token(token_hash(str(token or "")), "invite", _stamp(_now()))
    if row is None or not db.users.use_email_token(row["token_hash"], parse_time(None)):
        raise ValueError("this invite has expired or was already used; ask for a new one")
    if db.users.by_email(row["email"]):
        raise ValueError("that email already has an account")
    user_id = db.users.add_verified(row["email"], hash_password(password), row["role"] or "user",
                                    (name or "").strip()[:80] or None, parse_time(None))
    return public_user(db.users.get(user_id))


@use_case
def send_verification(db, user_id):
    """Email a link that confirms the user's address. False when this server cannot email."""
    from . import mail
    user = db.users.get(user_id)
    if not user or not user["email"] or user["email_verified_at"] or not mail.can_send():
        return False
    token = new_token("glv_")
    db.users.drop_email_tokens("verify", user_id=user_id)
    db.users.add_email_token(token_hash(token), "verify", user["email"], user_id, None, None, parse_time(None),
                             _expires(timedelta(days=VERIFY_DAYS)))
    mail.queue(db, "verify", user["email"], *mail.verify_message(user["name"], mail.link("verify=" + token), VERIFY_DAYS))
    return True


@use_case
def verify_email(db, token):
    row = db.users.email_token(token_hash(str(token or "")), "verify", _stamp(_now()))
    if row is None or not db.users.use_email_token(row["token_hash"], parse_time(None)):
        raise ValueError("this link has expired or was already used")
    user = db.users.get(row["user_id"])
    # A link to an address the account no longer has proves nothing.
    if not user or (user["email"] or "").lower() != row["email"].lower():
        raise ValueError("this link is for an address the account no longer has")
    db.users.mark_verified(row["user_id"], parse_time(None))
    return public_user(db.users.get(row["user_id"]))



# The account page: what a signed-in user may see and change about their own account.

def _session_id(token_hash_value):
    """How a browser's session is named on the account page: the start of its hash, which signs nothing in."""
    return token_hash_value[:16]


@use_case
def account(db, user_id, current_session=None):
    """The account page: the user, their signed-in browsers (this one marked), API tokens and shared links.
    `db` works as this user: shared links are theirs by row-level security."""
    from . import mail
    user = db.users.get(user_id)
    current = token_hash(current_session) if current_session else None
    sessions = [{"id": _session_id(row["token_hash"]), "user_agent": row["user_agent"], "created_at": row["created_at"],
                 "last_seen_at": row["last_seen_at"], "current": row["token_hash"] == current}
                for row in db.users.sessions(user_id)]
    return {"user": {**public_user(user), "email_verified": bool(user["email_verified_at"]),
                     "has_password": db.users.has_password(user_id), "created_at": user["created_at"]},
            "two_factor": two_factor_state(db, user_id),
            "sessions": sessions, "api_tokens": db.users.api_tokens(user_id), "shares": db.shares.mine(),
            "can_email": mail.can_send()}


@use_case
def rename(db, user_id, name):
    name = str(name or "").strip()
    if len(name) > 80:
        raise ValueError("name must be at most 80 characters")
    db.users.set_name(user_id, name or None)
    return public_user(db.users.get(user_id))


def _confirm_password(db, user_id, current):
    """The account's password must be given to change it or the email; one without a password
    (the owner, signed in with GEOLOGS_TOKEN) may set one."""
    stored = db.users.password_hash(user_id)
    if stored and not password_matches(str(current or ""), stored):
        raise ValueError("your current password is wrong")


@use_case
def change_password(db, user_id, current, password, current_session=None):
    """A new password; every other browser is signed out."""
    _count_attempt(("password", user_id))
    _confirm_password(db, user_id, current)
    check_password(password)
    db.users.set_password(user_id, hash_password(password))
    db.users.delete_sessions(user_id, keep=token_hash(current_session) if current_session else None)
    return {"ok": True}


@use_case
def change_email(db, user_id, email, current):
    """A new sign-in address, unconfirmed until its link is opened (emailed when this server can)."""
    _count_attempt(("password", user_id))
    _confirm_password(db, user_id, current)
    email = normal_email(email)
    other = db.users.by_email(email)
    if other and other["id"] != user_id:
        raise ValueError("that email already has an account")
    db.users.set_email(user_id, email)
    db.users.drop_email_tokens("verify", user_id=user_id)
    return {"user": public_user(db.users.get(user_id)), "verification_sent": send_verification(db, user_id)}


@use_case
def end_sessions(db, user_id, session_id=None, current_session=None):
    """Sign out one browser (session_id), or every one but this (none given)."""
    if session_id:
        if not db.users.delete_session_of(user_id, session_id):
            raise ValueError("that browser is already signed out")
    else:
        db.users.delete_sessions(user_id, keep=token_hash(current_session) if current_session else None)
    return {"ok": True}


@use_case
def revoke_api_token(db, user_id, token_id):
    if not db.users.delete_api_token(user_id, token_id):
        raise ValueError("that token was already revoked")
    return {"deleted": int(token_id)}



# Two-factor sign-in (TOTP): set up from the account page, then asked for after the password.

def second_factor_passes(db, user_id, code):
    """True without two-factor sign-in; else whether `code` is a fresh authenticator code or an
    unused recovery code (which is then spent)."""
    from ..domain.two_factor import matching_step, recovery_hash
    row = db.users.two_factor(user_id)
    if not row or not row["totp_enabled_at"]:
        return True
    if not code:
        return False
    step = matching_step(row["totp_secret"], code, time.time())
    if step is not None:
        return db.users.use_totp_step(user_id, step)
    codes = json.loads(row["recovery_codes"] or "[]")
    spent = recovery_hash(code)
    if spent in codes:
        db.users.set_recovery_codes(user_id, json.dumps([item for item in codes if item != spent]))
        return True
    return False


@use_case
def begin_two_factor(db, user_id):
    """A new secret for an authenticator app; two-factor sign-in starts once a code from it is confirmed."""
    from ..domain.two_factor import new_secret, setup_uri
    row = db.users.two_factor(user_id)
    if row and row["totp_enabled_at"]:
        raise ValueError("two-factor sign-in is already on; turn it off first to start again")
    secret = new_secret()
    db.users.set_two_factor(user_id, secret, None, None)
    user = db.users.get(user_id)
    return {"secret": secret, "uri": setup_uri(secret, user["email"] or f"user {user_id}")}


@use_case
def confirm_two_factor(db, user_id, code):
    """Turn two-factor sign-in on with a code from the app; returns recovery codes, shown once."""
    from ..domain.two_factor import matching_step, new_recovery_codes, recovery_hash
    row = db.users.two_factor(user_id)
    if not row or not row["totp_secret"] or row["totp_enabled_at"]:
        raise ValueError("start setting up two-factor sign-in first")
    step = matching_step(row["totp_secret"], code, time.time())
    if step is None:
        raise ValueError("that code is wrong; check the app's clock and try the newest code")
    codes = new_recovery_codes()
    db.users.set_two_factor(user_id, row["totp_secret"], parse_time(None), json.dumps([recovery_hash(c) for c in codes]))
    db.users.use_totp_step(user_id, step)
    return {"recovery_codes": codes}


@use_case
def stop_two_factor(db, user_id, current):
    """Turn two-factor sign-in off; the password must be given."""
    _count_attempt(("password", user_id))
    _confirm_password(db, user_id, current)
    db.users.set_two_factor(user_id, None, None, None)
    return {"ok": True}


@use_case
def two_factor_state(db, user_id):
    row = db.users.two_factor(user_id) or {}
    return {"enabled": bool(row.get("totp_enabled_at")),
            "recovery_codes_left": len(json.loads(row.get("recovery_codes") or "[]"))}


# Open sign-up, when an admin allows it (the admin portal; invites only by default).
SIGNUPS_PER_HOUR = 5


@use_case
def sign_up(db, email, password, name=None, client=""):
    """Make an account without an invite, where this server allows it; its address is checked by email."""
    if db.admin.setting("signup", "invite") != "open":
        raise PermissionError("this server is invite-only")
    _count_attempt(("signup", client), SIGNUPS_PER_HOUR, 3600)
    user = add_user(db, email, password, "user", name)
    send_verification(db, user["id"])
    return user
