"""Accounts: passwords, the tokens that stand for a user, and what a valid email and password are."""
import hashlib
import hmac
import re
import secrets

# scrypt at these costs takes about 50 ms and 32 MB per check: cheap for one sign-in, costly to guess at.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSWORD = 10
MAX_PASSWORD = 1024
ROLES = ("user", "admin")
# What a token is for, by its first letters: a browser session, or a personal API token.
SESSION_PREFIX, API_TOKEN_PREFIX = "gls_", "gla_"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def password_matches(password, stored):
    """Whether `password` is the one `stored` was made from (False for no stored password)."""
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    found = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                           maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(found.hex(), digest)


def check_password(password):
    """A password someone may choose, or ValueError saying why not."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"password must be at most {MAX_PASSWORD} characters")
    return password


def normal_email(email):
    """An email address as stored (trimmed), or ValueError."""
    email = str(email or "").strip()
    if len(email) > 254 or not EMAIL.match(email):
        raise ValueError("a valid email address is required")
    return email


def check_role(role):
    if role not in ROLES:
        raise ValueError("role must be user or admin")
    return role


def new_token(prefix):
    """A token to hand out once; only token_hash(token) is kept."""
    return prefix + secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
