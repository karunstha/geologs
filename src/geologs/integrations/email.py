"""Email through your own mail server (GEOLOGS_SMTP_*): invites, password resets, address checks."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from .. import config


def configured():
    return config.smtp() is not None


def send(to_address, subject, body):
    """Send one plain-text message; raises on failure (the outbox tries again later)."""
    host, port, username, password, sender, security = config.smtp()
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to_address, subject
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2].strip("> ") or None)
    message.set_content(body)
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if username:
            server.login(username, password or "")
        server.send_message(message)
