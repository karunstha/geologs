"""What a client is told when a request fails, and what only the server log hears."""
import sys
import traceback

from ..services import INTEGRITY_ERRORS

# Requests that cannot be done as asked: the client can fix these.
INPUT_ERRORS = (ValueError, KeyError, TypeError) + INTEGRITY_ERRORS


def input_message(exc):
    """A request's own mistake, said plainly: never a database's wording."""
    if isinstance(exc, INTEGRITY_ERRORS):
        return "that conflicts with something already saved"
    if isinstance(exc, KeyError):
        return f"missing field: {exc.args[0]}" if exc.args else "missing field"
    return str(exc)


def internal_failure(where):
    """Log an unexpected failure (with its traceback) for the server's owner; the client hears only that it failed."""
    print(f"GeoLogs {where} failed:", file=sys.stderr, flush=True)
    traceback.print_exc()
    return "internal error"
