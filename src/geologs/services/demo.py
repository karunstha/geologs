"""Demo mode (GEOLOGS_DEMO): anyone may look around GeoLogs, read-only, on a fictional archive.

Every visitor sees the same account, filled with Alex's last two months (domain.demo) through the
same ingest and stay detection as a real archive. It can't be changed (presentation refuses every
write), so there is nothing to undo. Filling takes about a minute, and the archive is filled again
each day so it always ends recently; the new one takes over the visitors of the old.
"""
from datetime import datetime, timedelta, timezone

from ..domain import demo as alex
from .accounts import _check_attempts, _count_attempt
from .fuel import set_manual_price
from .ingest import ingest
from .paths import learn_path_history
from .reminders import create_reminder
from .saved_places import save_place, save_place_label
from .settings import save_settings
from .stays import process_stays
from .use_case import use_case

# The archive is filled again this long after the last, so its days end recently.
FRESH_FOR = timedelta(days=1)
# A demo still being filled after this long was left behind (the server stopped while filling it).
FILL_TIMEOUT = timedelta(minutes=30)
# Times one client may open the demo within the window (each makes a session).
OPENS_PER_CLIENT, OPEN_WINDOW = 20, 60 * 60


class DemoUnavailable(Exception):
    """The demo can't be opened yet: the first archive is still being filled."""


def _stamp(at):
    return at.isoformat(timespec="seconds")


@use_case
def open_demo(db, client=""):
    """The demo's user, to sign a visitor's browser in as. DemoUnavailable while the first archive is
    being filled; TooManyAttempts when this client opened it too often lately."""
    key = ("demo", client)
    _check_attempts(key, OPENS_PER_CLIENT, OPEN_WINDOW)
    current = db.demo.current()
    if current is None:
        raise DemoUnavailable("The demo is being prepared. Try again in a minute.")
    _count_attempt(key, OPENS_PER_CLIENT, OPEN_WINDOW)
    return db.users.get(current["user_id"])


@use_case
def is_demo(db, user_id):
    """Whether this user is the demo (read-only), rather than a real account."""
    return db.demo.is_demo(user_id)


@use_case
def demo_due(db, now=None):
    """Whether to fill the archive (again): there is none yet, or the current one is a day old."""
    current = db.demo.current()
    now = now or datetime.now(timezone.utc)
    return current is None or datetime.fromisoformat(current["ready_at"]) <= now - FRESH_FOR


@use_case
def erase_abandoned_demos(db, now=None):
    """Erase demos left half filled when the server stopped."""
    now = now or datetime.now(timezone.utc)
    erased = db.demo.abandoned(_stamp(now - FILL_TIMEOUT))
    for user_id in erased:
        db.users.delete(user_id)
    return erased


def fill_demo(db, now=None, days=alex.DAYS):
    """Fill a new demo with Alex's archive as of `now`, through the same ingest and stay detection as
    a real one, and make it the demo. It stays disabled, unseen by visitors and background work, until
    it is full; then the browsers on the old one move to it and the old one is erased. One that fails
    part way is erased instead. Returns its user id."""
    now = now or datetime.now(timezone.utc)
    db.as_user(None)
    with db.transaction():
        user_id = db.demo.add("Alex", _stamp(now))
    try:
        db.as_user(user_id)
        save_settings(db, {**alex.SETTINGS, **alex.FUEL_SETTINGS})
        set_manual_price(db, alex.FUEL_PRICE_PER_L, alex.first_day(days, now))
        # Names first: a saved place inside a named spot takes its details instead of looking them up.
        for name, (lat, lon) in alex.PLACE_NAMES:
            save_place_label(db, {"name": name, "lat": lat, "lon": lon, "radius_m": 120, "address": name})
        for name, (lat, lon), radius in alex.SAVED_PLACES:
            save_place(db, {"name": name, "lat": lat, "lon": lon, "radius_m": radius})
        create_reminder(db, alex.REMINDER)
        # The bulk importer's way: batches of 1000, each kept as it goes, stays found once after.
        points = alex.fixes(days, now)
        for start in range(0, len(points), 1000):
            ingest(db, points[start:start + 1000], trigger_reminders=False, process_analysis=False)
        process_stays(db, {alex.SOURCE}, rebuild=True)
        db.commit()
        learn_path_history(db, source=alex.SOURCE)
        db.as_user(None)
        with db.transaction():
            db.demo.mark_ready(user_id, _stamp(datetime.now(timezone.utc)))
            for older in db.demo.replace(user_id):
                db.users.delete(older)
    except BaseException:
        db.rollback()
        db.as_user(None)
        with db.transaction():
            db.users.delete(user_id)
        raise
    return user_id
