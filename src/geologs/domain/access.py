"""Device tokens: how they are stored and which trackers they may send as."""
import hashlib


def ingest_token_hash(token):
    """Device tokens are long and random, so a plain SHA-256 is enough to keep them out of the database."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_allows_source(locked, source):
    """Whether a device token locked to `locked` may send as `source`."""
    if not locked:
        return True
    return source == locked or (locked.endswith(":") and str(source or "").startswith(locked))
