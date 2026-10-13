"""Timestamps as stored: ISO 8601 in UTC."""
from datetime import datetime, timezone


def parse_time(value):
    if value is None or value == "":
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="auto")
