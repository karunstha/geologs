"""How the server's background jobs last went, for the admin portal (kept in memory)."""
import threading
import time
from datetime import datetime, timezone

_lock = threading.Lock()
_jobs = {}


def record(name, started, error=None):
    """Note one run of a job that began at time.perf_counter() `started`."""
    seconds = round(time.perf_counter() - started, 3)
    with _lock:
        job = _jobs.setdefault(name, {"runs": 0, "failures": 0, "slowest_seconds": 0.0})
        job["runs"] += 1
        job["last_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        job["last_seconds"] = seconds
        job["slowest_seconds"] = max(job["slowest_seconds"], seconds)
        if error is not None:
            job["failures"] += 1
            job["last_error"] = str(error)[:300]
            job["last_error_at"] = job["last_at"]


def report():
    with _lock:
        return {name: dict(job) for name, job in sorted(_jobs.items())}
