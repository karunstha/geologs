"""A fictional person's last few months, for the demo, screenshots and tests.

Alex lives in Portland, Oregon (nothing here is anyone's real home or workplace): weekdays at a studio
in the Pearl District with lunch at a café, errands on some evenings, a park on weekends, and every
other month's Saturdays a drive to Seattle or out to Multnomah Falls and Hood River. Days are relative
to `now`, so the archive always ends with the last few minutes. Stationary fixes arrive every five
minutes and moving ones every thirty seconds, as a phone app in its usual mode would send them, with
GPS jitter.
"""
import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Los_Angeles")
SOURCE = "owntracks:alex/phone"
DAYS = 60

HOME, WORK, LUNCH = (45.5252, -122.6322), (45.5266, -122.6819), (45.5282, -122.6841)
SHOP, PARK = (45.5121, -122.6205), (45.5118, -122.5946)
FUEL, BANK, SALON, GYM = (45.5190, -122.6560), (45.5235, -122.6760), (45.5160, -122.6400), (45.5310, -122.6500)
PIKE, NEEDLE, FALLS, HOOD = (47.6097, -122.3422), (47.6205, -122.3493), (45.5762, -122.1158), (45.7090, -121.5150)

# Alex's saved places (zones), and the names of the places Alex goes (name corrections), so the
# archive reads well without a geocoding provider.
SAVED_PLACES = (("Home", HOME, 150), ("Mount Tabor Park", PARK, 400), ("Pearl District Studio", WORK, 120))
PLACE_NAMES = (
    ("Pearl District Studio", WORK), ("Northwind Coffee Roasters", LUNCH), ("Hawthorne Supermarket", SHOP),
    ("Mount Tabor Park", PARK), ("Shell", FUEL), ("Chase Bank", BANK), ("Hair by Riley Salon", SALON),
    ("Eastside Fitness Gym", GYM), ("Pike Place Market", PIKE), ("Space Needle Viewpoint", NEEDLE),
    ("Multnomah Falls", FALLS), ("Hood River Waterfront Park", HOOD),
)
SETTINGS = {"units": "imperial", "timezone": ZONE.key}
FUEL_SETTINGS = {"fuel_economy_l100km": 8.5, "fuel_price_source": "manual", "currency": "USD"}
FUEL_PRICE_PER_L = 0.95
REMINDER = {"text": "Pick up oat milk", "match_name": "Supermarket"}


def jitter(position, metres, rng):
    angle, distance = rng.uniform(0, 2 * math.pi), abs(rng.gauss(0, metres))
    return (position[0] + distance * math.cos(angle) / 111320,
            position[1] + distance * math.sin(angle) / (111320 * math.cos(math.radians(position[0]))))


class Day:
    """One day's fixes, written in order: stay somewhere, then travel somewhere else."""

    def __init__(self, day, rng, zone=ZONE, home=HOME):
        self.at = datetime(day.year, day.month, day.day, tzinfo=zone)
        self.rng, self.place, self.fixes = rng, home, []

    def fix(self, position, accuracy, speed=None):
        lat, lon = jitter(position, accuracy / 2, self.rng)
        fix = {"timestamp": self.at.astimezone(timezone.utc).isoformat(), "lat": round(lat, 6),
               "lon": round(lon, 6), "accuracy_m": accuracy}
        if speed is not None:
            fix["speed_mps"] = round(speed, 1)
        self.fixes.append(fix)

    def stay_until(self, hour, minute=0):
        end = self.at.replace(hour=hour, minute=minute)
        while self.at < end:
            self.fix(self.place, self.rng.choice((8, 10, 12, 16, 20)), 0)
            self.at += timedelta(minutes=5)

    def travel_to(self, place, kmh):
        metres = math.dist((self.place[0] * 111320, self.place[1] * 91500), (place[0] * 111320, place[1] * 91500))
        steps = max(2, int(metres / (kmh / 3.6 * 30)))
        for step in range(1, steps + 1):
            share = step / steps
            bend = math.sin(share * math.pi) * 0.002  # roads are not straight lines
            position = (self.place[0] + (place[0] - self.place[0]) * share + bend,
                        self.place[1] + (place[1] - self.place[1]) * share)
            self.at += timedelta(seconds=30)
            self.fix(position, 6, kmh / 3.6 * self.rng.uniform(0.6, 1.2))
        self.place = place


