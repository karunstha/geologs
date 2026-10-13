"""One transaction per use case: what a caller asked for is saved whole, or not at all."""
import functools


def use_case(function):
    """Run a service in its own transaction, committed when it returns and rolled back if it raises.

    A use case called by another joins the caller's transaction, so only the
    outermost one commits. A use case that must keep part of its work before a
    slow or outside step (sending webhooks, a bulk import's batches) commits
    that part itself, and says why.
    """
    @functools.wraps(function)
    def run(db, *args, **kwargs):
        with db.transaction():
            return function(db, *args, **kwargs)
    return run
