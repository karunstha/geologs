"""Reminder events delivered to your own webhook (GEOLOGS_WEBHOOK_URL)."""
import hashlib
import hmac
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .. import config

LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost", "host.docker.internal")


def url():
    """The configured webhook, or None. Plain HTTP is allowed only to this machine."""
    value = config.webhook()[0]
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
        raise ValueError("webhook URL must use HTTPS or loopback HTTP")
    return value


def send(target, event):
    """POST one event; returns "sent" or why it failed.

    With GEOLOGS_WEBHOOK_SECRET the body is signed (HMAC-SHA256 over
    "timestamp.body", in X-Webhook-Signature-V2 beside X-Webhook-Timestamp, so a
    receiver can reject forged and replayed requests); GEOLOGS_WEBHOOK_TOKEN is
    sent as a bearer token.
    """
    headers = {"Content-Type": "application/json"}
    _url, token, secret = config.webhook()
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(event, separators=(",", ":")).encode()
    if secret:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Webhook-Signature-V2"] = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(target, body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8):
            return "sent"
    except Exception as exc:
        return "failed: " + str(exc)[:150]
