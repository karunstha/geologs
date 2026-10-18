"""Two-factor sign-in: time-based one-time codes (TOTP, RFC 6238) that authenticator apps show,
and one-time recovery codes for when the phone is lost."""
import base64
import hashlib
import hmac
import secrets
import struct

STEP_SECONDS = 30
DIGITS = 6
# Codes from one step either side are accepted, for a phone clock a little off.
DRIFT_STEPS = 1
RECOVERY_CODES = 8


def new_secret():
    """A secret to share with an authenticator app, in the base32 it is typed in as."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def matching_step(secret, code, now_seconds):
    """The time step a code belongs to (within the drift allowed), or None."""
    code = "".join(str(code or "").split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int(now_seconds) // STEP_SECONDS
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def _quote(text):
    """Percent-encode for a URI (the domain keeps to text handling, not urllib)."""
    safe = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return "".join(chr(byte) if chr(byte) in safe else f"%{byte:02X}" for byte in str(text).encode())


def setup_uri(secret, account, issuer="GeoLogs"):
    """The otpauth:// link (or QR code) an authenticator app adds the account from."""
    return (f"otpauth://totp/{_quote(issuer)}:{_quote(account)}?secret={secret}&issuer={_quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}")


def new_recovery_codes():
    """Codes to keep somewhere safe, each usable once instead of an app's code ("abcd-efgh")."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)) for _ in range(RECOVERY_CODES)]


def recovery_hash(code):
    return hashlib.sha256("".join(str(code).lower().split()).replace("-", "").encode()).hexdigest()
