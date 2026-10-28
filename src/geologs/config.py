"""Everything GeoLogs reads from its environment, in one place.

Values are read when asked for, not once at start: a token file you edit
takes effect without a restart, and tests can set the environment per case.
The domain never reads configuration; every other layer may.
"""
import ipaddress
import os
from pathlib import Path
from . import __version__


def _env(name, default=""):
    return os.environ.get(name, default)


# Storage

def database_url():
    """GEOLOGS_DATABASE_URL: the PostgreSQL/PostGIS archive, as its owner (who creates and upgrades the tables)."""
    return _env("GEOLOGS_DATABASE_URL") or None


def app_database_password():
    """GEOLOGS_APP_DB_PASSWORD: the password of the login the app's requests use (geologs_app), which
    can never skip row-level security. Without it, requests use the owner's login and switch role."""
    return _env("GEOLOGS_APP_DB_PASSWORD") or None


def database_pool_size():
    """GEOLOGS_DB_POOL_SIZE: how many database connections the server keeps for its requests (2 to 100, 10 by default)."""
    return max(2, min(int(_env("GEOLOGS_DB_POOL_SIZE", "10")), 100))


def default_timezone():
    """GEOLOGS_TIMEZONE: the timezone until you choose one in Settings."""
    return _env("GEOLOGS_TIMEZONE") or None


def reverse_cache_days():
    """GEOLOGS_REVERSE_CACHE_DAYS: how long a place lookup is reused (1 to 3650 days, 180 by default)."""
    return max(1, min(int(_env("GEOLOGS_REVERSE_CACHE_DAYS", "180")), 3650))


# Access

def _token_file_value(path, name):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            return value
    return None


def api_token():
    """The token for the browser, API and MCP: GEOLOGS_TOKEN, or GEOLOGS_TOKEN in the file at
    GEOLOGS_TOKEN_FILE (read on every request, so edits take effect without a rebuild)."""
    path = _env("GEOLOGS_TOKEN_FILE")
    if not path:
        return _env("GEOLOGS_TOKEN")
    value = _token_file_value(path, "GEOLOGS_TOKEN")
    if value is None:
        raise RuntimeError("GEOLOGS_TOKEN_FILE has no GEOLOGS_TOKEN")
    if not value:
        raise RuntimeError("GEOLOGS_TOKEN_FILE has an empty token")
    return value


def ingest_token():
    """An optional token that can only send locations, safe to keep on a phone: GEOLOGS_INGEST_TOKEN
    (in the token file, when there is one)."""
    path = _env("GEOLOGS_TOKEN_FILE")
    value = _token_file_value(path, "GEOLOGS_INGEST_TOKEN") if path else None
    return value if value is not None else _env("GEOLOGS_INGEST_TOKEN")


# Where a proxy or tunnel on this machine, in a container beside it, or elsewhere on a home network connects from.
LOCAL_NETWORKS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")


def trusted_proxies():
    """GEOLOGS_TRUSTED_PROXIES: the addresses or networks (comma-separated) of the reverse proxies and
    tunnels in front of GeoLogs, whose X-Forwarded-For names the client; loopback and private networks
    by default, or none at all for "none". Read from the token file too, like GEOLOGS_INGEST_TOKEN."""
    path = _env("GEOLOGS_TOKEN_FILE")
    value = _token_file_value(path, "GEOLOGS_TRUSTED_PROXIES") if path else None
    value = (value if value is not None else _env("GEOLOGS_TRUSTED_PROXIES")).strip()
    if value.lower() == "none":
        return ()
    entries = [part.strip() for part in value.split(",") if part.strip()] or LOCAL_NETWORKS
    try:
        return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)
    except ValueError as exc:
        raise ValueError(f"GEOLOGS_TRUSTED_PROXIES: {exc}") from None


# Server and command line

def host():
    return _env("GEOLOGS_HOST", "127.0.0.1")


def port():
    return int(_env("GEOLOGS_PORT", "8765"))


def env_file():
    """GEOLOGS_ENV_FILE: settings to load when the environment has none of its own (.env by default)."""
    return Path(_env("GEOLOGS_ENV_FILE", ".env"))


def update_status_file():
    return Path(_env("GEOLOGS_UPDATE_STATUS_FILE", "/update-status/status.json"))


def app_version():
    return __version__


def revision():
    return _env("GEOLOGS_REVISION", "unknown")


# Outside services

def geoapify_key():
    return _env("GEOAPIFY_API_KEY") or None


def mapbox_token():
    return _env("MAPBOX_ACCESS_TOKEN").strip() or None


def photon_url():
    """GEOLOGS_PHOTON_URL: a self-hosted Photon server."""
    return _env("GEOLOGS_PHOTON_URL").rstrip("/")


def place_provider_configured(env_name):
    """Whether the variable a place provider needs is set."""
    return bool(_env(env_name).strip())


def eia_key():
    return _env("EIA_API_KEY").strip()


def unsplash_key():
    """UNSPLASH_ACCESS_KEY: a photo of where you are behind History; without it, a sky gradient."""
    return _env("UNSPLASH_ACCESS_KEY").strip() or None


def webhook():
    """(GEOLOGS_WEBHOOK_URL, GEOLOGS_WEBHOOK_TOKEN, GEOLOGS_WEBHOOK_SECRET); unset ones are None."""
    return (_env("GEOLOGS_WEBHOOK_URL") or None, _env("GEOLOGS_WEBHOOK_TOKEN") or None,
            _env("GEOLOGS_WEBHOOK_SECRET") or None)


def public_url():
    """GEOLOGS_PUBLIC_URL: where people open GeoLogs (https://geologs.example.com); links in emails point here."""
    return _env("GEOLOGS_PUBLIC_URL").strip().rstrip("/") or None


def smtp():
    """The mail server for invites and password resets: (host, port, username, password, sender, security),
    where security is starttls (the default, port 587), ssl (port 465) or none. None without
    GEOLOGS_SMTP_HOST and GEOLOGS_SMTP_FROM."""
    host, sender = _env("GEOLOGS_SMTP_HOST").strip(), _env("GEOLOGS_SMTP_FROM").strip()
    if not host or not sender:
        return None
    security = (_env("GEOLOGS_SMTP_SECURITY", "starttls").strip().lower() or "starttls")
    port = int(_env("GEOLOGS_SMTP_PORT") or (465 if security == "ssl" else 25 if security == "none" else 587))
    return (host, port, _env("GEOLOGS_SMTP_USERNAME") or None, _env("GEOLOGS_SMTP_PASSWORD") or None, sender, security)
