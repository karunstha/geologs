"""How often this server has asked each outside service, today and since it started: for the
admin portal, so a shared free quota (Geoapify, Unsplash) is not a surprise."""
import threading
from collections import Counter
from datetime import datetime, timezone

_lock = threading.Lock()
_by_day = {}
_since_start = Counter()


def count(service):
    day = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        _since_start[service] += 1
        _by_day.setdefault(day, Counter())[service] += 1
        for old in sorted(_by_day)[:-7]:  # a week is enough
            del _by_day[old]


def report():
    today = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        return {"today": dict(_by_day.get(today, {})), "since_start": dict(_since_start)}
