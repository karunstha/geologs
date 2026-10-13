"""Everything GeoLogs reads from its environment, in one place.

Values are read when asked for, not once at start: a token file you edit
takes effect without a restart, and tests can set the environment per case.
The domain never reads configuration; every other layer may.
"""
import os
from pathlib import Path


def _env(name, default=""):
    return os.environ.get(name, default)


# Storage

def database_url():
    """GEOLOGS_DATABASE_URL: a PostgreSQL/PostGIS archive (else SQLite)."""
    return _env("GEOLOGS_DATABASE_URL") or None


def database_file():
    """GEOLOGS_DB, else ~/.local/share/geologs/geologs.db (or the archive from before the rename, if it is there)."""
    if _env("GEOLOGS_DB"):
        return _env("GEOLOGS_DB")
    path = Path.home() / ".local/share/geologs/geologs.db"
    # An archive created before the rename (as Geotracker) stays where it is.
    legacy = Path.home() / ".local/share/geotracker/geotracker.db"
    return str(legacy if legacy.exists() and not path.exists() else path)


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


# Server and command line

def host():
    return _env("GEOLOGS_HOST", "127.0.0.1")


def port():
    return int(_env("GEOLOGS_PORT", "8765"))


def env_file():
    """GEOLOGS_ENV_FILE: settings to load when the environment has none of its own (.env by default)."""
    return Path(_env("GEOLOGS_ENV_FILE", ".env"))


def remote_server():
    """(GEOLOGS_URL, GEOLOGS_TOKEN): the server a Home Assistant poll sends to, else (None, token)."""
    return _env("GEOLOGS_URL") or None, _env("GEOLOGS_TOKEN") or None


def home_assistant():
    """(HA_URL, HA_TOKEN, HA_ENTITY) for polling a device tracker."""
    return _env("HA_URL").rstrip("/"), _env("HA_TOKEN"), _env("HA_ENTITY")


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


def webhook():
    """(GEOLOGS_WEBHOOK_URL, GEOLOGS_WEBHOOK_TOKEN, GEOLOGS_WEBHOOK_SECRET); unset ones are None."""
    return (_env("GEOLOGS_WEBHOOK_URL") or None, _env("GEOLOGS_WEBHOOK_TOKEN") or None,
            _env("GEOLOGS_WEBHOOK_SECRET") or None)
