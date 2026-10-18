"""Email: messages are queued in the outbox with the work that wrote them, and sent apart from it.

A request never waits on the mail server: the server's mail thread sends what is due (woken as
soon as something is queued) and tries a failed message again later, up to MAX_ATTEMPTS times.
"""
import threading
from datetime import datetime, timedelta, timezone

from .. import config
from ..integrations import email
from .use_case import use_case

MAX_ATTEMPTS = 8
# Sent and failed messages are kept (without their text) this long, for the admin portal.
KEEP_SENT = timedelta(days=30)
waiting = threading.Event()


def _stamp(at):
    return at.isoformat(timespec="seconds")


def can_send():
    """Whether this server can email: a mail server, and a public address for the links."""
    return email.configured() and config.public_url() is not None


def link(fragment):
    """A link into the browser app, such as /#reset=… (None without GEOLOGS_PUBLIC_URL)."""
    base = config.public_url()
    return f"{base}/#{fragment}" if base else None


def queue(db, kind, to_address, subject, body):
    """Queue a message with the caller's work; it is sent once that is saved. Nothing is queued
    when this server cannot email."""
    if not email.configured():
        return None
    message_id = db.outbox.add(kind, to_address, subject, body, _stamp(datetime.now(timezone.utc)))
    waiting.set()
    return message_id


@use_case
def deliver_due(db, now=None):
    """Send what is due; a failure waits longer each time (1, 2, 4… minutes) before the next try."""
    now = now or datetime.now(timezone.utc)
    sent = failed = 0
    for message in db.outbox.take_due(_stamp(now)):
        try:
            email.send(message["to_address"], message["subject"], message["body"])
        except Exception as exc:
            attempts = message["attempts"] + 1
            give_up = attempts >= MAX_ATTEMPTS
            db.outbox.failed(message["id"], f"{type(exc).__name__}: {exc}",
                             _stamp(now + timedelta(minutes=2 ** (attempts - 1))), _stamp(now) if give_up else None)
            failed += 1
            print(f"GeoLogs mail to {message['to_address']} ({message['kind']}): {exc}", flush=True)
        else:
            db.outbox.sent(message["id"], _stamp(now))
            sent += 1
    db.outbox.forget_sent(_stamp(now - KEEP_SENT))
    return {"sent": sent, "failed": failed}


# The messages. Plain text: every mail client shows it, and links cannot hide behind other words.

def reset_message(name, url, hours):
    return ("Reset your GeoLogs password",
            f"Hi{' ' + name if name else ''},\n\nSomeone asked to reset the password of your GeoLogs account. "
            f"To choose a new one, open this link within {hours} hour{'s' if hours != 1 else ''}:\n\n{url}\n\n"
            "Resetting it signs you out everywhere. If you did not ask for this, ignore this message: "
            "your password stays as it is.\n")


def invite_message(inviter, url, days):
    return ("You're invited to GeoLogs",
            f"{inviter or 'Someone'} invited you to GeoLogs, a private archive of where you have been.\n\n"
            f"To create your account, open this link within {days} days:\n\n{url}\n\n"
            "If you were not expecting this, ignore this message.\n")


def verify_message(name, url, days):
    return ("Confirm your email for GeoLogs",
            f"Hi{' ' + name if name else ''},\n\nTo confirm this is your address, open this link within {days} days:\n\n"
            f"{url}\n\nIf you did not ask for this, ignore this message.\n")