def routine(day, rng):
    """A usual day: work and lunch on weekdays (sometimes the shops on the way home), the park at weekends."""
    plan = Day(day, rng)
    if day.weekday() < 5:
        plan.stay_until(7, rng.randint(20, 50))
        plan.travel_to(WORK, 45)
        plan.stay_until(12, rng.randint(0, 20))
        plan.travel_to(LUNCH, 5)
        plan.stay_until(13, rng.randint(0, 15))
        plan.travel_to(WORK, 5)
        plan.stay_until(17, rng.randint(15, 50))
        if rng.random() < 0.4:
            plan.travel_to(SHOP, 50)
            plan.stay_until(18, rng.randint(30, 59))
        plan.travel_to(HOME, 45)
    else:
        plan.stay_until(rng.randint(9, 11), rng.randint(0, 59))
        plan.travel_to(PARK, 40)
        plan.stay_until(13, rng.randint(0, 59))
        plan.travel_to(HOME, 40)
    plan.stay_until(23, 55)
    return plan.fixes


def errand_day(day, rng):
    plan = Day(day, rng)
    plan.stay_until(7, rng.randint(20, 50)); plan.travel_to(WORK, 45); plan.stay_until(12, rng.randint(0, 20))
    plan.travel_to(LUNCH, 5); plan.stay_until(13, rng.randint(0, 15)); plan.travel_to(WORK, 5)
    plan.stay_until(17, rng.randint(15, 40))
    for place, minutes in rng.sample([(FUEL, 20), (BANK, 25), (SALON, 50), (GYM, 60)], 2):
        plan.travel_to(place, 40); end = plan.at + timedelta(minutes=minutes); plan.stay_until(end.hour, end.minute)
    plan.travel_to(HOME, 45); plan.stay_until(23, 55)
    return plan.fixes


def trip_day(day, rng, stops):
    plan = Day(day, rng)
    plan.stay_until(7, 30)
    for place, kmh, hour, minute in stops:
        plan.travel_to(place, kmh); plan.stay_until(hour, minute)
    plan.travel_to(HOME, 95); plan.stay_until(23, 55)
    return plan.fixes


def fixes(days, now):
    """Alex's fixes for the last `days` days and today, up to a few minutes before `now`, oldest first."""
    rng = random.Random(5)
    today = now.astimezone(ZONE).date()
    result, saturdays = [], 0
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        if day.weekday() == 5 and back > 2:
            saturdays += 1
            if saturdays % 4 == 1:
                day_fixes = trip_day(day, rng, [(PIKE, 100, 13, 0), (NEEDLE, 25, 15, 30)])
            elif saturdays % 4 == 3:
                day_fixes = trip_day(day, rng, [(FALLS, 80, 11, 30), (HOOD, 70, 15, 0)])
            else:
                day_fixes = routine(day, rng)
        elif day.weekday() < 5 and rng.random() < 0.35:
            day_fixes = errand_day(day, rng)
        else:
            day_fixes = routine(day, rng)
        # The phone's last report was a few minutes ago, as it would be on a real server.
        result += [{**fix, "source": SOURCE} for fix in day_fixes
                   if datetime.fromisoformat(fix["timestamp"]) <= now - timedelta(minutes=8)]
    return result


def first_day(days, now):
    """The date of the archive's first fix, as fixes() writes it."""
    return (now.astimezone(ZONE).date() - timedelta(days=days)).isoformat()
