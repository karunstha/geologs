"""The admin portal: accounts, invites, who may sign up, the audit log, and the server's health.

Admins see each user's counts (how much was recorded, and when last), never their locations: the
counts are written by the background worker while it works as that user (refresh_user_stats).
Every change an admin makes is logged.
"""
from datetime import datetime, timedelta, timezone

from .. import config
from ..data import OWNER_ID
from ..domain.accounts import check_role
from ..integrations import usage
from . import accounts, jobs, mail
from .use_case import use_case

SIGNUP_MODES = ("invite", "open")
# A user's counts are counted again once new fixes have come in (at once after none, else at most
# every RECOUNT_AFTER_NEW), and every STATS_EVERY regardless.
RECOUNT_AFTER_NEW = timedelta(minutes=5)
STATS_EVERY = timedelta(hours=1)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_admin(user):
    if not user or user.get("role") != "admin":
        raise PermissionError("only an admin may do that")


def _target(db, user_id):
    user = db.users.get(int(user_id))
    if user is None:
        raise ValueError("there is no such user")
    return user


@use_case
def users(db):
    return db.admin.users_with_stats(_now())


@use_case
def set_role(db, actor, user_id, role, client=None):
    check_role(role)
    target = _target(db, user_id)
    if target["role"] == "admin" and role != "admin" and not db.users.admin_count(target["id"]):
        raise ValueError("that is the last admin; make someone else an admin first")
    db.users.set_role(target["id"], role)
    db.admin.log(_now(), actor, "role", target, {"from": target["role"], "to": role}, client)
    return db.users.get(target["id"])


@use_case
def set_disabled(db, actor, user_id, disabled, client=None):
    """Disable an account (signing it out everywhere) or enable it again."""
    target = _target(db, user_id)
    if disabled:
        if target["id"] == actor["id"]:
            raise ValueError("you cannot disable your own account")
        if target["id"] == OWNER_ID:
            raise ValueError("the owner's account cannot be disabled")
        if target["role"] == "admin" and not db.users.admin_count(target["id"]):
            raise ValueError("that is the last admin")
        db.users.set_disabled(target["id"], _now())
        db.users.delete_sessions(target["id"])
    else:
        db.users.set_disabled(target["id"], None)
    db.admin.log(_now(), actor, "disable" if disabled else "enable", target, None, client)
    return db.users.get(target["id"])


@use_case
def reset_link(db, actor, user_id, client=None):
    """A password-reset link for a user: emailed to them when this server can, and given to the
    admin either way, to pass on."""
    target = _target(db, user_id)
    if not target["email"]:
        raise ValueError("that account has no email to reset a password for")
    made = accounts.make_reset(db, target)
    db.admin.log(_now(), actor, "reset link", target, {"emailed": made["emailed"]}, client)
    return made


@use_case
def delete_user(db, actor, user_id, confirm_email, client=None):
    """Erase an account and everything recorded for it. The admin types its email to confirm."""
    target = _target(db, user_id)
    if target["id"] == actor["id"]:
        raise ValueError("you cannot delete your own account here")
    if target["id"] == OWNER_ID:
        raise ValueError("the owner's account cannot be deleted")
    if (target["email"] or str(target["id"])).lower() != str(confirm_email or "").strip().lower():
        raise ValueError("type the account's email to confirm")
    db.users.delete(target["id"])
    db.admin.log(_now(), actor, "delete", target, None, client)
    return {"deleted": target["id"]}


@use_case
def invite(db, actor, email, role="user", client=None):
    made = accounts.invite(db, actor["id"], email, role)
    db.admin.log(_now(), actor, "invite", {"email": made["email"]}, {"role": role, "emailed": made["emailed"]}, client)
    return made


@use_case
def revoke_invite(db, actor, email, client=None):
    db.users.drop_email_tokens("invite", email=str(email or "").strip())
    db.admin.log(_now(), actor, "revoke invite", {"email": email}, None, client)
    return {"ok": True}


def signup_mode(db):
    return db.admin.setting("signup", "invite")


@use_case
def set_signup_mode(db, actor, mode, client=None):
    if mode not in SIGNUP_MODES:
        raise ValueError("sign-up must be invite or open")
    db.admin.set_setting("signup", mode)
    db.admin.log(_now(), actor, "sign-up", None, {"mode": mode}, client)
    return {"signup": mode}


@use_case
def audit(db, limit=200):
    return db.admin.audit(min(max(int(limit), 1), 1000))


@use_case
def clear_failures(db, actor, client=None):
    db.admin.clear_failures()
    db.admin.log(_now(), actor, "clear failed attempts", None, None, client)
    return {"ok": True}


@use_case
def health(db):
    """The server at a glance: email, failed tracker requests, background jobs, the database,
    and how often outside services were asked."""
    return {
        "signup": signup_mode(db),
        "email": {"can_send": mail.can_send(), "mail_server": config.smtp() is not None,
                  "public_url": config.public_url(), "recent": db.outbox.recent(25)},
        "failed_attempts": db.admin.failures(),
        "jobs": jobs.report(),
        "database": db.admin.database_size(),
        "outside_services": usage.report(),
    }


@use_case
def refresh_user_stats(db, force=False):
    """Count the current user's archive for the admin portal (the background worker calls this as
    each user in turn); at most every STATS_EVERY unless forced."""
    counted = db.admin.stats(db.user_id)
    if not force and counted:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(counted["updated_at"])
        # Whether fixes came in since: the newest each tracker's stay detection has read (a few rows).
        newest = db.stays.newest_read()
        fresh = newest is None or (counted["last_fix"] is not None and newest <= counted["last_fix"])
        if age < STATS_EVERY and (fresh or (counted["fixes"] and age < RECOUNT_AFTER_NEW)):
            return None
    first, last = db.points.span()
    stats = {"fixes": db.points.count(), "sources": len(db.points.sources()), "first_fix": first, "last_fix": last,
             "stays": db.stays.count(), "places": len(db.places.saved())}
    db.admin.save_stats(db.user_id, stats, _now())
    return stats


@use_case
def note_failed_attempt(db, kind, device, status, message, client):
    """A tracker request with a wrong token: whose phone it is cannot be told, so admins see it."""
    db.admin.note_failure(kind, device, status, message, client, _now())
