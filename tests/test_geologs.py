import base64
import json
import hashlib
import hmac
import io
import math
import os
import re
import sys
import tempfile
import unittest

import psycopg
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from geologs.services import open_database, open_pool
from geologs.data.database import STAY_REBUILDS
from geologs.services.corrections import delete_stay_edit, save_mode_override, save_stay_edit
from geologs.services.devices import create_ingest_token
from geologs.services.imports import import_csv
from geologs.services.ingest import ingest
from geologs.services.position import latest
from geologs.services.overview import daily_overviews
from geologs.services.reminders import cancel_reminder, create_reminder, delete_reminder
from geologs.services.samples import near, stats
from geologs.services.saved_places import delete_label, delete_place, place_visits, save_label, save_place
from geologs.services.settings import save_settings
from geologs.services.stays import anchored_trips
from geologs.services.places import place_details, geocode, nearby_places, places_provider, places_provider_identity, reverse_geocode
from geologs.services.location import current_location
from geologs.services.reminders import deliver_webhook
from geologs.integrations.places import reverse_cache_radius
from geologs.domain.motion import classify_motion, mode_for
from geologs.domain.places import name_matches, street_key
from geologs.domain.routes import route_segments_from_points, smooth_route_segment
from geologs.domain.briefs import daily_brief
from geologs.services.overview import daily_briefs, daily_overview, stored_daily_record, save_scheduled_summaries
from geologs.services.day import day_summary
from geologs.services.patterns import insights, place_overview, place_patterns, predict_next, resolve_place_patterns, search_visits, transitions, visited_places
from geologs.services.stays import process_stays, stays


HOME = (41.0, -87.9)
LAKE = (41.0, -87.78)


def shifted(origin, east_m, north_m):
    return (origin[0] + north_m / 110574,
            origin[1] + east_m / (111320 * math.cos(math.radians(origin[0]))))


def still(place, minutes):
    return [place] * minutes


def line(start, end, minutes):
    return [(start[0] + (end[0] - start[0]) * step / minutes, start[1] + (end[1] - start[1]) * step / minutes)
            for step in range(1, minutes + 1)]


def laps(center, radius_m, minutes, kmh, start_angle=math.pi):
    metres_per_minute = kmh / 3.6 * 60
    return [shifted(center, radius_m * math.cos(start_angle + metres_per_minute * step / radius_m),
                    radius_m * math.sin(start_angle + metres_per_minute * step / radius_m))
            for step in range(1, minutes + 1)]


def minute_fixes(segments, source="phone", start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc), accuracy_m=8, hold=1):
    """One fix per minute; `hold` > 1 repeats each position like a Home Assistant poll of an unchanged state."""
    positions = [position for segment in segments for position in segment]
    fixes, held = [], None
    for index, position in enumerate(positions):
        if held is None or index % hold == 0:
            held = (position, f"update-{index}")
        (lat, lon, *rest), external_id = held
        fixes.append({"source": source, "external_id": external_id,
                      "timestamp": (start + timedelta(minutes=index)).isoformat(),
                      "lat": lat, "lon": lon, "accuracy_m": rest[0] if rest else accuracy_m})
    return fixes


def metres_between(place, row):
    from geologs.domain.geo import metres
    return metres(place[0], place[1], row["lat"], row["lon"])


def lake_run_day(hold=1):
    """Home, drive 10 km, run an hour of laps around a 500 m radius lake, drive back, home."""
    shore = shifted(LAKE, -500, 0)
    run = laps(LAKE, 500, 60, 9)
    return minute_fixes([still(HOME, 31), line(HOME, shore, 12), run, line(run[-1], HOME, 12), still(HOME, 30)],
                        source="home_assistant:phone", hold=hold)
from geologs.services.imports import import_takeout
from geologs.presentation.mcp import dispatch
from geologs.presentation.cli import load_env_file, main
from geologs.config import api_token as active_token
from geologs.presentation.http import Handler, Server
from geologs.data import database as storage
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scratch_db sits beside this file
from scratch_db import scratch_database  # noqa: E402


def as_if_restarted(url):
    """Forget that this process already upgraded the archive, as a newly started server would."""
    storage.upgrade_again(url)


def older_schema(url, *migrations, changes=()):
    """Make an archive look like it predates these migrations, so the next open applies them again.
    `changes` are SQL statements that undo what they did (run as the tables' owner: the app may not)."""
    archive = psycopg.connect(url, autocommit=True)
    try:
        for statement in changes:
            archive.execute(statement)
        archive.execute("DELETE FROM schema_migrations WHERE version = ANY(%s)", (list(migrations),))
    finally:
        archive.close()
    as_if_restarted(url)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = scratch_database(self)
        self.db = open_database(self.url)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_arrival_once_and_idempotent_source_poll(self):
        place = save_place(self.db, {"name": "Walmart", "lat": 41, "lon": -87.9, "radius_m": 120})
        create_reminder(self.db, {"text": "Pick up salt", "place_id": place["id"]})
        now = datetime.now(timezone.utc)
        outside = {"source": "ha", "external_id": "a", "timestamp": (now-timedelta(minutes=10)).isoformat(), "lat": 41.01, "lon": -87.9}
        inside = {"source": "ha", "external_id": "b", "timestamp": (now-timedelta(minutes=5)).isoformat(), "lat": 41, "lon": -87.9}
        self.assertEqual(ingest(self.db, outside)["events"], [])
        result = ingest(self.db, inside)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(ingest(self.db, inside)["duplicates"], 1)
        self.assertEqual(len(self.db.points.observations()), 2)
        self.assertEqual(len(self.db.reminders.events()), 1)
        self.assertEqual(self.db.reminders.active(), [])
        self.assertEqual(self.db.reminders.history()[0]["status"], "triggered")

    def test_reminders_are_one_shot_even_if_old_repeat_fields_are_sent(self):
        place = save_place(self.db, {"name": "Campus", "lat": 41, "lon": -87.9})
        create_reminder(self.db, {"text": "Call friend", "place_id": place["id"], "repeat": True, "cooldown_minutes": 60})
        base = datetime.now(timezone.utc)-timedelta(minutes=25)
        def put(id_, minute, lat):
            return ingest(self.db, {"source": "phone", "external_id": str(id_), "timestamp": (base+timedelta(minutes=minute)).isoformat(), "lat": lat, "lon": -87.9})
        self.assertEqual(len(put(1, 0, 41.01)["events"]), 0)
        self.assertEqual(len(put(2, 5, 41)["events"]), 1)
        self.assertEqual(len(put(3, 10, 41)["events"]), 0)
        put(4, 15, 41.01)
        self.assertEqual(len(put(5, 20, 41)["events"]), 0)
        self.assertEqual(len(self.db.reminders.events()), 1)
        self.assertEqual(self.db.reminders.active(), [])

    def test_cancelled_reminder_moves_to_history_without_reenable(self):
        place = save_place(self.db, {"name": "Market", "lat": 41, "lon": -87.9})
        reminder = create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        self.assertEqual(len(self.db.reminders.active()), 1)
        cancel_reminder(self.db, reminder["id"])
        self.assertEqual(self.db.reminders.active(), [])
        history = self.db.reminders.history()
        self.assertEqual(history[0]["status"], "cancelled")
        self.assertIsNotNone(history[0]["cancelled_at"])
        with self.assertRaisesRegex(ValueError, "active reminder"):
            cancel_reminder(self.db, reminder["id"])

    def test_ended_reminders_can_be_deleted_from_history_with_their_destinations(self):
        place = save_place(self.db, {"name": "Market", "lat": 41, "lon": -87.9})
        kept = create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        with patch("geologs.services.places.place_for_coordinate", return_value={}):
            gone = create_reminder(self.db, {"text": "Swim", "place_name": "Lake", "lat": 41.2, "lon": -87.7})
            other = create_reminder(self.db, {"text": "Run", "place_name": "Track", "lat": 41.3, "lon": -87.6})
        waiting = create_reminder(self.db, {"text": "Still waiting", "place_id": place["id"]})
        for reminder in (kept, gone, other):
            cancel_reminder(self.db, reminder["id"])
        with self.assertRaisesRegex(ValueError, "not found in history"):
            delete_reminder(self.db, waiting["id"])
        self.assertEqual(delete_reminder(self.db, gone["id"]), {"deleted": 1})
        self.assertEqual({item["text"] for item in self.db.reminders.history()}, {"Buy salt", "Run"})
        # Its hidden destination goes with it; a saved place stays.
        self.assertIsNone(self.db.places.get(gone["place_id"]))
        self.assertEqual(delete_reminder(self.db), {"deleted": 2})
        self.assertEqual(self.db.reminders.history(), [])
        self.assertIsNone(self.db.places.get(other["place_id"]))
        self.assertEqual([item["name"] for item in self.db.places.saved()], ["Market"])
        self.assertEqual([item["text"] for item in self.db.reminders.active()], ["Still waiting"])

    def test_unsaved_coordinate_reminder_stays_out_of_saved_places_and_triggers(self):
        found = {"name": "Lake Example", "address": "Lake Example, Chicago",
                 "category": "natural.water", "provider_place_id": "lake-1"}
        with patch("geologs.services.places.place_for_coordinate", return_value=found):
            reminder = create_reminder(self.db, {"text": "Start my run", "place_name": "Lake Example",
                                                    "lat": 41, "lon": -87.9, "radius_m": 120})
        self.assertEqual(self.db.places.saved(), [])
        self.assertEqual(reminder["place_name"], "Lake Example")
        self.assertEqual(reminder["category"], "natural.water")
        now = datetime.now(timezone.utc)
        ingest(self.db, {"source": "phone", "timestamp": (now - timedelta(minutes=5)).isoformat(),
                           "lat": 41.01, "lon": -87.9})
        event = ingest(self.db, {"source": "phone", "timestamp": now.isoformat(),
                                   "lat": 41, "lon": -87.9})["events"][0]
        self.assertEqual(event["place"], "Lake Example")
        history = self.db.reminders.history()[0]
        self.assertNotIn("acknowledged_at", history)
        self.assertNotIn("acknowledged_at", {row["column_name"] for row in self.db.rows(
            "SELECT column_name FROM information_schema.columns WHERE table_name='events'")})

    def test_stale_and_cross_source_segments_do_not_inflate_distance(self):
        ingest(self.db, {"source": "one", "timestamp": "2026-09-26T10:00:00Z", "lat": 41, "lon": -87})
        ingest(self.db, {"source": "two", "timestamp": "2026-09-26T10:01:00Z", "lat": 42, "lon": -88})
        self.assertEqual(stats(self.db, )["distance_km"], 0)
        self.assertEqual(anchored_trips(self.db, ), [])
        self.assertEqual(stats(self.db, source="one")["points"], 1)
        self.assertEqual(stats(self.db, source="missing")["points"], 0)
        self.assertEqual(len(near(self.db, 41, -87, 100, source="one")), 1)
        self.assertEqual(near(self.db, 41, -87, 100, source="two"), [])

    def test_interleaved_sources_keep_independent_route_segments(self):
        points = [
            {"id": 1, "source": "one", "timestamp": "2026-09-26T10:00:00+00:00", "lat": 41, "lon": -87, "accuracy_m": 5},
            {"id": 2, "source": "two", "timestamp": "2026-09-26T10:01:00+00:00", "lat": 42, "lon": -88, "accuracy_m": 5},
            {"id": 3, "source": "one", "timestamp": "2026-09-26T10:02:00+00:00", "lat": 41.001, "lon": -87, "accuracy_m": 5},
            {"id": 4, "source": "two", "timestamp": "2026-09-26T10:03:00+00:00", "lat": 42.001, "lon": -88, "accuracy_m": 5},
        ]
        self.assertEqual(len(route_segments_from_points(points)), 2)

    def test_day_summary_counts_trips_and_unique_observed_places(self):
        save_place(self.db, {"name": "Home", "lat": 41.01, "lon": -87.9, "radius_m": 150})
        save_place(self.db, {"name": "Campus", "lat": 41, "lon": -87.9, "radius_m": 150})
        save_place(self.db, {"name": "Park", "lat": 41.002, "lon": -87.9, "radius_m": 100})
        fixes = [
            (0, 41.01, 0), (15, 41.01, 0),
            (16, 41.007, 15), (17, 41.004, 15), (18, 41.001, 15),
            (20, 41, 0), (35, 41, 0),
            (36, 41.0005, 1.4), (37, 41.001, 1.4),
            (40, 41.002, 0), (55, 41.002, 0),
        ]
        ingest(self.db, [{"source": "phone", "timestamp": (datetime(2026, 9, 25, 14, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
                           for minute, lat, speed in fixes])
        ingest(self.db, {"source": "other", "timestamp": "2026-09-25T14:20:00Z", "lat": 42, "lon": -88})
        day = day_summary(self.db, "2026-09-25", "America/Chicago", "phone")
        self.assertEqual(day["points"], 11)
        self.assertEqual(day["stats"]["points"], 11)
        self.assertEqual(day["activity_counts"]["driving"], 1)
        self.assertEqual(day["activity_counts"]["walking"], 1)
        self.assertEqual(len(day["visited_places"]), 2)
        self.assertEqual(day["visited_places"][0]["name"], "Campus")
        self.assertEqual(day["stays"][1]["display_name"], "Campus")
        self.assertEqual(day["visited_places"][0]["visits"], 1)
        self.assertEqual(len(day["trips"]), 2)
        self.assertEqual(len(day["route_segments"]), 1)

    def test_day_summary_is_persisted_until_its_inputs_change(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T10:01:00Z", "lat": 41.001, "lon": -87.9},
        ])
        first = day_summary(self.db, "2026-09-25", "UTC", "phone")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)
        with patch("geologs.services.day.stays", side_effect=AssertionError("summary was recalculated")):
            self.assertEqual(day_summary(self.db, "2026-09-25", "UTC", "phone"), first)

        ingest(self.db, {"source": "phone", "timestamp": "2026-09-25T10:02:00Z", "lat": 41.002, "lon": -87.9})
        with patch("geologs.services.day.stays", wraps=stays) as calculate_stays:
            updated = day_summary(self.db, "2026-09-25", "UTC", "phone")
        self.assertTrue(calculate_stays.called)
        self.assertEqual(updated["points"], 3)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)

        save_place(self.db, {"name": "Campus", "lat": 41, "lon": -87.9})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 0)

    def test_day_stay_uses_visited_place_reverse_geocode_label(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:16:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        found = {"name": "University of Arkansas at Little Rock", "address": "2801 South University Avenue"}
        with patch("geologs.services.places.place_for_coordinate", return_value=found) as reverse:
            day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual(day["visited_places"][0]["name"], "University of Arkansas at Little Rock")
        self.assertEqual(day["visited_places"][0]["address"], "2801 South University Avenue")
        self.assertEqual(day["stays"][0]["display_name"], day["visited_places"][0]["name"])
        reverse.assert_called_once()

    def test_recorded_route_does_not_connect_gaps(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T11:00:00Z", "lat": 41.01, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T11:05:00Z", "lat": 41.011, "lon": -87.9},
        ])
        segments = route_segments_from_points(list(reversed(self.db.points.points(limit=None))))
        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0]), 2)

    def test_local_route_smoothing_preserves_recorded_endpoints(self):
        path = [[-87.9, 41], [-87.89, 41.01], [-87.88, 41]]
        smoothed = smooth_route_segment(path)
        self.assertEqual((smoothed[0], smoothed[-1]), (path[0], path[-1]))
        self.assertGreater(len(smoothed), len(path))
        self.assertTrue(all(point in smoothed for point in path))

    def test_raw_observation_pagination(self):
        for minute in range(5):
            ingest(self.db, {"source": "phone", "timestamp": f"2026-09-25T10:0{minute}:00Z",
                               "lat": 41 + minute / 1000, "lon": -87.9})
        first = self.db.points.observations(limit=2, offset=0)
        second = self.db.points.observations(limit=2, offset=2)
        last = self.db.points.observations(limit=2, offset=4)
        self.assertEqual([row["id"] for row in first + second + last], [5, 4, 3, 2, 1])

    def test_speed_settings_reported_offset_and_calculated_mode(self):
        save_settings(self.db, {"speed_strategy": "reported", "reported_speed_offset_kmh": -3.6})
        ingest(self.db, {"source": "reported", "timestamp": "2026-09-25T10:00:00Z",
                           "lat": 41, "lon": -87.9, "speed_mps": 2})
        reported = self.db.points.observations(source="reported", limit=1)[0]
        self.assertAlmostEqual(reported["speed_mps"], 1)
        self.assertEqual(reported["speed_origin"], "reported_adjusted")
        save_settings(self.db, {"speed_strategy": "calculated", "reported_speed_offset_kmh": -3.6})
        ingest(self.db, [
            {"source": "calculated", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "speed_mps": 40},
            {"source": "calculated", "timestamp": "2026-09-25T10:01:00Z", "lat": 41.001, "lon": -87.9, "speed_mps": 40},
        ])
        calculated = self.db.points.observations(source="calculated", limit=1)[0]
        self.assertEqual(calculated["speed_origin"], "estimated")
        self.assertLess(calculated["speed_mps"], 3)

    def test_timezone_setting_is_validated(self):
        value = save_settings(self.db, {"timezone": "America/Chicago"})
        self.assertEqual(value["timezone"], "America/Chicago")
        self.assertEqual(self.db.settings.get()["timezone"], "America/Chicago")
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            save_settings(self.db, {"timezone": "not/a-zone"})

    def test_every_table_is_a_users_or_is_meant_to_be_shared(self):
        # A new table must say which it is: its rows are someone's (user_id, row-level security), or
        # it is shared by everyone or holds the accounts themselves.
        from geologs.data import postgres
        tables = {row["relname"]: row for row in self.db.rows("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r' AND c.relname<>'spatial_ref_sys'""")}
        known = set(postgres.USER_TABLES) | set(postgres.SHARED_TABLES) | set(postgres.ACCOUNT_TABLES) | set(postgres.ADMIN_TABLES)
        self.assertEqual(set(tables) - known, set(), "classify new tables in data/postgres.py")
        policies = {row["tablename"] for row in self.db.rows("SELECT tablename FROM pg_policies WHERE policyname='own_rows'")}
        for table in postgres.USER_TABLES:
            with self.subTest(table=table):
                self.assertTrue(tables[table]["relrowsecurity"] and tables[table]["relforcerowsecurity"])
                self.assertIn(table, policies)
                column = self.db.rows("""SELECT is_nullable FROM information_schema.columns
                    WHERE table_name=? AND column_name='user_id'""", (table,))
                self.assertEqual(column, [{"is_nullable": "NO"}])
        self.assertEqual(self.db.rows("SELECT current_user AS role")[0]["role"], postgres.APP_ROLE)

    def test_requests_log_in_as_a_role_that_cannot_see_past_row_level_security(self):
        from geologs.data import postgres
        who = self.db.rows("""SELECT session_user AS login, r.rolsuper, r.rolbypassrls
            FROM pg_roles r WHERE r.rolname=session_user""")[0]
        self.assertEqual((who["login"], who["rolsuper"], who["rolbypassrls"]), (postgres.APP_ROLE, False, False))
        self.db.conn.execute("RESET ROLE")  # nothing to go back to: the login itself is the app's
        self.assertEqual(self.db.rows("SELECT current_user AS role")[0]["role"], postgres.APP_ROLE)
        self.db.rollback()

    def test_query_paths_have_storage_indexes(self):
        indexes = {row["name"] for row in self.db.rows("SELECT indexname AS name FROM pg_indexes")}
        self.assertTrue({"point_observations_user_source_time", "observed_stays_v2_user_source_time",
                         "reminders_user_active_created", "events_user_triggered", "daily_summaries_user_date"}.issubset(indexes))

    def test_trip_classification_uses_connected_trip_context(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": f"2026-09-25T10:{minute:02d}:00Z", "lat": lat, "lon": -87.9, "speed_mps": speed}
            for minute, lat, speed in [(0, 41, 0), (15, 41, 0), (16, 41.01, 10), (17, 41.02, 10),
                                       (18, 41.021, 1.5), (19, 41.022, 1.5), (20, 41.023, 0), (35, 41.023, 0)]
        ])
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual(day["activity_counts"]["driving"], 1)
        self.assertEqual(day["activity_counts"]["walking"], 0)
        self.assertEqual(len(day["trips"]), 1)

    def test_red_light_does_not_split_driving_trip(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        ingest(self.db, [
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
            for minute, lat, speed in [(0, 41.0, 0), (15, 41.0, 0),
                                       (16, 41.001, 14), (17, 41.002, 14),
                                       (18, 41.002, 0), (19, 41.002, 0),
                                       (20, 41.003, 14), (21, 41.004, 16),
                                       (22, 41.005, 0), (37, 41.005, 0)]
        ])
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual(len(day["trips"]), 1)
        self.assertEqual(day["trips"][0]["mode"], "driving")
        self.assertEqual((day["activity_counts"]["running"], day["activity_counts"]["cycling"]), (0, 0))

    def test_stay_requires_fifteen_minutes(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        ingest(self.db, [
            {"source": "short", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 41, "lon": -87.9, "accuracy_m": 5}
            for minute in (0, 14)
        ] + [
            {"source": "confirmed", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 42, "lon": -88.9, "accuracy_m": 5}
            for minute in (0, 15)
        ])
        self.assertEqual(stays(self.db, source="short"), [])
        self.assertEqual(len(stays(self.db, source="confirmed")), 1)

    def test_trip_route_keeps_coverage_gaps_as_separate_segments(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41.000, 0), (15, 41.000, 0),
                 (16, 41.001, 15), (17, 41.002, 15),
                 (60, 41.003, 15), (61, 41.004, 15),
                 (62, 41.005, 0), (77, 41.005, 0)]
        ingest(self.db, [
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 5}
            for minute, lat, speed in fixes
        ])
        trip = day_summary(self.db, "2026-09-25", "UTC")["trips"][0]
        self.assertEqual(len(trip["route_segments"]), 2)
        self.assertFalse(trip["coverage_complete"])
        self.assertEqual(trip["coordinates"], [])

    def test_held_home_assistant_state_does_not_strand_next_fix(self):
        ingest(self.db, [
            {"source": "home_assistant:phone", "external_id": "held", "timestamp": "2026-09-25T10:00:00Z",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "home_assistant:phone", "external_id": "held", "timestamp": "2026-09-25T10:04:00Z",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "home_assistant:phone", "external_id": "fresh", "timestamp": "2026-09-25T10:05:00Z",
             "lat": 41.045, "lon": -87.9, "accuracy_m": 5},
        ])
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual(len(day["route_segments"]), 1)
        self.assertEqual(day["trips"], [])
        self.assertGreater(day["stats"]["distance_km"], 4)

    def test_held_source_state_does_not_bridge_more_than_fifteen_minutes(self):
        points = [
            {"id": 1, "source": "ha", "external_id": "held", "timestamp": "2026-09-25T10:00:00+00:00",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"id": 2, "source": "ha", "external_id": "held", "timestamp": "2026-09-25T10:20:00+00:00",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"id": 3, "source": "ha", "external_id": "fresh", "timestamp": "2026-09-25T10:21:00+00:00",
             "lat": 41.045, "lon": -87.9, "accuracy_m": 5},
        ]
        self.assertEqual(route_segments_from_points(points), [])

    def test_compact_stationary_timeline_preserves_raw_fixes(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 90},
            {"source": "phone", "timestamp": "2026-09-25T10:00:30Z", "lat": 41.00002, "lon": -87.9, "accuracy_m": 8},
            {"source": "phone", "timestamp": "2026-09-25T10:04:00Z", "lat": 41.00003, "lon": -87.9, "accuracy_m": 12},
        ])
        self.assertEqual(len(self.db.points.observations(limit=None)), 3)
        timeline = list(reversed(self.db.points.timeline(limit=None)))
        self.assertEqual([point["role"] for point in timeline], ["arrival", "latest"])
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual((day["timeline_points"], day["points"]), (2, 3))

    def test_repeated_poll_does_not_inherit_previous_movement_speed(self):
        ingest(self.db, {"source": "ha", "external_id": "first", "timestamp": "2026-09-25T10:00:00Z",
                           "lat": 41, "lon": -87.9, "accuracy_m": 4})
        moving = {"source": "ha", "external_id": "second", "timestamp": "2026-09-25T10:01:00Z",
                  "lat": 41.0014, "lon": -87.9, "accuracy_m": 4}
        ingest(self.db, moving)
        ingest(self.db, {**moving, "timestamp": "2026-09-25T10:01:01Z"})
        newest, previous = self.db.points.observations(limit=2)
        self.assertEqual(previous["mode"], "running_cycling")
        self.assertEqual(newest["speed_mps"], 0)
        self.assertEqual(newest["speed_origin"], "estimated")
        self.assertEqual(newest["mode"], "idle")

    def test_compact_timeline_keeps_stop_edges_and_movement(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        at_home = [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                    "lat": 41, "lon": -87.9, "accuracy_m": 5} for minute in range(100)]
        ingest(self.db, at_home)
        timeline = list(reversed(self.db.points.timeline(limit=None)))
        self.assertEqual(len(self.db.points.observations(limit=None)), 100)
        self.assertEqual([point["role"] for point in timeline], ["arrival", "latest"])
        self.assertEqual([point["timestamp"] for point in timeline], [at_home[0]["timestamp"], at_home[-1]["timestamp"]])
        movement = [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                     "lat": 41 + (minute - 99) * .001, "lon": -87.9, "accuracy_m": 5}
                    for minute in range(100, 104)]
        ingest(self.db, movement)
        timeline = list(reversed(self.db.points.timeline(limit=None)))
        self.assertEqual(len(timeline), 6)
        self.assertEqual(timeline[1]["role"], "departure")
        self.assertEqual([point["role"] for point in timeline[-4:]], ["movement"] * 4)

    def test_stays_use_best_accuracy_observation_for_a_timestamp(self):
        ingest(self.db, [
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 90},
            {"source": "phone", "timestamp": "2026-09-25T10:06:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        timeline = list(reversed(self.db.points.timeline(limit=None)))
        self.assertEqual(len(self.db.points.observations(limit=None)), 3)
        self.assertEqual([point["accuracy_m"] for point in timeline], [5, 5])

    def test_existing_observations_get_speed_origin_without_inheriting_speed(self):
        path = scratch_database(self)
        archive = open_database(path)
        ingest(archive, {"source": "ha", "external_id": "first", "timestamp": "2026-09-25T10:00:00Z",
                        "lat": 41, "lon": -87.9, "accuracy_m": 4})
        moving = {"source": "ha", "external_id": "second", "timestamp": "2026-09-25T10:01:00Z",
                  "lat": 41.0014, "lon": -87.9, "accuracy_m": 4}
        ingest(archive, moving)
        ingest(archive, {**moving, "timestamp": "2026-09-25T10:01:01Z"})
        archive.conn.execute("UPDATE point_observations SET speed_mps=NULL")
        archive.commit()
        archive.close()
        older_schema(path, 3, changes=["ALTER TABLE point_observations DROP COLUMN speed_origin"])
        migrated = open_database(path)
        try:
            newest, previous = migrated.points.observations(limit=2)
            self.assertEqual(previous["mode"], "running_cycling")
            self.assertEqual(newest["speed_mps"], 0)
            self.assertEqual(newest["mode"], "idle")
        finally:
            migrated.close()

    def test_duplicate_delivery_is_stored_and_better_accuracy_is_selected(self):
        first = {"source": "phone", "external_id": "ha-1", "timestamp": "2026-09-25T10:00:00Z",
                 "lat": 41, "lon": -87.9, "accuracy_m": 80}
        ingest(self.db, first)
        self.assertEqual(ingest(self.db, {**first, "accuracy_m": 5})["duplicates"], 1)
        self.assertEqual(len(self.db.points.points()), 1)
        self.assertEqual(len(self.db.points.observations()), 2)
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual((day["unique_points"], day["points"]), (1, 2))
        self.assertEqual(latest(self.db)["accuracy_m"], 5)

    def test_retry_of_backfilled_legacy_fix_does_not_add_an_observation(self):
        fix = {"source": "phone", "external_id": "ha-1", "timestamp": "2026-09-25T10:00:00Z",
               "lat": 41, "lon": -87.9, "accuracy_m": 80}
        ingest(self.db, fix)
        point_id = self.db.points.points()[0]["id"]
        self.db.conn.execute("UPDATE point_observations SET legacy_point_id=?,fingerprint=NULL WHERE point_id=?",
                              (point_id, point_id))
        self.db.commit()
        self.assertEqual(ingest(self.db, fix)["duplicates"], 1)
        self.assertEqual(len(self.db.points.observations()), 1)
        ingest(self.db, {**fix, "accuracy_m": 5})
        self.assertEqual(len(self.db.points.observations()), 2)

    def test_short_stop_after_a_drive_is_a_quick_stop(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        ingest(self.db, [
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
            for minute, lat, speed in [(0, 41.0, 0), (15, 41.0, 0),
                                       (16, 41.001, 15), (17, 41.002, 15),
                                       (18, 41.002, 0), (19, 41.002, 0), (20, 41.002, 0),
                                       (21, 41.002, 0), (22, 41.002, 0),
                                       (23, 41.0025, 1.5), (24, 41.003, 1.5),
                                       (25, 41.004, 0), (40, 41.004, 0)]
        ])
        trips = list(reversed(day_summary(self.db, "2026-09-25", "UTC")["trips"]))
        # Five minutes still after a drive is a quick stop (a fuel stop) that splits the trip.
        self.assertEqual([trip["mode"] for trip in trips], ["driving", "walking"])
        self.assertEqual([stay["kind"] for stay in reversed(stays(self.db, limit=None))], ["stationary", "stop", "stationary"])
        save_settings(self.db, {"quick_stop_minutes": 0})
        trips = list(reversed(day_summary(self.db, "2026-09-25", "UTC")["trips"]))
        self.assertEqual([trip["mode"] for trip in trips], ["driving"])

    def test_held_home_assistant_stop_under_15_minutes_does_not_split_trip(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41.000, "home", 0), (15, 41.000, "home", 0),
                 (16, 41.010, "drive-a", 15), (17, 41.020, "stop", 15),
                 (18, 41.020, "held", 0), (23, 41.020, "held", 0),
                 (24, 41.030, "drive-b", 15), (25, 41.040, "finish", 0),
                 (40, 41.040, "finish", 0)]
        ingest(self.db, [{"source": "home_assistant:phone", "external_id": external_id,
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 5}
                           for minute, lat, external_id, speed in fixes])
        trips = list(reversed(day_summary(self.db, "2026-09-25", "UTC")["trips"]))
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["mode"], "driving")
        # 1.1 km after six polls of one position: nobody saw when the car moved off, or which way.
        self.assertFalse(trips[0]["coverage_complete"])
        self.assertTrue(any(leg.get("jump") for leg in trips[0]["route"]))

    def test_home_is_hidden_from_visited_list_but_kept_in_daily_overview(self):
        home = save_place(self.db, {"name": "Home", "lat": 41, "lon": -87.9, "radius_m": 150})
        campus = save_place(self.db, {"name": "Campus", "lat": 41.02, "lon": -87.9, "radius_m": 150})
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41), (16, 41), (17, 41.005), (18, 41.01), (20, 41.02), (36, 41.02)]
        ingest(self.db, [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "accuracy_m": 5}
                           for minute, lat in fixes])
        reminder = create_reminder(self.db, {"text": "Call friend", "place_id": campus["id"]})
        point_id = self.db.points.points(limit=1)[0]["id"]
        triggered_at = (base + timedelta(minutes=16)).isoformat()
        self.db.conn.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?)",
                              (reminder["id"], point_id, triggered_at))
        self.db.conn.execute("UPDATE reminders SET active=0,last_triggered_at=? WHERE id=?", (triggered_at, reminder["id"]))
        self.db.commit()
        day = day_summary(self.db, "2026-09-25", "UTC")
        self.assertEqual([place["name"] for place in day["visited_places"]], ["Campus"])
        self.assertEqual({place["name"] for place in day["places_including_home"]}, {"Home", "Campus"})
        summary = daily_overview(self.db, "2026-09-25", "UTC")
        self.assertEqual(summary["schema_version"], 7)
        self.assertEqual({place["name"] for place in summary["places"]}, {"Home", "Campus"})
        self.assertEqual((summary["totals"]["trips"], summary["trips"][0]["from"], summary["trips"][0]["to"]),
                         (1, "Home", "Campus"))
        self.assertEqual(summary["totals"]["distance_km"], summary["trips"][0]["distance_km"])
        self.assertTrue(next(place for place in summary["places"] if place["name"] == "Home")["home"])
        self.assertEqual(summary["totals"]["reminders_triggered"], 1)
        self.assertEqual(len(daily_overviews(self.db, )), 1)
        self.assertEqual(current_location(self.db)["place_name"], "Campus")

    def test_a_month_adds_up_its_days_building_the_ones_not_yet_saved(self):
        from geologs.services.periods import period_summary
        save_place(self.db, {"name": "Home", "lat": 41, "lon": -87.9, "radius_m": 150})
        save_place(self.db, {"name": "Campus", "lat": 41.02, "lon": -87.9, "radius_m": 150})
        for day in (25, 26):
            base = datetime(2026, 9, day, 10, tzinfo=timezone.utc)
            ingest(self.db, [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                                "lat": lat, "lon": -87.9, "accuracy_m": 5}
                               for minute, lat in [(0, 41), (16, 41), (17, 41.005), (18, 41.01), (20, 41.02), (36, 41.02)]])
        town = {"city": "Chicago", "region": "Illinois", "country": "United States", "country_code": "US"}
        with patch("geologs.services.ambient.place_of", return_value=town):
            # With no time to spare, one day is built per call and the rest are left for the next.
            first = period_summary(self.db, "2026-09", budget_seconds=0)
            self.assertEqual((first["days_with_data"], first["pending"]["days"], first["totals"]["days"]), (2, 1, 1))
            month = period_summary(self.db, "2026-09")
        self.assertEqual(month["pending"], {"days": 0, "towns": 0})
        self.assertEqual((month["kind"], month["first"], month["last"]), ("month", "2026-09-01", "2026-09-30"))
        # The drive home that ran overnight is one trip, though both days' records hold it.
        self.assertEqual((month["totals"]["days"], month["totals"]["trips"]), (2, 3))
        # Campus is one place across both days; Home is kept apart from the list.
        self.assertEqual([(place["name"], place["days"]) for place in month["places"]], [("Campus", 2)])
        self.assertEqual(month["home"]["days"], 2)
        self.assertEqual([town["name"] for town in month["towns"]], ["Chicago"])
        self.assertEqual([day["date"] for day in month["days"]], ["2026-09-25", "2026-09-26"])
        self.assertGreater(month["highlights"]["longest_trip"]["distance_km"], 2)
        year = period_summary(self.db, "2026")
        self.assertEqual([item["days"] for item in year["months"]][8], 2)
        for wrong in ("2026-13", "26", "2026-9"):
            with self.assertRaises(ValueError):
                period_summary(self.db, wrong)

    def test_csv_import_and_mcp(self):
        csv = "timestamp,lat,lon,accuracy_m,speed_mps\n2026-09-26T10:00:00Z,41,-87.9,,\n"
        self.assertEqual(import_csv(self.db, csv)["inserted"], 1)
        init = dispatch(self.db, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "geologs")
        tools = dispatch(self.db, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("get_current_location", tool_names)
        self.assertIn("get_compact_timeline", tool_names)
        self.assertIn("get_visited_places", tool_names)
        self.assertIn("get_daily_overview", tool_names)
        self.assertIn("list_reminder_history", tool_names)
        self.assertNotIn("set_reminder_active", tool_names)
        self.assertNotIn("plan_route", tool_names)
        result = dispatch(self.db, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_current_location", "arguments": {}}})
        self.assertEqual(result["result"]["structuredContent"]["result"]["lat"], 41)

    def test_mcp_streamable_http_headers_and_notification(self):
        handler = Handler.__new__(Handler)
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json; charset=utf-8"
        handler.headers["Accept"] = "application/json, text/event-stream"
        handler.headers["MCP-Protocol-Version"] = "2025-11-25"
        handler.mcp_error = Mock()
        self.assertTrue(handler.mcp_headers_valid())
        self.assertIsNone(dispatch(self.db, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        handler.headers.replace_header("Accept", "application/json")
        self.assertFalse(handler.mcp_headers_valid())
        handler.mcp_error.assert_called_with(406, -32600, "Accept must include application/json and text/event-stream")
        handler.headers.replace_header("Accept", "application/json, text/event-stream")
        handler.headers.replace_header("MCP-Protocol-Version", "2024-11-05")
        self.assertFalse(handler.mcp_headers_valid())
        handler.mcp_error.assert_called_with(400, -32600, "Unsupported MCP protocol version")

    def test_historical_points_do_not_fire_live_reminders(self):
        place = save_place(self.db, {"name": "Market", "lat": 41, "lon": -87.9})
        create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        result = import_csv(self.db, "timestamp,lat,lon\n2020-01-01T10:00:00Z,41,-87.9\n")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.db.reminders.events(), [])

    def test_mode_thresholds(self):
        self.assertEqual(mode_for(0), "idle")
        self.assertEqual(mode_for(1), "walking")
        self.assertEqual(mode_for(3), "running_cycling")
        self.assertEqual(mode_for(15), "driving")
        self.assertEqual(mode_for(1, 300), "unknown")

    def test_local_env_file_loads_token_without_overriding_process_env(self):
        path = Path(self.tmp.name) / ".env"
        path.write_text("GEOLOGS_TOKEN=file-token\n# comment\nGEOAPIFY_API_KEY='quoted-key'\n")
        with patch.dict(os.environ, {"GEOLOGS_ENV_FILE": str(path)}, clear=True):
            load_env_file()
            self.assertEqual(os.environ["GEOLOGS_TOKEN"], "file-token")
            self.assertEqual(os.environ["GEOAPIFY_API_KEY"], "quoted-key")
            os.environ["GEOLOGS_TOKEN"] = "process-token"
            load_env_file()
            self.assertEqual(os.environ["GEOLOGS_TOKEN"], "process-token")

    def test_mounted_token_file_tracks_changes(self):
        path = Path(self.tmp.name) / ".env"
        path.write_text("GEOLOGS_TOKEN=first\n")
        with patch.dict(os.environ, {"GEOLOGS_TOKEN_FILE": str(path), "GEOLOGS_TOKEN": "old-env"}):
            self.assertEqual(active_token(), "first")
            path.write_text("GEOLOGS_TOKEN=second\n")
            self.assertEqual(active_token(), "second")

    def test_optional_self_hosted_providers_and_cache(self):
        feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-87.9, 41]}, "properties": {"name": "Market", "city": "Chicago", "type": "house"}}
        with patch.dict(os.environ, {"GEOLOGS_PHOTON_URL": "http://127.0.0.1:2322"}), patch("geologs.integrations.places.json_get", return_value={"features": [feature]}) as request:
            self.assertEqual(geocode(self.db, "market")[0]["lat"], 41)
            self.assertEqual(geocode(self.db, "market")[0]["name"], "Market, Chicago")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(reverse_geocode(self.db, 41, -87.9)["cache"], "provider")
            exact = reverse_geocode(self.db, 41, -87.9)
            self.assertEqual(exact["cache"], "region")
            self.assertFalse(exact["approximate"])
            reused = reverse_geocode(self.db, 41.00005, -87.9)
            self.assertEqual(reused["cache"], "region")
            self.assertTrue(reused["approximate"])
            self.assertEqual(reverse_geocode(self.db, 41.001, -87.9)["cache"], "provider")
            self.assertEqual(request.call_count, 3)

    def test_geocode_bias_uses_latest_fix_and_cache_is_area_specific(self):
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9})
        response = {"results": [{"name": "Neighborhood Market", "formatted": "Neighborhood Market, 1200 Main Street", "lat": 41.01, "lon": -87.9}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.integrations.places.provider_request", return_value=response) as request:
            self.assertEqual(geocode(self.db, "market")[0]["name"], "Neighborhood Market")
            self.assertEqual(geocode(self.db, "market")[0]["address"], "Neighborhood Market, 1200 Main Street")
            # Nearby first (a 100 km circle), then anywhere; both favour the latest fix.
            nearby, anywhere = (call.args[1] for call in request.call_args_list)
            self.assertEqual((nearby["filter"], nearby["bias"]), ("circle:-87.9,41.0,100000", "proximity:-87.9,41.0"))
            self.assertEqual((anywhere.get("filter"), anywhere["bias"]), (None, "proximity:-87.9,41.0"))
            geocode(self.db, "market")
            self.assertEqual(request.call_count, 2)
            geocode(self.db, "market", 42, -88)
            self.assertEqual(request.call_count, 4)

    def test_saved_places_reverse_geocode_once_per_cached_region(self):
        response = {"results": [{"formatted": "Market Street", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.integrations.places.provider_request", return_value=response) as request:
            first = save_place(self.db, {"name": "Shop", "lat": 41, "lon": -87.9})
            second = save_place(self.db, {"name": "Office", "lat": 41.00003, "lon": -87.9})
            self.assertEqual(first["address"], "Market Street")
            self.assertEqual(second["address"], "Market Street")
            self.assertEqual(request.call_count, 3)

    def test_nearest_named_place_across_both_category_groups_names_a_position(self):
        # Mirrors a live Maumelle lookup: the point is inside a park; hotels are kilometres away.
        everyday = {"features": [
            {"properties": {"name": "Bank ATM", "categories": ["service.financial.atm"], "distance": 0}},
            {"properties": {"name": "Lake Willastein Park", "formatted": "Lake Willastein Park, Maumelle, AR",
                            "categories": ["leisure", "leisure.park"], "place_id": "park", "distance": 3}},
            {"properties": {"name": "Kroger", "categories": ["building", "building.commercial"], "place_id": "kroger", "distance": 558}},
        ]}
        travel = {"features": [{"properties": {"name": "Hampton Inn Maumelle", "categories": ["accommodation.hotel"],
                                               "place_id": "hotel", "distance": 3170}}]}
        responses = lambda path, params: travel if "accommodation" in params["categories"] else everyday
        env = {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.provider_request", side_effect=responses) as request:
            result = reverse_geocode(self.db, 34.852116, -92.412809)
            cached = reverse_geocode(self.db, 34.85214, -92.412809)
            wider = reverse_geocode(self.db, 34.852116, -92.412809, radius_m=515)
        self.assertEqual((result["name"], result["category"], result["distance_m"]), ("Lake Willastein Park", "leisure.park", 3))
        self.assertEqual(result["address"], "Lake Willastein Park, Maumelle, AR")
        self.assertEqual((cached["cache"], cached["approximate"]), ("region", True))
        self.assertEqual(wider["cache"], "provider")
        # Two requests per lookup, one per category group, never mixed in one request.
        self.assertEqual(request.call_count, 4)
        first, second = (call.args[1] for call in request.call_args_list[:2])
        self.assertEqual({first["categories"].split(",")[0], second["categories"].split(",")[0]}, {"activity", "accommodation"})
        self.assertFalse(set(first["categories"].split(",")) & set(second["categories"].split(",")))
        for params in (first, second):
            self.assertEqual((params["filter"], params["bias"], params["conditions"], params["limit"]),
                             ("circle:-92.412809,34.852116,150", "proximity:-92.412809,34.852116", "named", 20))
            self.assertNotIn("highway", params["categories"].split(","))
        self.assertEqual(request.call_args_list[2].args[1]["filter"], "circle:-92.412809,34.852116,600")

    def test_nearer_place_from_the_second_category_group_wins(self):
        everyday = {"features": [{"properties": {"name": "Riverside Park", "categories": ["leisure.park"], "distance": 60}}]}
        travel = {"features": [{"properties": {"name": "Planet Fitness", "categories": ["sport.fitness"], "distance": 20}}]}
        responses = lambda path, params: travel if "sport" in params["categories"] else everyday
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), \
                patch("geologs.integrations.places.provider_request", side_effect=responses):
            self.assertEqual(reverse_geocode(self.db, 41, -87.9)["name"], "Planet Fitness")

    def test_no_named_place_nearby_falls_back_to_the_address(self):
        reverse = {"results": [{"formatted": "5511 West Markham Street, Little Rock, AR", "result_type": "building"}]}
        responses = lambda path, params: {"features": []} if path == "/v2/places" else reverse
        env = {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.provider_request", side_effect=responses) as request:
            result = reverse_geocode(self.db, 34.749, -92.332)
        self.assertEqual(result["name"], "5511 West Markham Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/v2/places", "/v2/places", "/v1/geocode/reverse"])

    def test_photon_searches_its_radius_and_skips_streets(self):
        street = {"geometry": {"coordinates": [-87.9, 41]}, "properties": {"name": "Main Street", "osm_key": "highway"}}
        cafe = {"geometry": {"coordinates": [-87.9, 41.0003]}, "properties": {"name": "Corner Cafe", "osm_key": "amenity", "osm_value": "cafe"}}
        with patch.dict(os.environ, {"GEOLOGS_PHOTON_URL": "http://127.0.0.1:2322"}), \
                patch("geologs.integrations.places.json_get", return_value={"features": [street, cafe]}) as request:
            result = reverse_geocode(self.db, 41, -87.9, radius_m=250)
        self.assertEqual(result["name"], "Corner Cafe")
        self.assertIn("radius=0.3", request.call_args.args[0])

    def test_places_provider_follows_settings_when_configured(self):
        only_geoapify = {"GEOAPIFY_API_KEY": "key", "MAPBOX_ACCESS_TOKEN": "", "GEOLOGS_PHOTON_URL": ""}
        both = {**only_geoapify, "MAPBOX_ACCESS_TOKEN": "token"}
        with patch.dict(os.environ, only_geoapify):
            self.assertEqual(places_provider(self.db), "geoapify")
            with self.assertRaisesRegex(ValueError, "Set MAPBOX_ACCESS_TOKEN to use Mapbox"):
                save_settings(self.db, {"places_provider": "mapbox"})
            with self.assertRaisesRegex(ValueError, "places_provider must be one of"):
                save_settings(self.db, {"places_provider": "google"})
        with patch.dict(os.environ, both):
            self.assertEqual(save_settings(self.db, {"places_provider": "mapbox"})["places_provider"], "mapbox")
            self.assertEqual((places_provider(self.db), places_provider_identity(self.db)), ("mapbox", "mapbox"))
        # A chosen provider whose token was removed falls back to one that works.
        with patch.dict(os.environ, only_geoapify):
            self.assertEqual(places_provider(self.db), "geoapify")
            self.assertEqual(save_settings(self.db, {"timezone": "UTC"})["places_provider"], "mapbox")
        with patch.dict(os.environ, {**only_geoapify, "GEOAPIFY_API_KEY": ""}):
            self.assertIsNone(places_provider(self.db))
            with self.assertRaisesRegex(ValueError, "MAPBOX_ACCESS_TOKEN"):
                reverse_geocode(self.db, 41, -87.9)

    def test_other_configured_providers_answer_when_the_preferred_one_is_down(self):
        import urllib.error
        origin = (34.852116, -92.412809)
        lat, lon = shifted(origin, 40, 0)
        park = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"name": "Lake Willastein Park", "feature_type": "poi", "mapbox_id": "id-park",
                               "full_address": "Lake Willastein Park, Maumelle, Arkansas",
                               "coordinates": {"latitude": lat, "longitude": lon},
                               "poi_category_ids": ["park"], "poi_category": ["park"]}}
        asked = []
        def respond(url):
            asked.append(url.split("/")[2])
            if "geoapify" in url:
                raise urllib.error.URLError("quota spent")
            return {"features": [park]}
        env = {"GEOAPIFY_API_KEY": "key", "MAPBOX_ACCESS_TOKEN": "token", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.json_get", side_effect=respond):
            save_settings(self.db, {"places_provider": "geoapify"})
            self.assertEqual(places_provider(self.db), "geoapify")
            found = reverse_geocode(self.db, *origin)
        self.assertEqual((found["name"], found["source"]), ("Lake Willastein Park", "mapbox_searchbox"))
        self.assertEqual(asked[0], "api.geoapify.com")
        self.assertIn("api.mapbox.com", asked)
        # With nothing else configured, the preferred provider's failure is the answer.
        with patch.dict(os.environ, {**env, "MAPBOX_ACCESS_TOKEN": ""}), \
                patch("geologs.integrations.places.json_get", side_effect=respond):
            with self.assertRaises(OSError):
                reverse_geocode(self.db, *shifted(origin, 0, 3000))

    def test_mapbox_names_a_position_after_the_nearest_poi_within_the_radius(self):
        origin = (34.852116, -92.412809)
        def poi(name, east_m, categories, feature_type="poi"):
            lat, lon = shifted(origin, east_m, 0)
            return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"name": name, "feature_type": feature_type, "mapbox_id": "id-" + name,
                                   "full_address": name + ", Maumelle, Arkansas", "coordinates": {"latitude": lat, "longitude": lon},
                                   "poi_category_ids": categories, "poi_category": [c.replace("_", " ") for c in categories]}}
        pois = {"features": [poi("Kroger", 400, ["grocery"]), poi("Chase ATM", 10, ["atm"]),
                             poi("Lake Willastein Park", 40, ["park"])]}
        address = {"features": [poi("1 Lake Drive", 0, [], "address")]}
        requests = []
        def respond(url):
            requests.append(url)
            return pois if "types=poi" in url else address
        env = {"MAPBOX_ACCESS_TOKEN": "token", "GEOAPIFY_API_KEY": "", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.json_get", side_effect=respond):
            park = reverse_geocode(self.db, *origin)
            far = reverse_geocode(self.db, *shifted(origin, 0, 2000))
        self.assertEqual((park["name"], park["category"], park["distance_m"], park["source"]),
                         ("Lake Willastein Park", "park", 40, "mapbox_searchbox"))
        self.assertEqual(park["provider_place_id"], "id-Lake Willastein Park")
        self.assertEqual((far["name"], far["place_name"]), ("1 Lake Drive, Maumelle, Arkansas", ""))
        self.assertTrue(requests[0].startswith("https://api.mapbox.com/search/searchbox/v1/reverse?"))
        for part in ("longitude=-92.412809", "latitude=34.852116", "types=poi", "limit=10", "access_token=token"):
            self.assertIn(part, requests[0])
        self.assertIn("types=address%2Cstreet", requests[-1])

    def test_mapbox_nearby_places_use_category_search_and_deduplicate(self):
        origin = (34.7575, -92.452695)
        def poi(name, east_m, category, place_id=None, feature_type="poi"):
            lat, lon = shifted(origin, east_m, 0)
            return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"name": name, "feature_type": feature_type, "mapbox_id": place_id or "id-" + name,
                                   "full_address": name + ", Little Rock, Arkansas",
                                   "coordinates": {"latitude": lat, "longitude": lon},
                                   "poi_category_ids": [category] if category else [],
                                   "poi_category": [category.replace("_", " ")] if category else []}}
        reverse = {"features": [poi("16901 Chenal Parkway", 84, "", feature_type="address"),
                                poi("Natural State Eyecare", 40, "health_services")]}
        costco = poi("Costco", 113, "shopping", "costco")
        responses = {
            "wholesale_store": {"features": [costco]},
            "shopping": {"features": [costco]},  # the same Mapbox ID from another category
            "grocery": {"features": [poi("Kroger", 371, "grocery")]},
            "food_and_drink": {"features": [poi("Starbucks", 249, "coffee"),
                                             poi("Outside the circle", 1200, "food")]},
            "services": {"features": [poi("Nearby ATM", 10, "atm")]},
        }
        requests = []
        def respond(url):
            requests.append(url)
            if "/reverse?" in url:
                return reverse
            return next((value for category, value in responses.items()
                         if f"/category/{category}?" in url), {"features": []})
        env = {"MAPBOX_ACCESS_TOKEN": "token", "GEOAPIFY_API_KEY": "", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS", 0), \
                patch("geologs.integrations.places.json_get", side_effect=respond) as request:
            places = nearby_places(self.db, *origin, radius_m=1000)
            cached = nearby_places(self.db, *shifted(origin, 5, 0), radius_m=1000)
        self.assertEqual([place["name"] for place in places],
                         ["Natural State Eyecare", "Costco", "Starbucks", "Kroger"])
        self.assertEqual([place["provider_place_id"] for place in places].count("costco"), 1)
        self.assertEqual([place["name"] for place in cached], [place["name"] for place in places])
        self.assertEqual(request.call_count, 14)  # reverse plus 13 visit-relevant categories; second call was cached
        self.assertTrue(any("/search/searchbox/v1/category/wholesale_store?" in url for url in requests))
        category_url = next(url for url in requests if "/category/food_and_drink?" in url)
        for part in ("proximity=-92.452695%2C34.7575", "limit=25", "bbox=", "exclude_fields=photos%2Creviews"):
            self.assertIn(part, category_url)

    def test_mapbox_search_and_cache_are_separate_from_other_providers(self):
        suggested = {"suggestions": [{"name": "Costco Wholesale", "mapbox_id": "costco", "feature_type": "poi",
                                      "full_address": "Costco Wholesale, 1 Warehouse Way", "distance": 9800,
                                      "poi_category_ids": ["warehouse_store"]}]}
        retrieved = {"features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-92.3, 34.8]},
                                   "properties": {"name": "Costco Wholesale", "feature_type": "poi", "mapbox_id": "costco",
                                                  "full_address": "Costco Wholesale, 1 Warehouse Way",
                                                  "coordinates": {"latitude": 34.8, "longitude": -92.3},
                                                  "poi_category_ids": ["warehouse_store"]}}]}
        env = {"MAPBOX_ACCESS_TOKEN": "token", "GEOAPIFY_API_KEY": "key", "GEOLOGS_PHOTON_URL": ""}
        respond = lambda url: suggested if "/suggest?" in url else retrieved
        with patch.dict(os.environ, env), patch("geologs.integrations.places.json_get", side_effect=respond) as request:
            save_settings(self.db, {"places_provider": "mapbox"})
            listed = geocode(self.db, "costco", 34.85, -92.41, resolve=0)
            geocode(self.db, "costco", 34.85, -92.41, resolve=0)
            # The browser lists suggestions, then fetches coordinates for the one picked.
            self.assertEqual(request.call_count, 1)
            self.assertIn("/search/searchbox/v1/suggest?q=costco", request.call_args.args[0])
            self.assertIn("proximity=-92.41%2C34.85", request.call_args.args[0])
            self.assertEqual((listed[0]["name"], listed[0]["lat"], listed[0]["distance_m"]), ("Costco Wholesale", None, 9800))
            picked = place_details(self.db, listed[0]["provider_place_id"], listed[0]["session"])
            self.assertIn(f"/retrieve/costco?session_token={listed[0]['session']}", request.call_args.args[0])
            self.assertEqual((picked["lat"], picked["lon"]), (34.8, -92.3))
            # Callers that need coordinates (a reminder by place name, MCP) get them resolved.
            resolved = geocode(self.db, "costco", 34.85, -92.41, resolve=1)
            self.assertEqual((resolved[0]["lat"], request.call_count), (34.8, 2))
        # Switching back to Geoapify asks Geoapify instead of reusing Mapbox's cached answer.
        with patch.dict(os.environ, env), patch("geologs.integrations.places.provider_request", return_value={"results": []}) as geoapify:
            save_settings(self.db, {"places_provider": "geoapify"})
            self.assertEqual(geocode(self.db, "costco", 34.85, -92.41), [])
        self.assertEqual(geoapify.call_count, 2)

    def test_place_search_puts_the_nearby_match_before_distant_ones(self):
        # Live Mapbox /suggest for "Tea Capital" near Little Rock: the shop across town comes first.
        here = (34.852116, -92.412809)
        suggested = {"suggestions": [
            {"name": "Tea Capital", "mapbox_id": "lr", "feature_type": "poi", "distance": 18000,
             "full_address": "1501 Main St Ste G, Little Rock, Arkansas 72202, United States"},
            {"name": "Tea capital", "mapbox_id": "kochi", "feature_type": "poi", "distance": 14892000,
             "full_address": "Panampilly Nagar Ave, Ernakulam, 682036, India"},
            {"name": "Capital Tea", "mapbox_id": "denver", "feature_type": "poi", "distance": 1234000,
             "full_address": "1450 S Broadway, Denver, Colorado 80210, United States"}]}
        env = {"MAPBOX_ACCESS_TOKEN": "token", "GEOAPIFY_API_KEY": "", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.places.json_get", return_value=suggested):
            save_settings(self.db, {"places_provider": "mapbox"})
            found = geocode(self.db, "Tea Capital", *here, resolve=0)
        self.assertEqual([(place["name"], place["distance_m"]) for place in found],
                         [("Tea Capital", 18000), ("Capital Tea", 1234000), ("Tea capital", 14892000)])
        # Geoapify: a nearby search runs first, so a tea shop across town beats an exact name in India.
        def geoapify(path, params):
            if "filter" in params:
                return {"results": [{"name": "Zen Tea", "formatted": "Zen Tea, 5625 Kavanaugh Boulevard, Little Rock", "lat": 34.79, "lon": -92.33}]}
            return {"results": [{"name": "Tea Capital", "formatted": "Tea Capital, Ernakulam, India", "lat": 9.96, "lon": 76.29},
                                {"name": "Zen Tea", "formatted": "Zen Tea, 5625 Kavanaugh Boulevard, Little Rock", "lat": 34.79, "lon": -92.33}]}
        with patch.dict(os.environ, {**env, "GEOAPIFY_API_KEY": "key"}), patch("geologs.integrations.places.provider_request", side_effect=geoapify):
            save_settings(self.db, {"places_provider": "geoapify"})
            found = geocode(self.db, "Tea Capital", *here)
        self.assertEqual([place["name"] for place in found], ["Zen Tea", "Tea Capital"])
        self.assertLess(found[0]["distance_m"], 15000)
        self.assertGreater(found[1]["distance_m"], 14_000_000)

    def test_geoapify_reverse_prefers_place_name_and_keeps_address(self):
        response = {"results": [{"name": "War Memorial Park",
                                  "formatted": "War Memorial Park, 5511 West Markham Street, Little Rock, AR",
                                  "address_line1": "War Memorial Park", "result_type": "amenity",
                                  "category": "leisure.park", "distance": 8}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.integrations.places.provider_request", return_value=response) as request:
            result = reverse_geocode(self.db, 34.749, -92.332)
            nearby = reverse_geocode(self.db, 34.74903, -92.332)
        self.assertEqual(result["name"], "War Memorial Park")
        self.assertEqual(result["place_name"], "War Memorial Park")
        self.assertEqual(result["address"], "War Memorial Park, 5511 West Markham Street, Little Rock, AR")
        self.assertEqual(nearby["name"], "War Memorial Park")
        self.assertEqual(nearby["cache"], "region")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual(request.call_count, 3)

    def test_geoapify_reverse_uses_address_when_place_is_unnamed(self):
        response = {"results": [{"formatted": "1200 Main Street, Little Rock, AR", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.integrations.places.provider_request", return_value=response):
            result = reverse_geocode(self.db, 34.74, -92.27)
        self.assertEqual(result["name"], "1200 Main Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")

    def test_a_lookup_never_commits_half_of_an_ingest(self):
        # A stop near a café: the name-only reminder check looks up nearby places (and caches
        # them) halfway through the ingest. Only the ingest itself may commit, once, at its end.
        create_reminder(self.db, {"text": "Get beans", "match_name": "Starbucks"})
        start = datetime.now(timezone.utc) - timedelta(minutes=10)
        fixes = minute_fixes([still(HOME, 8)], start=start)
        nearby = [{"name": "Starbucks Coffee", "place_lat": HOME[0], "place_lon": HOME[1], "distance_m": 5}]
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "key"}), \
                patch("geologs.integrations.places.nearby", return_value=nearby) as lookup, \
                patch.object(self.db, "commit", wraps=self.db.commit) as commit:
            result = ingest(self.db, fixes)
        self.assertEqual((lookup.call_count, commit.call_count), (1, 1))
        self.assertEqual([event["place"] for event in result["events"]], ["Starbucks Coffee"])
        # The names are kept beside the open stop, not inside stay detection's own state.
        state = self.db.stays.state("phone")
        self.assertNotIn("nearby_names", state["candidate"])
        self.assertEqual(self.db.stays.nearby_names("phone", state["candidate"]["start_id"]), ["Starbucks Coffee"])

    def test_stay_detection_runs_without_a_database(self):
        from geologs.domain.settings import ANALYSIS_DEFAULTS
        from geologs.domain.stay_detection import advance

        class Stays:
            """Keeps what the detector reports, in memory."""
            def __init__(self):
                self.kept, self.withdrawn = {}, []
            def keep(self, record):
                self.kept[record["start_id"]] = record
            def withdraw(self, start_id):
                self.withdrawn.append(start_id)
                self.kept.pop(start_id, None)

        stays_found, candidate = Stays(), None
        fixes = minute_fixes([still(HOME, 20), line(HOME, shifted(HOME, 3000, 0), 5)])
        for index, fix in enumerate(fixes):
            candidate = advance(candidate, {**fix, "id": index + 1, "speed_mps": None, "speed_origin": None},
                                ANALYSIS_DEFAULTS, stays_found)
        (stay,) = stays_found.kept.values()
        self.assertEqual((stay["kind"], stay["count"], stay["ongoing"]), ("stationary", 20, False))
        self.assertEqual(stays_found.withdrawn, [])

    def test_repositories_hand_out_plain_rows_and_chosen_columns(self):
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9})
        self.assertIs(type(self.db.points.live_point("phone")), dict)
        self.assertIs(type(self.db.points.prior_fix("phone", "2026-09-26T00:00:00+00:00")), dict)
        place = save_place(self.db, {"name": "Market", "lat": 41.01, "lon": -87.9})
        reminder = create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        # Legacy columns stay inside: what a reminder reads as is chosen, not whatever the table holds.
        self.assertFalse({"repeat", "cooldown_minutes"} & set(reminder))
        self.assertFalse({"repeat", "cooldown_minutes"} & set(self.db.reminders.active()[0]))

    def test_a_page_keeps_each_lookup_at_once_without_holding_the_database(self):
        # Reading a day can look up many places; each is kept as it arrives, so the write lock
        # is never held across the provider calls between them.
        found = ({"name": "Cafe", "place_name": "Cafe", "address": "1 Main St", "lat": HOME[0], "lon": HOME[1]}, "place")
        other = open_database(self.url)
        try:
            with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "key"}), \
                    patch("geologs.integrations.places.lookup", return_value=found), self.db.transaction():
                reverse_geocode(self.db, HOME[0], HOME[1])
                self.assertIsNotNone(other.caches.reverse_region("geoapify:nearest-v2:r150", HOME[0], HOME[1]))
        finally:
            other.close()

    def test_a_cached_lookup_does_not_wait_for_a_slow_provider(self):
        import threading
        from geologs.services import places
        self.db.caches.save_reverse_region("geoapify:nearest-v2:r150", HOME[0], HOME[1], 15,
                                           {"name": "Cafe", "place_name": "Cafe", "lat": HOME[0], "lon": HOME[1]})
        answered = []
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "key"}), places._provider_turn:  # another lookup is out
            thread = threading.Thread(target=lambda: answered.append(reverse_geocode(self.db, HOME[0], HOME[1])))
            thread.start()
            thread.join(2)
        self.assertEqual([item["cache"] for item in answered], ["region"])

    def test_a_bad_fuel_price_saves_none_of_the_fuel_settings(self):
        from geologs.services.fuel import update_fuel
        with self.assertRaises(ValueError):
            update_fuel(self.db, {"currency": "EUR", "fuel_economy_l100km": 7, "price_per_l": 500})
        settings = self.db.settings.get()
        self.assertEqual((settings["currency"], settings["fuel_economy_l100km"]), ("USD", None))
        self.assertEqual(self.db.fuel.table("manual"), [])

    def test_fired_reminders_are_kept_before_the_webhook_hears_of_them(self):
        from geologs.services.ingest import record_locations
        place = save_place(self.db, {"name": "Market", "lat": 41.01, "lon": -87.9, "radius_m": 100})
        create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        now = datetime.now(timezone.utc)
        seen = []
        def send(_target, event):
            other = open_database(self.url)
            try:
                seen.append([row["id"] for row in other.reminders.events()] == [event["id"]])
            finally:
                other.close()
            return "sent"
        with patch.dict(os.environ, {"GEOLOGS_WEBHOOK_URL": "https://hooks.example/geologs"}), \
                patch("geologs.integrations.webhooks.send", side_effect=send):
            record_locations(self.db, [{"source": "phone", "timestamp": (now - timedelta(minutes=1)).isoformat(), "lat": 41.0, "lon": -87.9},
                                       {"source": "phone", "timestamp": now.isoformat(), "lat": 41.01, "lon": -87.9}])
        self.assertEqual(seen, [True])
        self.assertEqual(self.db.reminders.events()[0]["webhook_status"], "sent")

    def test_webhook_signs_the_body_with_a_replay_protected_hmac(self):
        event = {"id": 99, "text": "Pick up salt", "place": "Walmart"}
        env = {"GEOLOGS_WEBHOOK_URL": "http://host.docker.internal:8644/webhooks/geologs-reminders",
               "GEOLOGS_WEBHOOK_SECRET": "shared-secret", "GEOLOGS_WEBHOOK_TOKEN": ""}
        with patch.dict(os.environ, env), patch("geologs.integrations.webhooks.urllib.request.urlopen", return_value=io.BytesIO()) as urlopen:
            deliver_webhook(self.db, [event])
        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        timestamp = headers["x-webhook-timestamp"]
        expected = hmac.new(b"shared-secret", timestamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-webhook-signature-v2"], expected)

    def test_reverse_cache_expires_and_coarse_results_have_no_assumed_area(self):
        self.assertEqual(reverse_cache_radius("city"), 0)
        self.assertEqual(reverse_cache_radius("building"), 15)
        self.assertEqual(reverse_cache_radius("street"), 30)
        self.db.caches.save_reverse_region("provider", 41, -87.9, 30, {"name": "First"})
        self.assertEqual(self.db.caches.reverse_region("provider", 41.0001, -87.9)["name"], "First")
        self.assertIsNone(self.db.caches.reverse_region("other", 41, -87.9))
        self.db.conn.execute("UPDATE reverse_regions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.db.commit()
        self.assertIsNone(self.db.caches.reverse_region("provider", 41, -87.9))

    def test_stays_frequent_places_transitions_and_day(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        batch = []
        for cycle in range(3):
            for label, lat, offset in (("A", 41.0, 0), ("B", 41.01, 25)):
                for minute in (0, 8, 16):
                    batch.append({"source": "phone", "external_id": f"{cycle}-{label}-{minute}",
                                  "timestamp": (base + timedelta(hours=cycle, minutes=offset + minute)).isoformat(),
                                  "lat": lat, "lon": -87.9})
        for minute in (0, 8, 16):
            batch.append({"source": "phone", "external_id": f"3-A-{minute}",
                          "timestamp": (base + timedelta(hours=3, minutes=minute)).isoformat(),
                          "lat": 41, "lon": -87.9})
        ingest(self.db, batch)
        self.assertEqual(len(stays(self.db)), 7)
        self.assertEqual(len(place_patterns(self.db)[0]), 2)
        self.assertEqual(sum(t["count"] for t in transitions(self.db)), 6)
        self.assertEqual(predict_next(self.db)["frequency"], 1)
        self.assertEqual(day_summary(self.db, "2026-09-25", "UTC")["points"], 21)
        place = save_place(self.db, {"name": "University", "lat": 41, "lon": -87.9})
        # Fixes come every 8 minutes, so each visit lasts until shortly before the next fix elsewhere.
        self.assertEqual(place_visits(self.db, place["id"])["total_observed_minutes"], 84.4)
        ranked = visited_places(self.db)
        # A saved place is a zone: with nothing named right there, the place inside is known by the zone's name.
        university = next(item for item in ranked if item["zone"] == "University")
        self.assertEqual((university["display_name"], university["saved"], university["visits"]), ("University", False, 4))
        self.assertGreaterEqual(ranked[0]["observed_minutes"], ranked[1]["observed_minutes"])
        insight = insights(self.db)
        self.assertEqual(insight["unlabeled_places"][0]["visits"], 3)
        self.assertNotIn("Unlabeled", insight["unlabeled_places"][0]["display_name"])
        mcp = dispatch(self.db, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": "get_frequent_places", "arguments": {"limit": 10}}})
        self.assertIn("University", [item["name"] for item in mcp["result"]["structuredContent"]["result"]])

    def test_resolved_stays_at_same_provider_place_are_merged(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [
            (0, 41.0000), (16, 41.0000),
            (17, 41.0005), (20, 41.0010), (36, 41.0010),
            (37, 41.0015), (40, 41.0020), (56, 41.0020),
        ]
        ingest(self.db, [{"source": "phone", "external_id": str(minute),
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "accuracy_m": 5}
                           for minute, lat in fixes])
        resolved = {"name": "Lake Example", "address": "Lake Example, Chicago",
                    "category": "natural.water", "categories": ["natural.water"],
                    "provider_place_id": "geoapify-lake-1"}
        with patch("geologs.services.places.place_for_coordinate", return_value=resolved):
            places = visited_places(self.db)
            day = day_summary(self.db, "2026-09-25", "UTC", "phone")
            pattern = insights(self.db)["frequent_places"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "Lake Example")
        self.assertEqual(places[0]["category"], "natural.water")
        # Three stays four minutes apart along one shore are one visit.
        self.assertEqual(places[0]["visits"], 1)
        self.assertEqual(len(day["visited_places"]), 1)
        self.assertEqual(len(day["trips"]), 0)
        self.assertEqual([(stay["started_at"], stay["ended_at"]) for stay in day["stays"]],
                         [("2026-09-25T10:00:00+00:00", "2026-09-25T10:56:00+00:00")])
        self.assertEqual(day["stays"][0]["observed_minutes"], 56)
        self.assertEqual(len(pattern), 1)

    def test_nearby_resolved_stays_with_same_name_are_merged(self):
        clusters = [
            {"cluster_id": 0, "place_id": None, "name": None, "lat": 41, "lon": -87.9,
             "visits": 1, "observed_minutes": 20, "last_seen": "2026-09-25T10:20:00+00:00",
             "display_name": "41.00000, -87.90000"},
            {"cluster_id": 1, "place_id": None, "name": None, "lat": 41.01, "lon": -87.9,
             "visits": 1, "observed_minutes": 20, "last_seen": "2026-09-25T11:20:00+00:00",
             "display_name": "41.01000, -87.90000"},
        ]
        labels = [
            {"name": "Lake Example", "address": "North shore", "category": "natural.water",
             "categories": ["natural.water"], "provider_place_id": "north"},
            {"name": "  lake   example ", "address": "South shore", "category": "natural.water",
             "categories": ["natural.water"], "provider_place_id": "south"},
        ]
        with patch("geologs.services.places.place_for_coordinate", side_effect=labels):
            merged, mapping = resolve_place_patterns(self.db, (clusters, {10: 0, 11: 1}))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["visits"], 2)
        self.assertEqual(mapping[10], mapping[11])

    def test_saved_place_keeps_provider_metadata(self):
        place = save_place(self.db, {"name": "Walmart", "lat": 41, "lon": -87.9,
                                       "address": "100 Main Street", "category": "commercial.supermarket",
                                       "provider_place_id": "geoapify-store-1"})
        self.assertEqual(place["category"], "commercial.supermarket")
        self.assertEqual(place["provider_place_id"], "geoapify-store-1")
        self.assertEqual(self.db.places.saved()[0]["category"], "commercial.supermarket")

    def test_late_import_rebuilds_affected_source_stays(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def put(id_, minute):
            ingest(self.db, {"source": "phone", "external_id": str(id_),
                               "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                               "lat": 41, "lon": -87.9})
        put(1, 0)
        put(3, 16)
        self.assertEqual(len(stays(self.db)), 1)
        put(2, 8)
        self.assertEqual(stays(self.db)[0]["point_count"], 3)

    def test_repeated_home_assistant_samples_at_one_spot_are_one_stay_across_a_gap(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def poll(minute):
            return ingest(self.db, {"source": "home_assistant:phone", "external_id": "unchanged-state",
                                      "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                                      "lat": 41, "lon": -87.9, "accuracy_m": 4})
        poll(0)
        self.assertEqual(poll(16)["recorded"], 1)
        self.assertEqual(len(self.db.points.points()), 1)
        self.assertEqual(len(stays(self.db)), 1)
        self.assertEqual(stays(self.db)[0]["point_count"], 2)
        poll(50)
        poll(66)
        # Still at the same spot after a quiet half hour: one stay, not two.
        rows = stays(self.db)
        self.assertEqual([(row["point_count"], row["observed_minutes"]) for row in rows], [(4, 66)])

    def test_existing_raw_samples_bootstrap_stays_when_opened(self):
        path = scratch_database(self)
        archive = open_database(path)
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        for minute in (0, 16):
            ingest(archive, {"source": "home_assistant:phone", "external_id": "same-state",
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": 41, "lon": -87.9, "accuracy_m": 4}, process_analysis=False)
        self.assertEqual(len(stays(archive)), 0)
        archive.close()
        as_if_restarted(path)
        reopened = open_database(path)
        try:
            self.assertEqual(len(stays(reopened)), 1)
            self.assertEqual(stays(reopened)[0]["point_count"], 2)
        finally:
            reopened.close()

    def test_stay_threshold_migration_removes_old_short_stays(self):
        path = scratch_database(self)
        archive = open_database(path)
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        ingest(archive, [
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 41, "lon": -87.9, "accuracy_m": 5}
            for minute in (0, 6)
        ])
        observations = list(reversed(archive.points.observations(limit=None)))
        archive.conn.execute("""INSERT INTO observed_stays_v2
            (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing)
            VALUES (?,?,?,?,?,?,?,?,1)""",
            ("phone", observations[0]["id"], observations[1]["id"], observations[0]["timestamp"],
             observations[1]["timestamp"], 41, -87.9, 2))
        archive.commit()
        archive.close()
        older_schema(path, 8, 12, 14, 17, 18, 19, 20, 21)
        migrated = open_database(path)
        try:
            self.assertEqual(stays(migrated), [])
            self.assertEqual(migrated._missing_migrations(STAY_REBUILDS), [])
        finally:
            migrated.close()

    def test_takeout_import_is_idempotent_and_does_not_trigger_reminders(self):
        place = save_place(self.db, {"name": "Market", "lat": 41, "lon": -87.9})
        create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        content = json.dumps({"locations": [
            {"timestampMs": "1600000000000", "latitudeE7": 410000000, "longitudeE7": -879000000, "accuracy": 12},
            {"timestamp": "2020-09-13T12:30:00Z", "latitudeE7": 410000001, "longitudeE7": -879000000},
            {"timestamp": "2020-09-13T12:31:00Z"}]})
        first = import_takeout(self.db, io.StringIO(content))
        second = import_takeout(self.db, io.StringIO(content))
        self.assertEqual((first["inserted"], first["skipped"]), (2, 1))
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(self.db.reminders.events(), [])

    def test_lake_run_between_drives_is_a_running_area_stay(self):
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 100})
        # Home Assistant holds each position for two polls; timing a move from
        # the last poll instead of the first would make the run look like a drive.
        for fix in lake_run_day(hold=2):
            ingest(self.db, fix)
        rows = list(reversed(stays(self.db, limit=None)))
        self.assertEqual([row["kind"] for row in rows], ["stationary", "area", "stationary"])
        lake = rows[1]
        self.assertEqual(lake["activity"], "running")
        self.assertGreaterEqual(lake["observed_minutes"], 55)
        self.assertLess(metres_from(lake, LAKE), 100)
        self.assertAlmostEqual(lake["radius_m"], 500, delta=60)
        self.assertAlmostEqual(lake["distance_m"] / 1000, 9, delta=1)
        live = [(row["kind"], row["started_at"], row["ended_at"]) for row in rows]
        process_stays_rebuild(self.db)
        self.assertEqual([(row["kind"], row["started_at"], row["ended_at"]) for row in reversed(stays(self.db, limit=None))], live)

        found = {"name": "Lake Example", "address": "Lake Example, Chicago", "category": "natural.water",
                 "categories": ["natural.water"], "provider_place_id": "lake-1"}
        nothing = {"name": "", "named": False, "address": "", "category": "", "categories": [], "provider_place_id": ""}
        # The provider knows the lake, not Home's street.
        lookup = lambda _db, lat, lon, *_args: found if metres_between(LAKE, {"lat": lat, "lon": lon}) < 1000 else nothing
        with patch("geologs.services.places.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.db, "2026-09-27", "UTC")
            overview = daily_overview(self.db, "2026-09-27", "UTC")
        self.assertEqual([trip["mode"] for trip in day["trips"]], ["driving", "driving"])
        self.assertEqual((day["activity_counts"]["running"], day["activity_counts"]["driving"]), (1, 2))
        self.assertEqual([place["name"] for place in day["visited_places"]], ["Lake Example"])
        self.assertEqual(day["visited_places"][0]["activities"], {"running": 1})
        self.assertTrue(day["stays"][1]["route_segments"])
        self.assertEqual([(visit["place"], visit["activity"]) for visit in overview["visits"]],
                         [("Home", None), ("Lake Example", "running"), ("Home", None)])
        self.assertRegex(overview["overview"], r"running 6\d min \(9\.\d km\)|running 6\d min \(8\.\d km\)")

    def test_round_trip_from_home_is_a_loop_trip_not_a_visit(self):
        walk = laps(shifted(HOME, 150, 0), 150, 20, 4.5)
        ingest(self.db, minute_fixes([still(HOME, 30), walk, still(HOME, 30)]))
        self.assertEqual([row["kind"] for row in stays(self.db, limit=None)], ["stationary", "stationary"])
        trips = day_summary(self.db, "2026-09-27", "UTC")["trips"]
        self.assertEqual([(trip["mode"], trip["loop"]) for trip in trips], [("walking", True)])
        self.assertGreater(trips[0]["distance_km"], 1)

    def test_errands_driven_with_short_stops_are_not_an_area_stay(self):
        save_settings(self.db, {"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
        a, b, c = shifted(HOME, 3000, 0), shifted(HOME, 3000, 1500), shifted(HOME, 1500, 1500)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, a, 6), still(a, 8), line(a, b, 4), still(b, 6),
                                        line(b, c, 4), still(c, 9), line(c, HOME, 5), still(HOME, 30)]))
        self.assertEqual([row["kind"] for row in stays(self.db, limit=None)], ["stationary", "stationary"])
        trips = day_summary(self.db, "2026-09-27", "UTC")["trips"]
        self.assertEqual([trip["mode"] for trip in trips], ["driving"])

    def test_indoor_jitter_at_a_large_store_is_one_visit(self):
        db = shifted(HOME, 6000, 2000)
        jitter = [shifted(db, 70 * math.sin(index * 2.3), 50 * math.cos(index * 1.7)) + (35,) for index in range(40)]
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, db, 10), jitter, line(db, HOME, 10), still(HOME, 30)]))
        rows = list(reversed(stays(self.db, limit=None)))
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(rows[1]["observed_minutes"], 35)
        self.assertLess(metres_from(rows[1], db), 60)

    def test_inaccurate_and_stray_fixes_do_not_split_a_stay(self):
        fixes = still(HOME, 20) + [(HOME[0] + .003, HOME[1], 300)] + still(HOME, 10) + [shifted(HOME, 80, 0) + (20,)] + still(HOME, 10)
        ingest(self.db, minute_fixes([fixes]))
        rows = stays(self.db, limit=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_minutes"], 41)

    def test_departure_confirmed_after_stray_fixes_keeps_stay_end(self):
        away = line(HOME, shifted(HOME, 3000, 0), 6)
        for fix in minute_fixes([still(HOME, 20), away]):
            ingest(self.db, fix)
        row = stays(self.db, limit=None)[0]
        self.assertEqual((row["ongoing"], row["observed_minutes"]), (0, 19))
        timeline = list(reversed(self.db.points.timeline(limit=None)))
        self.assertEqual([point["role"] for point in timeline[:2]], ["arrival", "departure"])

    def test_stay_split_by_gps_noise_is_reported_as_one_visit(self):
        db = shifted(HOME, 4000, 0)
        strays = [shifted(db, 80, 0), shifted(db, 85, 10), shifted(db, 80, 20)]
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, db, 6), still(db, 16), strays,
                                        still(db, 17), line(db, HOME, 6), still(HOME, 30)]))
        self.assertEqual(len(stays(self.db, limit=None)), 4)
        label = {"name": "Costco Wholesale", "address": "", "category": "commercial.supermarket",
                 "categories": ["commercial.supermarket"], "provider_place_id": "costco"}
        home = {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
        def resolve(_store, lat, lon, _radius=None):
            return label if lon > HOME[1] + .01 else home
        with patch("geologs.services.places.place_for_coordinate", side_effect=resolve):
            found = search_visits(self.db, "costco", timezone_name="UTC")
            day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual(found["total_visits"], 1)
        self.assertEqual(found["places"][0]["visits"], 1)
        self.assertGreaterEqual(found["visits"][0]["observed_minutes"], 35)
        self.assertEqual([stay["display_name"] for stay in day["stays"]], ["Home Street", "Costco Wholesale", "Home Street"])

    def test_tracker_without_stationary_stays_matches_rebuild_when_live(self):
        park, track = shifted(HOME, 3000, 0), shifted(HOME, 9000, 400)
        fixes = minute_fixes([line(HOME, shifted(park, -300, 0), 8), laps(park, 300, 30, 5),
                              line(shifted(park, -300, 0), shifted(track, 0, -400), 10), laps(track, 400, 25, 9),
                              line(shifted(track, 0, -400), HOME, 12)], source="car")
        for fix in fixes:
            ingest(self.db, fix)
        signature = lambda: [(row["kind"], row["activity"], row["started_at"], row["ended_at"], row["went_elsewhere"])
                             for row in stays(self.db, limit=None)]
        live = signature()
        self.assertEqual([(kind, activity) for kind, activity, *_ in reversed(live)], [("area", "walking"), ("area", "running")])
        process_stays_rebuild(self.db)
        self.assertEqual(signature(), live)

    def campus_day(self, middle, extra=None):
        """Home, drive to campus, `middle`, drive home; campus coordinates geocode to University."""
        library = CAMPUS
        fixes = minute_fixes([still(HOME, 30), line(HOME, library, 12), *middle, line(middle[-1][-1][:2], HOME, 12), still(HOME, 30)])
        ingest(self.db, fixes)
        if extra:
            ingest(self.db, extra)
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            return (day_summary(self.db, "2026-09-27", "UTC"), search_visits(self.db, "university", timezone_name="UTC"),
                    daily_overview(self.db, "2026-09-27", "UTC"))

    def test_walking_between_buildings_is_one_visit_not_a_trip(self):
        day, found, overview = self.campus_day([still(CAMPUS, 90), line(CAMPUS, LAB, 7), still(LAB, 80)])
        self.assertEqual([stay["display_name"] for stay in day["stays"]], ["Home Street", "University", "Home Street"])
        university = day["stays"][1]
        self.assertEqual([(move["mode"], move["distance_km"]) for move in university["internal_moves"]], [("walking", 0.4)])
        self.assertGreaterEqual(university["observed_minutes"], 170)
        self.assertEqual([trip["mode"] for trip in day["trips"]], ["driving", "driving"])
        self.assertEqual((found["total_visits"], found["places"][0]["visits"]), (1, 1))
        self.assertEqual(next(place for place in day["places_including_home"] if place["name"] == "University")["visits"], 1)
        self.assertEqual(overview["visits"][1]["moved_within"], [{"mode": "walking", "distance_km": 0.4}])

    def test_indoor_gps_jump_is_not_a_drive(self):
        jump = [shifted(CAMPUS, 420, 0) + (65,)] * 3
        day, found, _overview = self.campus_day([still(CAMPUS, 60), jump, still(CAMPUS, 60)])
        self.assertEqual([stay["display_name"] for stay in day["stays"]], ["Home Street", "University", "Home Street"])
        self.assertEqual(day["stays"][1]["internal_moves"], [])
        self.assertEqual(len(day["trips"]), 2)
        self.assertEqual(found["total_visits"], 1)

    def test_car_moved_between_campus_lots_is_driving_within_the_visit(self):
        lot_a, lot_b = shifted(CAMPUS, -200, -300), shifted(CAMPUS, 900, 500)
        day, _found, _overview = self.campus_day([line(CAMPUS, lot_a, 1), still(lot_a, 90), line(lot_a, lot_b, 4), still(lot_b, 80)])
        moves = [move for stay in day["stays"] for move in stay["internal_moves"]]
        self.assertEqual([move["mode"] for move in moves], ["driving"])
        self.assertEqual([stay["display_name"] for stay in day["stays"]].count("University"), 1)

    def test_lunch_off_campus_is_a_round_trip(self):
        save_settings(self.db, {"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
        taco = shifted(CAMPUS, 2500, 0)
        day, found, _overview = self.campus_day([still(CAMPUS, 60), line(CAMPUS, taco, 5), still(taco, 10),
                                                 line(taco, CAMPUS, 5), still(CAMPUS, 60)])
        loops = [trip for trip in day["trips"] if trip.get("loop")]
        self.assertEqual([(trip["mode"], trip["went_elsewhere"]) for trip in loops], [("driving", True)])
        self.assertEqual(found["total_visits"], 2)

    def test_overlapping_sources_never_place_you_in_two_spots(self):
        tablet = minute_fixes([still(HOME, 200)], source="old_tablet", start=datetime(2026, 9, 27, 14, tzinfo=timezone.utc))
        day, _found, _overview = self.campus_day([still(CAMPUS, 180)], extra=tablet)
        spans = sorted([(stay["started_at"], stay["ended_at"]) for stay in day["stays"]]
                       + [(trip["start"], trip["end"]) for trip in day["trips"]])
        for (_, first_end), (second_start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(first_end, second_start)
        self.assertEqual(day["timeline_sources"][0], "phone")
        self.assertIn("University", [stay["display_name"] for stay in day["stays"]])

    def test_quiet_hours_at_the_same_place_are_one_visit(self):
        # An app that reports only while moving: two quiet hours on campus.
        before = minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), still(CAMPUS, 60)])
        after = minute_fixes([still(CAMPUS, 60), line(CAMPUS, HOME, 12), still(HOME, 30)], start=datetime(2026, 9, 27, 17, tzinfo=timezone.utc))
        ingest(self.db, before + after)
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual(day["coverage_gaps"], [])
        campus = [stay for stay in day["stays"] if metres_between(CAMPUS, stay) < 100]
        self.assertEqual([(stay["started_at"], stay["ended_at"]) for stay in campus],
                         [("2026-09-27T13:41:00+00:00", "2026-09-27T17:59:00+00:00")])

    def test_tracker_priority_decides_conflicting_sources(self):
        # A tablet left at home all afternoon sends more samples than the phone you carried to campus.
        tablet = minute_fixes([still(HOME, 200)], source="old_tablet", start=datetime(2026, 9, 27, 14, tzinfo=timezone.utc))
        phone = minute_fixes([still(CAMPUS, 180)], source="phone", start=datetime(2026, 9, 27, 14, 10, tzinfo=timezone.utc))
        ingest(self.db, tablet + phone)
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            busiest = day_summary(self.db, "2026-09-27", "UTC")
            save_settings(self.db, {"source_priority": ["phone", "old_tablet"]})
            ranked = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual((busiest["timeline_sources"][0], ranked["timeline_sources"][0]), ("old_tablet", "phone"))
        self.assertIn("University", [stay["display_name"] for stay in ranked["stays"]])
        self.assertEqual(self.db.settings.get()["source_priority"], ["phone", "old_tablet"])
        with self.assertRaises(ValueError):
            save_settings(self.db, {"source_priority": "phone"})

    def test_current_location_prefers_the_top_tracker_while_it_is_fresh(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        ingest(self.db, [{"source": "phone", "timestamp": (now - timedelta(minutes=5)).isoformat(), "lat": CAMPUS[0], "lon": CAMPUS[1]},
                           {"source": "tablet", "timestamp": now.isoformat(), "lat": HOME[0], "lon": HOME[1]}])
        self.assertEqual(latest(self.db)["source"], "tablet")  # newest wins without a priority
        save_settings(self.db, {"source_priority": ["phone"]})
        self.assertEqual(latest(self.db)["source"], "phone")
        ingest(self.db, {"source": "tablet", "timestamp": (now + timedelta(minutes=20)).isoformat(), "lat": HOME[0], "lon": HOME[1]})
        self.assertEqual(latest(self.db)["source"], "tablet")  # the phone has gone quiet for 25 minutes

    def test_coverage_gap_between_different_places_is_still_reported(self):
        # Silent overnight and the next fix 300 km away: no way to know when you left.
        far = shifted(HOME, 300000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30)]) + minute_fixes([still(far, 30)], start=datetime(2026, 9, 27, 20, tzinfo=timezone.utc)))
        day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual(len(day["coverage_gaps"]), 1)
        home = next(stay for stay in day["stays"] if metres_between(HOME, stay) < 100)
        self.assertEqual(home["ended_at"], "2026-09-27T13:29:00+00:00")

    def test_went_elsewhere_flags_match_rebuild_when_ingested_live(self):
        taco = shifted(CAMPUS, 2500, 0)
        for fix in minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), still(CAMPUS, 60), line(CAMPUS, LAB, 7), still(LAB, 40),
                                 line(LAB, taco, 5), still(taco, 10), line(taco, LAB, 5), still(LAB, 40)]):
            ingest(self.db, fix)
        live = [(row["started_at"], row["went_elsewhere"]) for row in stays(self.db, limit=None)]
        # Home -> campus and campus -> lab go straight; lab -> taco place -> lab went elsewhere.
        # The taco place is a quick stop; flags still compare stays, as a rebuild does.
        self.assertEqual([(row["kind"], row["went_elsewhere"]) for row in reversed(stays(self.db, limit=None))],
                         [("stationary", None), ("stationary", 0), ("stationary", 0), ("stop", None), ("stationary", 1)])
        process_stays_rebuild(self.db)
        self.assertEqual([(row["started_at"], row["went_elsewhere"]) for row in stays(self.db, limit=None)], live)

    def test_place_is_named_at_the_sample_weighted_average_of_its_stays(self):
        def row(id_, east, count, radius=None):
            lat, lon = shifted(CAMPUS, east, 0)
            return {"id": id_, "source": "phone", "lat": lat, "lon": lon, "place_id": None, "radius_m": radius,
                    "activity": None, "started_at": f"2026-09-2{id_}T10:00:00+00:00", "ended_at": f"2026-09-2{id_}T11:00:00+00:00",
                    "observed_minutes": 60, "point_count": count}
        clusters, mapping = place_patterns(self.db, rows=[row(1, 0, 10), row(2, 60, 30)])
        self.assertEqual(len(clusters), 1)
        self.assertLess(metres_from(clusters[0], shifted(CAMPUS, 45, 0)), 1)
        self.assertEqual(clusters[0]["extent_m"], 45)
        self.assertNotIn("_members", clusters[0])
        with patch("geologs.services.places.place_for_coordinate", return_value={"name": "University"}) as lookup:
            resolve_place_patterns(self.db, (clusters, mapping))
        lat, lon, radius = lookup.call_args.args[1:]
        self.assertEqual(round(lat, 6), round(clusters[0]["lat"], 6))
        self.assertEqual(radius, 45)

    def test_round_trip_names_where_it_stopped(self):
        save_settings(self.db, {"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
        db = shifted(HOME, 4000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, db, 8), still(db, 8), line(db, HOME, 8), still(HOME, 30)]))
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            day = day_summary(self.db, "2026-09-27", "UTC")
        trip = day["trips"][0]
        self.assertTrue(trip["loop"])
        self.assertEqual((trip["turnaround"]["name"], trip["turnaround"]["stop_minutes"]), ("Taco Place", 8))
        self.assertAlmostEqual(trip["turnaround"]["distance_m"], 4000, delta=20)
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            brief = daily_brief(daily_overview(self.db, "2026-09-27", "UTC"))
        self.assertRegex(brief["timeline"][1], r"^13:[23]\d-13:5\d drove \d\.\d km round trip from .+ via Taco Place$")

    def test_short_fast_looking_move_within_a_place_is_walking(self):
        day, _found, _overview = self.campus_day([still(CAMPUS, 90), line(CAMPUS, LAB, 1), still(LAB, 80)])
        self.assertEqual([move["mode"] for move in day["stays"][1]["internal_moves"]], ["walking"])

    def test_one_daily_summary_per_date_in_the_configured_timezone(self):
        save_settings(self.db, {"timezone": "UTC"})
        ingest(self.db, minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        daily_overview(self.db, "2026-09-27")
        # An agent asking in another zone or for one source gets an answer, but no second record.
        other_zone = daily_overview(self.db, "2026-09-27", "America/Chicago")
        one_source = daily_overview(self.db, "2026-09-27", source="phone")
        self.assertEqual((other_zone["timezone"], one_source["source"]), ("America/Chicago", "phone"))
        self.assertEqual([(row["date"], row["timezone"], row["source"]) for row in daily_overviews(self.db, )],
                         [("2026-09-27", "UTC", None)])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM daily_summaries").fetchone()["n"], 1)
        # Changing the timezone drops summaries stored for the old one.
        save_settings(self.db, {"timezone": "America/Chicago"})
        self.assertEqual(daily_overviews(self.db, ), [])
        daily_overview(self.db, "2026-09-27")
        self.assertEqual([row["timezone"] for row in daily_overviews(self.db, )], ["America/Chicago"])

    def test_upgrade_removes_duplicate_daily_summaries_and_keeps_reminders(self):
        path = scratch_database(self)
        archive = open_database(path)
        save_settings(archive, {"timezone": "UTC"})
        place = save_place(archive, {"name": "Market", "lat": 41, "lon": -87.9})
        reminder = create_reminder(archive, {"text": "Buy salt", "place_id": place["id"]})
        ingest(archive, {"source": "phone", "timestamp": "2026-09-27T10:00:00Z", "lat": 41, "lon": -87.9})
        point_id = archive.points.points()[0]["id"]
        archive.conn.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?)",
                           (reminder["id"], point_id, "2026-09-27T10:00:00+00:00"))
        for zone, source in (("UTC", ""), ("America/Chicago", ""), ("UTC", "phone")):
            archive.conn.execute("""INSERT INTO daily_summaries(summary_date,timezone,source,watermark,points,distance_km,
                trip_count,place_count,reminder_count,summary,created_at,updated_at) VALUES (?,?,?,?,5,0,0,0,0,'{}',?,?)""",
                ("2026-09-27", zone, source, "w", "2026-09-28T00:00:00+00:00", "2026-09-28T00:00:00+00:00"))
        archive.commit()
        archive.close()
        # Before name-only reminders, a reminder needed a place.
        older_schema(path, 15, 16, changes=["ALTER TABLE reminders DROP COLUMN match_name",
                                             "ALTER TABLE reminders ALTER COLUMN place_id SET NOT NULL"])
        migrated = open_database(path)
        try:
            self.assertEqual([(row["timezone"], row["source"]) for row in daily_overviews(migrated, )], [("UTC", None)])
            self.assertEqual(migrated.conn.execute("SELECT COUNT(*) AS n FROM daily_summaries").fetchone()["n"], 1)
            self.assertEqual([item["text"] for item in migrated.reminders.active()], ["Buy salt"])
            self.assertEqual(len(migrated.reminders.events()), 1)
            named = create_reminder(migrated, {"text": "Get beans", "match_name": "Starbucks"})
            self.assertIsNone(named["place_id"])
        finally:
            migrated.close()

    def test_name_only_reminder_fires_after_a_short_stop_near_a_matching_place(self):
        reminder = create_reminder(self.db, {"text": "Get coffee beans", "match_name": "starbuks"})
        self.assertEqual((reminder["place_name"], reminder["match_name"], reminder["place_id"]), ("starbuks", "starbuks", None))
        cafe = shifted(HOME, 5000, 0)
        nearby = [{"name": "Chipotle Mexican Grill", "distance_m": 12}, {"name": "Starbucks Coffee", "distance_m": 40},
                  {"name": "Starbucks Reserve", "distance_m": 400}]
        start = datetime.now(timezone.utc) - timedelta(minutes=20)
        fixes = minute_fixes([line(HOME, cafe, 10), still(cafe, 8)], start=start)
        events = []
        with patch("geologs.services.places.nearby_places", return_value=nearby) as lookup:
            for fix in fixes[:12]:  # two minutes still: not yet
                events += ingest(self.db, fix)["events"]
            self.assertEqual(events, [])
            for fix in fixes[12:]:
                events += ingest(self.db, fix)["events"]
        self.assertEqual([(event["text"], event["place"]) for event in events], [("Get coffee beans", "Starbucks Coffee")])
        self.assertEqual(lookup.call_count, 1)  # looked up once per stop, not on every poll
        self.assertEqual(self.db.reminders.active(), [])
        history = self.db.reminders.history()[0]
        self.assertEqual((history["status"], history["place_name"]), ("triggered", "starbuks"))
        self.assertEqual(self.db.reminders.events()[0]["place_name"], "starbuks")

    def test_name_only_reminder_ignores_other_places_and_old_data(self):
        create_reminder(self.db, {"text": "Get coffee beans", "match_name": "Starbucks"})
        with patch("geologs.services.places.nearby_places", return_value=[{"name": "Dunkin'", "distance_m": 5}]):
            start = datetime.now(timezone.utc) - timedelta(minutes=10)
            events = ingest(self.db, minute_fixes([still(HOME, 6)], start=start))["events"]
        self.assertEqual(events, [])
        with patch("geologs.services.places.nearby_places", return_value=[{"name": "Starbucks", "distance_m": 5}]) as lookup:
            old = ingest(self.db, minute_fixes([still(HOME, 6)], source="old", start=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual((old["events"], lookup.call_count), ([], 0))
        self.assertEqual(len(self.db.reminders.active()), 1)

    def test_place_name_matching_tolerates_spelling_and_punctuation(self):
        self.assertTrue(name_matches("starbucks", "Starbucks Coffee"))
        self.assertTrue(name_matches("starbuks", "STARBUCKS #1234"))
        self.assertTrue(name_matches("mcdonalds", "McDonald's"))
        self.assertTrue(name_matches("chick fil a", "Chick-fil-A"))
        self.assertTrue(name_matches("walgreens", "Walgreen Co"))
        self.assertFalse(name_matches("starbucks", "Dunkin'"))
        self.assertFalse(name_matches("target", "Walmart Supercenter"))
        self.assertFalse(name_matches("  ", "Starbucks"))

    def test_nearby_places_lists_candidates_nearest_first_and_caches_them(self):
        everyday = {"features": [
            {"properties": {"name": "Chipotle", "categories": ["catering.restaurant"], "distance": 12, "lat": 41.0001, "lon": -87.9}},
            {"properties": {"name": "Bank ATM", "categories": ["service.financial.atm"], "distance": 3, "lat": 41, "lon": -87.9}},
        ]}
        travel = {"features": [{"properties": {"name": "Hampton Inn", "categories": ["accommodation.hotel"], "distance": 90,
                                               "lat": 41.0008, "lon": -87.9}}]}
        responses = lambda path, params: travel if "accommodation" in params["categories"] else everyday
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": "", "MAPBOX_ACCESS_TOKEN": ""}), \
                patch("geologs.integrations.places.provider_request", side_effect=responses) as request:
            places = nearby_places(self.db, 41, -87.9, 250)
            again = nearby_places(self.db, 41.00005, -87.9, 250)
        self.assertEqual([place["name"] for place in places], ["Chipotle", "Hampton Inn"])
        self.assertEqual(places[0]["distance_m"], 11)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([place["name"] for place in again], ["Chipotle", "Hampton Inn"])
        self.assertEqual(request.call_args_list[0].args[1]["filter"], "circle:-87.9,41.0,300")

    def test_corrected_name_replaces_the_providers_guess_everywhere(self):
        cafe = shifted(HOME, 5000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, cafe, 8), still(cafe, 25), line(cafe, HOME, 8), still(HOME, 30)]))
        wrong = {"name": "Chipotle Mexican Grill", "address": "1 Strip Mall", "category": "catering.restaurant",
                 "categories": ["catering.restaurant"], "provider_place_id": "chipotle"}
        home = {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
        resolve = lambda _store, lat, lon, _radius=None: wrong if lon > HOME[1] + .01 else home
        with patch("geologs.services.places.place_for_coordinate", side_effect=resolve):
            before = day_summary(self.db, "2026-09-27", "UTC")
            self.assertEqual(before["stays"][1]["display_name"], "Chipotle Mexican Grill")
            visit = before["stays"][1]
            label = save_label(self.db, {"name": "Starbucks", "lat": visit["lat"], "lon": visit["lon"],
                                                 "category": "catering.cafe", "provider_place_id": "starbucks"})
            after = day_summary(self.db, "2026-09-27", "UTC")
            overview = daily_overview(self.db, "2026-09-27", "UTC")
            found = search_visits(self.db, "starbucks", timezone_name="UTC")
            missed = search_visits(self.db, "chipotle", timezone_name="UTC")
        self.assertEqual(label["radius_m"], 50)
        self.assertEqual(after["stays"][1]["display_name"], "Starbucks")
        corrected = next(place for place in after["visited_places"] if place["name"] == "Starbucks")
        self.assertEqual(corrected["label_id"], label["id"])
        self.assertNotIn("Chipotle Mexican Grill", [place["name"] for place in after["visited_places"]])
        self.assertIn("Starbucks", [visit["place"] for visit in overview["visits"]])
        self.assertEqual((found["total_visits"], missed["total_visits"]), (1, 0))
        # No saved place was created.
        self.assertEqual(self.db.places.saved(), [])
        # Renaming the same visit again replaces the correction; removing it restores the automatic name.
        again = save_label(self.db, {"name": "Starbucks Reserve", "lat": visit["lat"] + .0001, "lon": visit["lon"]})
        self.assertEqual((again["id"], len(self.db.places.labels())), (label["id"], 1))
        delete_label(self.db, label["id"])
        with patch("geologs.services.places.place_for_coordinate", side_effect=resolve):
            self.assertEqual(day_summary(self.db, "2026-09-27", "UTC")["stays"][1]["display_name"], "Chipotle Mexican Grill")

    def test_visit_only_correction_leaves_other_visits_to_the_same_spot_alone(self):
        cafe = shifted(HOME, 5000, 0)
        for day in (26, 27):
            ingest(self.db, minute_fixes([still(cafe, 30)], start=datetime(2026, 9, day, 15, tzinfo=timezone.utc)))
        wrong = {"name": "Chipotle Mexican Grill", "address": "", "category": "catering.restaurant",
                 "categories": ["catering.restaurant"], "provider_place_id": "chipotle"}
        names = lambda: {stay["started_at"][:10]: stay["place_name"] for stay in stays(self.db, limit=None)}
        first = next(stay for stay in stays(self.db, limit=None) if stay["started_at"].startswith("2026-09-26"))
        only_first = save_label(self.db, {"name": "Starbucks", "lat": first["lat"], "lon": first["lon"], "source": first["source"],
                                                  "started_at": first["started_at"], "ended_at": first["ended_at"]})
        self.assertEqual((only_first["source"], only_first["started_at"]), ("phone", first["started_at"]))
        self.assertEqual(names(), {"2026-09-26": "Starbucks", "2026-09-27": None})
        with patch("geologs.services.places.place_for_coordinate", return_value=wrong):
            self.assertEqual(day_summary(self.db, "2026-09-27", "UTC")["stays"][0]["display_name"], "Chipotle Mexican Grill")
            self.assertEqual([visit["date"] for visit in search_visits(self.db, "starbucks", timezone_name="UTC")["visits"]], ["2026-09-26"])
        # An every-visit correction names both days; the visit-only one still wins for its own visit.
        everywhere = save_label(self.db, {"name": "Coffee Corner", "lat": first["lat"], "lon": first["lon"]})
        self.assertEqual(names(), {"2026-09-26": "Starbucks", "2026-09-27": "Coffee Corner"})
        # Correcting the same visit again replaces it; another visit gets its own.
        again = save_label(self.db, {"name": "Starbucks Reserve", "lat": first["lat"], "lon": first["lon"],
                                             "source": "phone", "started_at": first["started_at"], "ended_at": first["ended_at"]})
        self.assertEqual(again["id"], only_first["id"])
        self.assertEqual(len(self.db.places.labels()), 2)
        self.assertNotEqual(everywhere["id"], only_first["id"])
        # Every-visit corrections only name the spot itself when there is no visit to match.
        from geologs.services.places import place_for_coordinate
        self.assertEqual(place_for_coordinate(self.db, first["lat"], first["lon"])["name"], "Coffee Corner")
        with self.assertRaisesRegex(ValueError, "together"):
            save_label(self.db, {"name": "X", "lat": 41, "lon": -87.9, "started_at": first["started_at"]})

    def test_rename_without_an_address_looks_up_the_spot_s_own_address(self):
        from geologs.services.saved_places import save_place_label
        house = {"name": "742 Evergreen Terrace", "place_name": "", "address": "742 Evergreen Terrace"}
        cafe_nearby = {"name": "Corner Cafe", "place_name": "Corner Cafe", "address": "10 Main St",
                       "place_lat": 41.0010, "place_lon": -87.9}
        with patch("geologs.services.places.reverse_geocode", return_value=house):
            friend = save_place_label(self.db, {"name": "Alex", "lat": 41, "lon": -87.9})
        with patch("geologs.services.places.reverse_geocode", return_value=cafe_nearby):
            other = save_place_label(self.db, {"name": "Sam", "lat": 41, "lon": -87.8})
        with patch("geologs.services.places.reverse_geocode", side_effect=AssertionError("looked up a given address")):
            typed = save_place_label(self.db, {"name": "Kim", "lat": 41, "lon": -87.7, "address": "1 Typed Rd"})
        with patch("geologs.services.places.reverse_geocode", side_effect=ValueError("no places provider")):
            offline = save_place_label(self.db, {"name": "Lee", "lat": 41, "lon": -87.6})
        self.assertEqual(friend["address"], "742 Evergreen Terrace")
        # A named place 110 m away is a neighbour, not this spot: its address is not borrowed.
        self.assertEqual(other["address"], "")
        self.assertEqual(typed["address"], "1 Typed Rd")
        self.assertEqual(offline["address"], "")

    def test_current_location_keeps_a_visit_only_correction_while_you_are_there(self):
        cafe = shifted(HOME, 5000, 0)
        start = datetime.now(timezone.utc) - timedelta(minutes=40)
        fixes = minute_fixes([still(cafe, 40)], start=start)
        ingest(self.db, fixes[:30])
        visit = stays(self.db, limit=None)[0]
        save_label(self.db, {"name": "Starbucks", "lat": visit["lat"], "lon": visit["lon"], "source": "phone",
                                     "started_at": visit["started_at"], "ended_at": visit["ended_at"]})
        ingest(self.db, fixes[30:])  # still there, past the corrected window
        with patch("geologs.services.places.place_for_coordinate", return_value={"name": "Chipotle", "address": "", "category": "",
                                                                       "provider_place_id": ""}):
            self.assertEqual(current_location(self.db)["place_name"], "Starbucks")

    def test_correction_inside_a_large_saved_place_wins_only_where_it_points(self):
        save_place(self.db, {"name": "Campus", "lat": CAMPUS[0], "lon": CAMPUS[1], "radius_m": 600})
        save_label(self.db, {"name": "Ottenheimer Library", "lat": CAMPUS[0], "lon": CAMPUS[1]})
        day, _found, _overview = self.campus_day([still(CAMPUS, 60), line(CAMPUS, shifted(LAB, 0, 3000), 8),
                                                   line(shifted(LAB, 0, 3000), LAB, 8), still(LAB, 40)])
        # The saved Campus is a zone: the lab is named by the provider, and keeps the zone.
        self.assertEqual([(stay["display_name"], stay["zone"]) for stay in day["stays"]],
                         [("Home Street", None), ("Ottenheimer Library", "Campus"), ("University", "Campus"), ("Home Street", None)])
        latest = {"source": "phone", "timestamp": datetime.now(timezone.utc).isoformat(), "lat": CAMPUS[0], "lon": CAMPUS[1]}
        ingest(self.db, latest)
        self.assertEqual(current_location(self.db)["place_name"], "Ottenheimer Library")

    def buildings_lookup(self, buildings):
        """A provider that knows named buildings at their own coordinates, and only addresses elsewhere."""
        from geologs.domain.geo import metres
        def lookup(_store, lat, lon, _radius=None):
            name, spot = min(buildings.items(), key=lambda item: metres(lat, lon, *item[1]))
            return {"name": name, "named": True, "address": f"{name} address", "category": "", "categories": [],
                    "provider_place_id": name, "place_lat": spot[0], "place_lon": spot[1]}
        return lookup

    def test_saved_place_is_a_zone_holding_the_buildings_visited_inside_it(self):
        university = save_place(self.db, {"name": "University", "lat": CAMPUS[0], "lon": CAMPUS[1], "radius_m": 600})
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), still(CAMPUS, 90), line(CAMPUS, LAB, 7),
                                        still(LAB, 80), line(LAB, HOME, 12), still(HOME, 30)]))
        lookup = self.buildings_lookup({"Engineering Hall": shifted(CAMPUS, 10, 5), "Library": shifted(LAB, -8, 0),
                                        "Home Street": HOME})
        with patch("geologs.services.places.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.db, "2026-09-27", "UTC")
            overview = daily_overview(self.db, "2026-09-27", "UTC")
            places = place_overview(self.db)
            found = search_visits(self.db, "university", timezone_name="UTC")
        self.assertEqual([(stay["display_name"], stay["zone"]) for stay in day["stays"]],
                         [("Home Street", None), ("Engineering Hall", "University"), ("Library", "University"), ("Home Street", None)])
        self.assertEqual([trip["mode"] for trip in day["trips"] if trip["from_name"] == "Engineering Hall"], ["walking"])
        # Totals for the saved place cover the whole campus: one visit, both buildings.
        self.assertEqual(places["saved"][str(university["id"])]["visits"], 1)
        self.assertEqual(places["saved"][str(university["id"])]["observed_minutes"], 170)
        self.assertEqual(overview["zones"], [{"name": "University", "observed_minutes": 170.0,
                                              "places": ["Engineering Hall", "Library"]}])
        brief = daily_brief(overview)
        self.assertEqual(brief["timeline"][2:5], ["13:41-15:11 at Engineering Hall (University)",
                                                  "15:11-15:18 walked 0.4 km from Engineering Hall to Library",
                                                  "15:18-16:38 at Library (University)"])
        self.assertEqual(brief["zones"], ["University: 2 h 50 min at Engineering Hall, Library"])
        self.assertEqual(sorted(place["name"] for place in found["places"]), ["Engineering Hall", "Library"])
        self.assertEqual({visit["zone"] for visit in found["visits"]}, {"University"})
        # One visit to the zone, walking between buildings included.
        self.assertEqual(place_visits(self.db, university["id"])["total_observed_minutes"], 177)

    def test_a_named_place_down_the_street_does_not_name_a_spot_inside_a_zone(self):
        house = shifted(HOME, 4000, 0)
        save_place(self.db, {"name": "Mom's house", "lat": house[0], "lon": house[1], "radius_m": 150})
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, house, 10), still(house, 60), line(house, HOME, 10), still(HOME, 30)]))
        # The provider's nearest named place is a shop 120 m away: fine for a stranger's
        # street, wrong inside a zone you saved, so the zone names it.
        lookup = self.buildings_lookup({"Corner Shop": shifted(house, 120, 0), "Home Street": HOME})
        with patch("geologs.services.places.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual([stay["display_name"] for stay in day["stays"]], ["Home Street", "Mom's house", "Home Street"])

    def test_home_names_everything_inside_it(self):
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1]})
        ingest(self.db, minute_fixes([still(HOME, 60)]))
        lookup = self.buildings_lookup({"Neighbour's Bakery": shifted(HOME, 15, 0)})
        with patch("geologs.services.places.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual([(stay["display_name"], stay["zone"]) for stay in day["stays"]], [("Home", "Home")])
        latest = {"source": "phone", "timestamp": datetime.now(timezone.utc).isoformat(), "lat": HOME[0], "lon": HOME[1]}
        ingest(self.db, latest)
        current = current_location(self.db)
        self.assertEqual((current["place_name"], current["zone"]), ("Home", "Home"))

    def test_deleting_a_saved_place_keeps_reminders_that_use_it(self):
        unused = save_place(self.db, {"name": "Gym", "lat": 41.1, "lon": -87.9})
        market = save_place(self.db, {"name": "Market", "lat": 41, "lon": -87.9})
        create_reminder(self.db, {"text": "Buy salt", "place_id": market["id"]})
        delete_place(self.db, unused["id"])
        delete_place(self.db, market["id"])
        self.assertEqual(self.db.places.saved(), [])
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM places WHERE id=?", (unused["id"],)).fetchone())
        # The reminder still knows where to fire and what to call it.
        self.assertEqual([(item["text"], item["place_name"]) for item in self.db.reminders.active()], [("Buy salt", "Market")])
        now = datetime.now(timezone.utc)
        ingest(self.db, {"source": "phone", "timestamp": (now - timedelta(minutes=5)).isoformat(), "lat": 41.01, "lon": -87.9})
        event = ingest(self.db, {"source": "phone", "timestamp": now.isoformat(), "lat": 41, "lon": -87.9})["events"][0]
        self.assertEqual(event["place"], "Market")
        # The name is free to use again.
        self.assertEqual(save_place(self.db, {"name": "Market", "lat": 41.2, "lon": -87.9})["name"], "Market")
        with self.assertRaisesRegex(ValueError, "saved place not found"):
            delete_place(self.db, unused["id"])

    def test_unnamed_places_on_one_street_are_one_place_despite_gps_drift(self):
        house, drifted = shifted(HOME, 4000, 0), shifted(HOME, 4140, 30)        # 143 m apart
        far_down, cross_street = shifted(HOME, 4600, 0), shifted(HOME, 4000, 120)
        cafe = shifted(HOME, 4000, -150)                                             # named, 150 m away
        labels = {house: "9004 Longacre Drive", drifted: "9109 Longacre Drive", far_down: "9600 Longacre Drive",
                  cross_street: "1200 Pine Street", cafe: "Corner Cafe"}
        def resolve(_store, lat, lon, _radius=None):
            spot = min(labels, key=lambda key: abs(key[0] - lat) + abs(key[1] - lon))
            named = labels[spot] == "Corner Cafe"
            address = labels[spot] if named else labels[spot] + ", Little Rock, Arkansas 72205, United States"
            return {"name": address, "named": named, "address": address, "category": "", "categories": [], "provider_place_id": ""}
        for day, spot, hours in ((20, house, 8), (21, drifted, 5), (22, house, 8), (23, far_down, 2), (24, cross_street, 2), (25, cafe, 1)):
            ingest(self.db, minute_fixes([still(spot, hours * 60)], start=datetime(2026, 9, day, 1, tzinfo=timezone.utc)))
        with patch("geologs.services.places.place_for_coordinate", side_effect=resolve):
            places = {place["name"].split(",")[0]: place["visits"] for place in visited_places(self.db)}
        # The drifted stay joins the place where most time was spent; the rest stay apart.
        self.assertEqual(places, {"9004 Longacre Drive": 3, "9600 Longacre Drive": 1, "1200 Pine Street": 1, "Corner Cafe": 1})
        self.assertEqual(street_key("9004 Longacre Drive, Little Rock, Arkansas 72205, United States"), "longacre drive|little rock")
        self.assertIsNone(street_key("Lake Willastein Park, Maumelle, AR"))
        self.assertIsNone(street_key("9004 Longacre Drive"))

    def test_saved_home_counts_its_visits_including_gps_drift_down_the_street(self):
        house, drifted = shifted(HOME, 4000, 0), shifted(HOME, 4170, 0)             # 170 m: outside Home's circle
        neighbour, elsewhere = shifted(HOME, 3850, 0), shifted(HOME, 4000, 400)
        street = ", Little Rock, Arkansas 72205, United States"
        home = save_place(self.db, {"name": "Home", "lat": house[0], "lon": house[1], "radius_m": 150, "address": "9004 Longacre Drive" + street})
        friend = save_place(self.db, {"name": "Sam's", "lat": neighbour[0], "lon": neighbour[1], "radius_m": 50, "address": "8950 Longacre Drive" + street})
        def resolve(_store, lat, lon, _radius=None):
            text = ("1300 Pine Street" if lat > house[0] + .002 else "9109 Longacre Drive") + street
            return {"name": text, "named": False, "address": text, "category": "", "categories": [], "provider_place_id": ""}
        for day, spot in ((20, house), (21, drifted), (22, house), (23, neighbour), (24, elsewhere)):
            ingest(self.db, minute_fixes([still(spot, 120)], start=datetime(2026, 9, day, 1, tzinfo=timezone.utc)))
        with patch("geologs.services.places.place_for_coordinate", side_effect=resolve):
            overview = place_overview(self.db)
        self.assertEqual(overview["saved"][str(home["id"])]["visits"], 3)
        self.assertEqual(overview["saved"][str(friend["id"])]["visits"], 1)
        self.assertEqual(sorted(place["name"].split(",")[0] for place in overview["visited"]), ["1300 Pine Street", "Sam's"])

    def test_a_day_with_nothing_recorded_is_summarized_but_not_stored(self):
        save_settings(self.db, {"timezone": "UTC"})
        ingest(self.db, minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        empty = daily_overview(self.db, "2026-09-28")
        self.assertEqual((empty["totals"]["raw_fixes"], daily_brief(empty)["summary"]), (0, "No location recorded."))
        with self.assertRaisesRegex(ValueError, "no summary is stored for 2026-09-28"):
            stored_daily_record(self.db, "2026-09-28")
        daily_overview(self.db, "2026-09-27")
        self.assertEqual([row["date"] for row in daily_overviews(self.db, )], ["2026-09-27"])

    def test_stored_daily_record_shows_what_is_stored_and_when_it_is_out_of_date(self):
        save_settings(self.db, {"timezone": "UTC"})
        ingest(self.db, minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(ValueError, "no summary is stored for 2026-09-27"):
            stored_daily_record(self.db, "2026-09-27")
        built = daily_overview(self.db, "2026-09-27")
        record = stored_daily_record(self.db, "2026-09-27")
        self.assertEqual((record["date"], record["timezone"], record["stale"]), ("2026-09-27", "UTC", False))
        self.assertEqual(record["summary"], built)
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-27T18:00:00Z", "lat": HOME[0], "lon": HOME[1]})
        self.assertTrue(stored_daily_record(self.db, "2026-09-27")["stale"])
        daily_overview(self.db, "2026-09-27")
        self.assertFalse(stored_daily_record(self.db, "2026-09-27")["stale"])

    def test_early_home_return_is_linked_to_both_daily_summaries(self):
        late = shifted(HOME, 3000, 0)
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 150})
        save_place(self.db, {"name": "Late Cafe", "lat": late[0], "lon": late[1], "radius_m": 150})
        start = datetime(2026, 9, 27, 21, 30, tzinfo=timezone.utc)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, late, 10), still(late, 190),
                                        line(late, HOME, 10), still(HOME, 45)], start=start))
        evening = daily_overview(self.db, "2026-09-27", "UTC")
        morning = daily_overview(self.db, "2026-09-28", "UTC")
        self.assertEqual((evening["home_return"]["timing"], morning["home_return"]["timing"]),
                         ("next_day", "after_midnight"))
        self.assertEqual(evening["home_return"]["returned_at"], morning["home_return"]["returned_at"])
        self.assertTrue(evening["home_return"]["summary"].endswith("the next day."))
        self.assertTrue(morning["home_return"]["summary"].endswith("after midnight."))
        self.assertIn(evening["home_return"]["summary"], evening["overview"])
        self.assertEqual(daily_brief(evening)["home_return"], evening["home_return"]["summary"])
        self.assertEqual(daily_brief(morning)["home_return"], morning["home_return"]["summary"])

    def test_prior_day_summary_becomes_stale_when_an_early_home_return_arrives(self):
        late = shifted(HOME, 3000, 0)
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 150})
        save_place(self.db, {"name": "Late Cafe", "lat": late[0], "lon": late[1], "radius_m": 150})
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, late, 10), still(late, 130)],
                                       start=datetime(2026, 9, 27, 21, tzinfo=timezone.utc)))
        self.assertIsNone(daily_overview(self.db, "2026-09-27", "UTC")["home_return"])
        ingest(self.db, minute_fixes([line(late, HOME, 10), still(HOME, 30)],
                                       start=datetime(2026, 9, 28, 0, 10, tzinfo=timezone.utc)))
        self.assertTrue(stored_daily_record(self.db, "2026-09-27")["stale"])
        refreshed = daily_overview(self.db, "2026-09-27", "UTC")
        self.assertEqual(refreshed["home_return"]["timing"], "next_day")
        self.assertFalse(stored_daily_record(self.db, "2026-09-27")["stale"])

    def test_daily_brief_states_only_what_happened_in_plain_text(self):
        save_settings(self.db, {"timezone": "America/Chicago"})
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1]})
        lake = save_place(self.db, {"name": "Lake Example", "lat": LAKE[0], "lon": LAKE[1], "radius_m": 600})
        create_reminder(self.db, {"text": "Stretch first", "place_id": lake["id"]})
        create_reminder(self.db, {"text": "Buy sunscreen", "match_name": "Walgreens"})
        self.db.conn.execute("UPDATE reminders SET created_at='2026-09-27T12:15:00+00:00'")
        for fix in lake_run_day():
            ingest(self.db, fix)
        # Reminders fire only on live fixes; record the lake arrival as if it had.
        point = self.db.conn.execute("SELECT id,timestamp FROM points ORDER BY ABS(lat-?)+ABS(lon-?) LIMIT 1", LAKE).fetchone()
        self.db.conn.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (1,?,?)", (point["id"], point["timestamp"]))
        self.db.commit()
        overview = daily_overview(self.db, "2026-09-27")
        brief = stored_daily_record(self.db, "2026-09-27")["brief"]
        self.assertEqual(brief, daily_brief(overview))
        self.assertEqual(list(brief), ["date", "summary", "timeline", "places", "reminders_set", "reminders_triggered"])
        self.assertEqual(brief["date"], "Sunday, 2026-09-27 (America/Chicago)")
        self.assertEqual(brief["summary"], "2 places, 2 trips (19.3 km). Drove 19.3 km (24 min), ran 9.0 km (1 h).")
        self.assertEqual(brief["timeline"], ["08:00-08:30 at Home",
                                             "08:30-08:42 drove 9.6 km from Home to Lake Example",
                                             "08:42-09:42 at Lake Example, ran 9.0 km",
                                             "09:42-09:54 drove 9.8 km from Lake Example to Home",
                                             # The latest stay: as of the last fix, not now.
                                             "from 09:54 at Home, still there at 10:24"])
        self.assertEqual(brief["places"], ["Home: 1 h over 2 visits", "Lake Example: 1 h, running"])
        self.assertEqual(brief["reminders_set"], ["07:15 'Stretch first' at Lake Example",
                                                  "07:15 'Buy sunscreen' at any place named Walgreens"])
        self.assertRegex(brief["reminders_triggered"][0], r"^0[89]:\d\d 'Stretch first' at Lake Example$")
        text = json.dumps(brief)
        for noise in ("false", "true", "null", "[]", "ongoing", '"lat"', "pending"):
            self.assertNotIn(noise, text)
        self.assertNotRegex(text, r"\d\d:\d\d:\d\d")
        self.assertLess(len(text), len(json.dumps(overview)) / 4)

        briefs = daily_briefs(self.db, "2026-09-27", "2026-09-28")
        self.assertEqual(briefs, [brief, {"date": "Monday, 2026-09-28 (America/Chicago)", "summary": "No location recorded."}])
        called = dispatch(self.db, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "get_daily_brief", "arguments": {"date": "2026-09-27"}}})
        self.assertEqual(called["result"]["structuredContent"]["result"], [brief])
        with self.assertRaisesRegex(ValueError, "end_date must be on or up to 30 days after date"):
            daily_briefs(self.db, "2026-09-27", "2026-09-26")
        with self.assertRaisesRegex(ValueError, "end_date must be on or up to 30 days after date"):
            daily_briefs(self.db, "2026-09-01", "2026-10-02")

    def test_travel_mode_corrections_override_the_speed_guess(self):
        office = shifted(HOME, 2000, 0)
        # Crawling 2 km in 25 minutes reads as walking at these speeds.
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, office, 25), still(office, 30)]))
        day = day_summary(self.db, "2026-09-27", "UTC")
        trip = day["trips"][0]
        self.assertEqual(trip["mode"], "walking")
        # Saved a couple of minutes off, as if the trip had been recalculated since.
        save_mode_override(self.db, {"source": trip["source"], "target": "trip", "mode": "driving",
                                       "started_at": (datetime.fromisoformat(trip["start"]) + timedelta(minutes=2)).isoformat(),
                                       "ended_at": (datetime.fromisoformat(trip["end"]) + timedelta(minutes=1)).isoformat()})
        corrected = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual((corrected["trips"][0]["mode"], corrected["trips"][0]["detected_mode"]), ("driving", "walking"))
        self.assertEqual((corrected["activity_totals"]["driving"]["count"], corrected["activity_totals"]["walking"]["count"]), (1, 0))
        self.assertTrue(daily_overview(self.db, "2026-09-27", "UTC")["trips"][0]["mode_corrected"])
        self.assertEqual([item["mode"] for item in anchored_trips(self.db, "2026-09-27T00:00:00Z", "2026-09-28T00:00:00Z")], ["driving"])
        # Setting it again replaces the correction; "auto" restores the guess.
        save_mode_override(self.db, {"source": trip["source"], "mode": "transit", "started_at": trip["start"], "ended_at": trip["end"]})
        self.assertEqual(len(self.db.corrections.mode_overrides()), 1)
        self.assertEqual(day_summary(self.db, "2026-09-27", "UTC")["activity_totals"]["transit"]["count"], 1)
        save_mode_override(self.db, {"source": trip["source"], "mode": "auto", "started_at": trip["start"], "ended_at": trip["end"]})
        self.assertEqual((self.db.corrections.mode_overrides(), day_summary(self.db, "2026-09-27", "UTC")["trips"][0]["mode"]), ([], "walking"))
        with self.assertRaisesRegex(ValueError, "mode must be one of"):
            save_mode_override(self.db, {"source": "phone", "mode": "skateboard", "started_at": trip["start"], "ended_at": trip["end"]})
        with self.assertRaisesRegex(ValueError, "target must be"):
            save_mode_override(self.db, {"source": "phone", "target": "place", "mode": "driving", "started_at": trip["start"], "ended_at": trip["end"]})

    def test_corrected_ride_between_drives_is_not_turned_back_into_driving(self):
        lot_a, lot_b = shifted(CAMPUS, -200, -300), shifted(CAMPUS, 900, 500)
        middle = [line(CAMPUS, lot_a, 1), still(lot_a, 90), line(lot_a, lot_b, 4), still(lot_b, 80)]
        fixes = minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), *middle, line(lot_b, HOME, 12), still(HOME, 30)])
        ingest(self.db, fixes)
        with patch("geologs.services.places.place_for_coordinate", return_value={"name": "Street"}):
            move = [trip for trip in anchored_trips_for_day(self.db) if 1 < trip["distance_km"] < 2][0]
            save_mode_override(self.db, {"source": move["source"], "mode": "cycling", "started_at": move["start"], "ended_at": move["end"]})
            day = day_summary(self.db, "2026-09-27", "UTC")
        modes = [trip["mode"] for trip in day["trips"]] + [item["mode"] for stay in day["stays"] for item in stay["internal_moves"]]
        self.assertIn("cycling", modes)

    def test_area_stay_activity_can_be_corrected(self):
        ingest(self.db, lake_run_day())
        lake = next(row for row in stays(self.db, limit=None) if row["kind"] == "area")
        save_mode_override(self.db, {"source": lake["source"], "target": "stay", "mode": "cycling",
                                       "started_at": lake["started_at"], "ended_at": lake["ended_at"]})
        corrected = next(row for row in stays(self.db, limit=None) if row["kind"] == "area")
        self.assertEqual((corrected["activity"], corrected["detected_activity"], corrected["activity_corrected"]), ("cycling", "running", True))
        totals = day_summary(self.db, "2026-09-27", "UTC")["activity_totals"]
        self.assertEqual((totals["cycling"]["count"], totals["running"]["count"]), (1, 0))

    def test_parked_out_and_back_run_is_one_visit(self):
        trail = shifted(HOME, 15000, 0)
        turn = shifted(trail, 0, 3000)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, trail, 15), line(trail, turn, 20),
                                        line(turn, trail, 20), line(trail, HOME, 15), still(HOME, 30)]))
        area = [row for row in stays(self.db, limit=None) if row["kind"] == "area"]
        self.assertEqual(len(area), 1)
        self.assertEqual(area[0]["activity"], "running")
        self.assertGreaterEqual(area[0]["observed_minutes"], 38)

    def test_motion_classification_uses_distance_weighted_speeds(self):
        minute = 60
        self.assertEqual(classify_motion([(80, minute, None)] * 10)["mode"], "walking")
        self.assertEqual(classify_motion([(150, minute, None)] * 10)["mode"], "running")
        self.assertEqual(classify_motion([(330, minute, None)] * 10)["mode"], "cycling")
        self.assertEqual(classify_motion([(800, minute, None)] * 10)["mode"], "driving")
        # One implausible jump inside a walk does not make it a drive.
        self.assertEqual(classify_motion([(80, minute, None)] * 10 + [(300, 20, None)])["mode"], "walking")
        self.assertIsNone(classify_motion([(3, minute, None)]))

    def test_activity_speeds_from_settings(self):
        minute = 60
        brisk = {**self.db.settings.get(), "walking_max_kmh": 10}
        self.assertEqual(classify_motion([(150, minute, None)] * 10, brisk)["mode"], "walking")
        self.assertEqual(mode_for(2.5, None, brisk), "walking")
        # A slow drive through traffic still counts once stretches reach the driving speed.
        slow_traffic = [(300, minute, None)] * 8 + [(700, minute, None)] * 2
        self.assertEqual(classify_motion(slow_traffic)["mode"], "driving")
        self.assertEqual(classify_motion(slow_traffic, {**brisk, "driving_min_kmh": 50})["mode"], "cycling")

    def test_changing_activity_speeds_recalculates_stored_stays(self):
        ingest(self.db, lake_run_day())
        area = lambda: [stay for stay in stays(self.db, limit=None) if stay["kind"] == "area"]
        self.assertEqual(area()[0]["activity"], "running")
        before = daily_overview(self.db, "2026-09-27", "UTC")
        save_settings(self.db, {"walking_max_kmh": 10})
        self.assertEqual(area()[0]["activity"], "walking")
        record = stored_daily_record(self.db, "2026-09-27")
        self.assertTrue(record["stale"])
        self.assertNotEqual(daily_overview(self.db, "2026-09-27", "UTC")["activity_totals"], before["activity_totals"])

    def test_minimum_stay_length_from_settings(self):
        from geologs.domain.geo import metres
        save_settings(self.db, {"quick_stop_minutes": 0})
        cafe = shifted(HOME, 3000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, cafe, 6), still(cafe, 9),
                                        line(cafe, HOME, 6), still(HOME, 30)]))
        near_cafe = lambda: [stay for stay in stays(self.db, limit=None) if metres(*cafe, stay["lat"], stay["lon"]) < 100]
        self.assertEqual(near_cafe(), [])
        save_settings(self.db, {"stay_min_minutes": 5})
        self.assertEqual(len(near_cafe()), 1)
        self.assertIn("at least 5 minutes", search_visits(self.db, "anything", timezone_name="UTC")["note"])

    def test_analysis_settings_are_validated(self):
        for bad in ({"stay_min_minutes": 2}, {"stay_min_minutes": 7.5}, {"walking_max_kmh": 20},
                    {"cycling_max_kmh": 40}, {"driving_min_kmh": 0}, {"summary_schedule": "weekly"},
                    {"summary_interval_hours": 24}):
            with self.assertRaises(ValueError, msg=bad):
                save_settings(self.db, bad)
        saved = save_settings(self.db, {"summary_schedule": "interval", "summary_interval_hours": 2})
        self.assertEqual((saved["summary_schedule"], saved["summary_interval_hours"], saved["stay_min_minutes"]),
                         ("interval", 2, 15))

    def test_summary_schedule_saves_today_only_on_an_interval(self):
        start = datetime(2026, 9, 27, 13, tzinfo=timezone.utc)
        ingest(self.db, minute_fixes([still(HOME, 60)], start=start))
        saved_today = lambda: any(item["date"] == "2026-09-27" for item in daily_overviews(self.db, ))
        save_scheduled_summaries(self.db, now=start + timedelta(hours=2))
        self.assertFalse(saved_today())
        save_settings(self.db, {"summary_schedule": "interval", "summary_interval_hours": 3})
        save_scheduled_summaries(self.db, now=start + timedelta(hours=2))
        self.assertTrue(saved_today())
        # Not due again until the interval has passed.
        with patch("geologs.services.overview.daily_overview") as rebuild:
            save_scheduled_summaries(self.db, now=start + timedelta(hours=3))
        self.assertNotIn("2026-09-27", [call.args[1] for call in rebuild.call_args_list])

    def test_deleting_samples_previews_then_removes_and_recalculates(self):
        from geologs.services.samples import delete_samples
        cafe = shifted(HOME, 5000, 0)
        place = save_place(self.db, {"name": "Cafe", "lat": cafe[0], "lon": cafe[1], "radius_m": 120})
        create_reminder(self.db, {"text": "Buy beans", "place_id": place["id"]})
        fixes = minute_fixes([still(HOME, 30), line(HOME, cafe, 8), still(cafe, 25), line(cafe, HOME, 8), still(HOME, 30)])
        with patch("geologs.services.ingest.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 27, 13, 40, tzinfo=timezone.utc)
            clock.fromisoformat = datetime.fromisoformat
            clock.fromtimestamp = datetime.fromtimestamp
            ingest(self.db, fixes)
        daily_overview(self.db, "2026-09-27", "UTC")
        at_cafe = [row for row in self.db.points.observations(limit=None)
                   if metres_between(cafe, row) < 60]
        self.assertGreaterEqual(len(at_cafe), 20)
        ids = [row["id"] for row in at_cafe]
        preview = delete_samples(self.db, ids)
        self.assertFalse(preview["deleted"])
        self.assertEqual((preview["samples"], preview["sources"], preview["dates"]), (len(ids), ["phone"], ["2026-09-27"]))
        self.assertEqual([item["reminder"] for item in preview["reminder_triggers"]], ["Buy beans"])
        self.assertEqual(self.db.points.count(), len(fixes))  # a preview changes nothing
        result = delete_samples(self.db, ids, confirm=True)
        self.assertTrue(result["deleted"])
        self.assertEqual(self.db.points.count(), len(fixes) - len(ids))
        self.assertEqual(self.db.rows("SELECT * FROM events"), [])
        # The cafe visit is gone from stays and from the stored summary.
        self.assertFalse([stay for stay in stays(self.db, limit=None) if metres_between(cafe, stay) < 100])
        record = stored_daily_record(self.db, "2026-09-27")
        self.assertFalse(record["stale"])
        self.assertEqual(record["summary"]["totals"]["raw_fixes"], len(fixes) - len(ids))
        with self.assertRaises(ValueError):
            delete_samples(self.db, ids, confirm=True)

    def test_deleting_every_sample_of_a_day_drops_its_summary(self):
        from geologs.services.samples import delete_samples
        ingest(self.db, minute_fixes([still(HOME, 40)]))
        daily_overview(self.db, "2026-09-27", "UTC")
        ids = [row["id"] for row in self.db.points.observations(limit=None)]
        delete_samples(self.db, ids, confirm=True)
        self.assertEqual((daily_overviews(self.db, ), latest(self.db)), ([], None))
        # The source can send again afterwards.
        self.assertEqual(ingest(self.db, minute_fixes([still(HOME, 2)]))["inserted"], 2)
        for bad in ([], ["x"], list(range(1, 502))):
            with self.assertRaises(ValueError):
                delete_samples(self.db, bad)

    def test_quick_stop_needs_stillness_and_travel_on_both_sides(self):
        fuel = shifted(HOME, 4000, 0)
        work = shifted(HOME, 9000, 0)
        jitter = [shifted(fuel, 4 * math.sin(i), 4 * math.cos(i)) for i in range(5)]
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, fuel, 5), jitter, line(fuel, work, 6), still(work, 30)]))
        kinds = lambda: [stay["kind"] for stay in reversed(stays(self.db, limit=None))]
        self.assertEqual(kinds(), ["stationary", "stop", "stationary"])
        trips = day_summary(self.db, "2026-09-27", "UTC")["trips"]
        self.assertEqual([trip["mode"] for trip in reversed(trips)], ["driving", "driving"])

    def test_sparse_fuel_stop_from_owntracks_is_kept_only_near_a_place(self):
        # Real OwnTracks fixes from 2026-10-10 19:27-19:35 Chicago time, at full precision.
        # The phone went quiet at the Costco pumps; 00:28:11 is still rolling in at 12 km/h,
        # 76 m from where the car stood at 00:32:55, but only 37 m from the stop's centre.
        source = "owntracks:owntracks/AAECA792-3041-476E-A643-12321E9A9694"
        rows = [("00:26:59", 34.761179, -92.454262, 3), ("00:27:13", 34.759461, -92.453592, 2),
                ("00:28:11", 34.758368, -92.451837, 2), ("00:29:29", 34.757962, -92.451609, 16),
                ("00:32:55", 34.75773, -92.452137, 5), ("00:34:44", 34.758283, -92.449974, 3),
                ("00:34:58", 34.757837, -92.447753, 2), ("00:35:11", 34.757339, -92.445648, 2)]
        ingest(self.db, [{"source": source, "external_id": at, "timestamp": f"2026-10-11T{at}+00:00",
                            "lat": lat, "lon": lon, "accuracy_m": accuracy} for at, lat, lon, accuracy in rows])
        costco = {"place_name": "Costco Gas Station", "place_lat": 34.757878, "place_lon": -92.451721}
        with patch("geologs.services.places.named_place_near", return_value=costco) as lookup:
            kept = stays(self.db, limit=None)
        self.assertEqual([(row["kind"], row["started_at"], row["ended_at"]) for row in kept],
                         [("stop", "2026-10-11T00:28:11+00:00", "2026-10-11T00:32:55+00:00")])
        self.assertFalse(lookup.call_args.args[4])  # a history sweep reads only the lookup cache
        # With no place there it was probably the road or a red light, so the day offers it to keep.
        with patch("geologs.services.places.named_place_near", return_value=None):
            self.assertEqual(stays(self.db, limit=None), [])
            day = day_summary(self.db, "2026-10-11", "UTC")
        self.assertEqual([(stop["started_at"], stop["minutes"]) for stop in day["possible_stops"]],
                         [("2026-10-11T00:28:11+00:00", 4.7)])
        kept = save_stay_edit(self.db, {"action": "add", **{key: day["possible_stops"][0][key] for key in ("source", "started_at", "ended_at")}})
        with patch("geologs.services.places.named_place_near", return_value=None):
            day = day_summary(self.db, "2026-10-11", "UTC")
        self.assertEqual(([stay["kind"] for stay in day["stays"]], day["possible_stops"]), (["manual"], []))
        delete_stay_edit(self.db, kept["id"])
        # A saved place there is evidence too, with no lookup at all.
        save_place(self.db, {"name": "Fuel", "lat": 34.757878, "lon": -92.451721, "radius_m": 80})
        with patch("geologs.services.places.named_place_near", side_effect=AssertionError("looked up")):
            self.assertEqual(len(stays(self.db, limit=None)), 1)

    def test_a_cached_place_with_only_its_distance_still_confirms_a_stop(self):
        from geologs.services.places import named_place_near
        # The Mapbox result cached on the server: no position of its own, 11 m from the lookup point.
        cached = {"place_name": "Costco Gas Station", "lat": 34.75796, "lon": -92.4516, "distance_m": 11,
                  "category": "gas_station"}
        with patch("geologs.services.places.reverse_geocode", return_value={**cached, "lat": 34.75802, "lon": -92.45186}):
            self.assertEqual(named_place_near(self.db, 34.75802, -92.45186, 75)["place_name"], "Costco Gas Station")
        with patch("geologs.services.places.reverse_geocode", return_value={**cached, "distance_m": 140}):
            self.assertIsNone(named_place_near(self.db, 34.75802, -92.45186, 75))

    def test_leaving_after_a_quiet_stretch_ends_the_stay_near_the_departure(self):
        # Home at 13:00, the phone quiet until it reports 2.7 km away at 15:00.
        away = shifted(HOME, 2000, 0)  # line() starts one 700 m step past this
        ingest(self.db, minute_fixes([still(HOME, 3)]) + minute_fixes(
            [line(away, shifted(HOME, 9000, 0), 10), still(shifted(HOME, 9000, 0), 30)],
            start=datetime(2026, 9, 27, 15, tzinfo=timezone.utc)))
        home = list(reversed(stays(self.db, limit=None)))[0]
        self.assertEqual(home["started_at"], "2026-09-27T13:00:00+00:00")
        # 2.7 km at 30 km/h is 5 min 24 s before the fix.
        self.assertEqual(home["ended_at"], "2026-09-27T14:54:36+00:00")
        day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual(day["coverage_gaps"], [])

    def test_every_move_between_places_is_a_trip_even_unrecorded(self):
        # Real fixes from 2026-10-13 (Chicago + 5 h): two buildings 225 m apart, one fix in each, and
        # nothing in between: the walk is still a trip, said to be unrecorded and without a mode.
        source = "owntracks:phone"
        rows = [("18:59:06", 34.72534, -92.34065), ("20:06:43", 34.72341, -92.33989), ("20:40:00", 34.72341, -92.33989)]
        ingest(self.db, [{"source": source, "external_id": at, "timestamp": f"2026-10-13T{at}+00:00",
                          "lat": lat, "lon": lon, "accuracy_m": 14} for at, lat, lon in rows])
        ingest(self.db, [{"source": source, "external_id": "before", "timestamp": "2026-10-13T18:28:47+00:00",
                          "lat": 34.74628, "lon": -92.37063, "accuracy_m": 2}])
        day = day_summary(self.db, "2026-10-13", "UTC")
        (walk,) = [trip for trip in day["trips"] if trip["start"] > "2026-10-13T20:00"]
        self.assertEqual((walk["recorded"], walk["mode"], walk["coverage_complete"]), (False, None, False))
        self.assertAlmostEqual(walk["distance_km"], 0.22, delta=0.02)
        self.assertEqual([leg["kind"] for leg in walk["route"]], ["gap"])
        # Not counted as any activity, and the brief says only that you moved.
        self.assertEqual(sum(totals["count"] for totals in day["activity_totals"].values()), 0)
        self.assertIn("moved 0.2 km", " ".join(daily_brief(daily_overview(self.db, "2026-10-13", "UTC"))["timeline"]))
        # Nothing it recorded may hide a coverage gap.
        self.assertEqual(day["coverage_gaps"], [])

    def test_a_trip_way_runs_in_order_and_fills_take_their_gaps_place(self):
        from geologs.domain.paths import with_fills
        from geologs.domain.routes import route_legs
        origin, destination = {"lat": 41.0, "lon": -87.9}, {"lat": 41.0, "lon": -87.8}
        pieces = [[[-87.89, 41.0], [-87.88, 41.0]], [[-87.85, 41.0]], [[-87.83, 41.0], [-87.82, 41.0]]]
        legs = route_legs(pieces, origin, destination)
        self.assertEqual([leg["kind"] for leg in legs], ["gap", "recorded", "gap", "gap", "recorded", "gap"])
        trip = {"route": legs, "distance_km": 1.0, "mode": "driving", "modes": {"driving": 3}}
        fill = {"coordinates": [[-87.88, 41.0], [-87.86, 41.01], [-87.85, 41.0]], "past_trips": 2, "mode": "driving", "distance_m": 2500}
        filled = with_fills(trip, {2: fill})
        self.assertEqual([leg["kind"] for leg in filled["route"]], ["gap", "recorded", "fill", "gap", "recorded", "gap"])
        self.assertEqual(trip["route"][2]["kind"], "gap")  # the trip it was given is left as it was
        self.assertEqual(filled["distance_km"], 3.5)

    def test_one_fix_between_quiet_stretches_is_a_stay(self):
        # Real OwnTracks fixes from 2026-10-13 (Chicago time + 5 h): home, a drive, an hour in
        # one building (one 14 m fix), then a walk to another building 225 m away. The 1 km fixes
        # neither confirm nor end a stay; 13:28:47 is still driving, reached at 4.6 m/s.
        source = "owntracks:owntracks/AAECA792-3041-476E-A643-12321E9A9694"
        rows = [("16:40:00", 34.74314, -92.37475, 5), ("17:11:26", 34.74314, -92.37475, 5),
                ("18:27:18", 34.74288, -92.37241, 38), ("18:27:20", 34.74288, -92.37243, 27),
                ("18:28:47", 34.74628, -92.37063, 2), ("18:34:14", 34.72709, -92.34091, 1000),
                ("18:59:06", 34.72534, -92.34065, 14), ("19:57:26", 34.72534, -92.34065, 1000),
                ("20:06:43", 34.72341, -92.33989, 14)]
        ingest(self.db, [{"source": source, "external_id": at, "timestamp": f"2026-10-13T{at}+00:00",
                            "lat": lat, "lon": lon, "accuracy_m": accuracy} for at, lat, lon, accuracy in rows])
        kept = list(reversed(stays(self.db, limit=None)))
        # Home until about the time it takes to reach 18:27:18; the building from about the time
        # it takes to get there from 18:28:47 until about the time it takes to walk to 20:06:43.
        self.assertEqual([(row["started_at"], row["ended_at"], row["point_count"]) for row in kept],
                         [("2026-10-13T16:40:00+00:00", "2026-10-13T18:26:52+00:00", 2),
                          ("2026-10-13T18:35:58+00:00", "2026-10-13T20:06:15+00:00", 1)])
        day = day_summary(self.db, "2026-10-13", "UTC")
        self.assertEqual([(trip["start"], trip["end"]) for trip in day["trips"]],
                         [("2026-10-13T18:26:52+00:00", "2026-10-13T18:35:58+00:00")])
        # The drive is recorded only near home; dashed legs join it to both stays.
        trip, home, building = day["trips"][0], kept[0], kept[1]
        self.assertEqual(len(trip["route_segments"]), 1)
        self.assertEqual(trip["route_gaps"], [[[home["lon"], home["lat"]], trip["route_segments"][0][0]],
                                              [trip["route_segments"][0][-1], [building["lon"], building["lat"]]]])
        self.assertEqual(day["coverage_gaps"], [])

    def test_sparse_trip_follows_the_route_a_dense_trip_taught(self):
        from geologs.services.paths import learn_path_history
        # Day one drives Home -> Work round a corner, a fix a minute; day two's phone
        # rolls away from home at bike speed, then says nothing until Work.
        corner, work = shifted(HOME, 6000, 0), shifted(HOME, 6000, 6000)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, corner, 10), line(corner, work, 10), still(work, 30)]))
        day_two = datetime(2026, 9, 28, 13, tzinfo=timezone.utc)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, shifted(HOME, 600, 0), 2)], start=day_two)
                          + minute_fixes([still(work, 30)], start=day_two + timedelta(minutes=52)))
        sparse = lambda: day_summary(self.db, "2026-09-28", "UTC")["trips"][0]
        # Nothing learned yet: the unrecorded leg is a straight line.
        trip = sparse()
        self.assertEqual((len(trip["route_gaps"]), trip["route_fills"], trip["mode"]), (1, [], "cycling"))
        self.assertEqual(learn_path_history(self.db)["learned"], 1)
        learned = self.db.paths.signature()
        self.assertEqual(learn_path_history(self.db)["paths"], 1)
        self.assertEqual(self.db.paths.signature(), learned)  # learning again changes nothing
        trip = sparse()
        self.assertEqual(trip["route_gaps"], [])
        (fill,) = trip["route_fills"]
        # Round the corner, not across the diagonal, and as the drive it was.
        self.assertLess(min(metres_between(corner, {"lat": lat, "lon": lon}) for lon, lat in fill["coordinates"]), 20)
        self.assertEqual((fill["past_trips"], trip["mode"], trip["detected_mode"], trip["mode_from_paths"]),
                         (1, "driving", "cycling", True))  # the trip itself joined the route: not a past trip
        self.assertAlmostEqual(trip["distance_km"], 12.0, delta=.1)
        # The filled trip never teaches a path itself.
        self.assertEqual(self.db.paths.signature(), learned)

    def drive_to_work(self, source="phone", start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)):
        """A dense drive Home -> Work round a corner: enough to teach a path."""
        corner, work = shifted(HOME, 6000, 0), shifted(HOME, 6000, 6000)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, corner, 10), line(corner, work, 10), still(work, 30)],
                                     source=source, start=start))
        return work

    def test_deleting_a_tracker_forgets_its_routes_and_place_lookups(self):
        from geologs.services.paths import fill_gap, learn_path_history
        from geologs.services.samples import delete_source
        work = self.drive_to_work(source="old-phone")
        self.assertEqual(learn_path_history(self.db)["paths"], 1)
        self.db.caches.save_reverse_region("provider", HOME[0], HOME[1], 15, {"name": "Home street"})
        delete_source(self.db, "old-phone", confirm=True)
        self.assertEqual(self.db.paths.summary_rows(), [])
        # Its route no longer fills anyone's trips, and where it stopped is no longer on record.
        self.assertIsNone(fill_gap(self.db, [HOME[1], HOME[0]], [work[1], work[0]]))
        self.assertIsNone(self.db.caches.reverse_region("provider", HOME[0], HOME[1]))

    def test_deleting_samples_forgets_routes_and_lookups_made_from_them(self):
        from geologs.services.paths import learn_path_history
        from geologs.services.samples import delete_samples
        self.drive_to_work()
        self.assertEqual(learn_path_history(self.db)["paths"], 1)
        elsewhere = shifted(HOME, 0, -20000)
        self.db.caches.save_reverse_region("provider", HOME[0], HOME[1], 15, {"name": "Home street"})
        self.db.caches.save_reverse_region("provider", elsewhere[0], elsewhere[1], 15, {"name": "Far away"})
        self.db.commit()
        # The whole drive, and one fix from home: the route it taught goes, and is not learned
        # again from what is left (deleting one fix of a dense drive would teach it again, without it).
        drive = self.db.points.observations("2026-09-27T13:30:00+00:00", "2026-09-27T13:49:00+00:00", limit=None)
        home = self.db.points.observations("2026-09-27T13:10:00+00:00", "2026-09-27T13:10:00+00:00")
        delete_samples(self.db, [row["id"] for row in drive] + [home[0]["id"]], confirm=True)
        self.assertEqual(self.db.paths.summary_rows(), [])
        self.assertIsNone(self.db.caches.reverse_region("provider", HOME[0], HOME[1]))
        self.assertEqual(self.db.caches.reverse_region("provider", elsewhere[0], elsewhere[1]), {"name": "Far away"})

    def test_reading_a_day_never_adds_to_the_path_library(self):
        from geologs.services.paths import learn_recent_paths
        self.drive_to_work()
        day_summary(self.db, "2026-09-27", "UTC")
        self.assertEqual(self.db.paths.summary_rows(), [])
        # The background worker learns it, once per new batch of samples.
        now = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
        self.assertEqual(learn_recent_paths(self.db, now=now)["learned"], 1)
        self.assertIsNone(learn_recent_paths(self.db, now=now))
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-27T20:00:00Z", "lat": 41, "lon": -87.9})
        self.assertIsNotNone(learn_recent_paths(self.db, now=now))

    def test_place_details_are_filled_in_the_background_not_when_read(self):
        from geologs.services.saved_places import enrich_saved_places
        place_id = self.db.places.add("Market", 41.01, -87.9, 150, "", "", "")  # saved before lookups kept details
        self.db.commit()
        found = {"name": "Market", "named": True, "address": "1 Main St", "category": "commercial.supermarket",
                 "categories": [], "provider_place_id": "market-1"}
        with patch("geologs.services.places.place_for_coordinate", side_effect=AssertionError("looked up")):
            self.assertEqual(self.db.places.saved_place(place_id)["address"], "")
            visited_places(self.db)
        with patch("geologs.services.places.place_for_coordinate", return_value=found):
            enrich_saved_places(self.db)
        self.assertEqual(self.db.places.saved_place(place_id)["address"], "1 Main St")

    def test_two_requests_learning_one_day_at_once_both_succeed(self):
        import threading
        from geologs.data.paths import PathsRepository
        from geologs.services.paths import learn_paths
        self.drive_to_work()
        start, end = "2026-09-27T00:00:00+00:00", "2026-09-27T23:59:59.999999+00:00"
        trips = anchored_trips(self.db, start, end)
        # The browser and the summary thread both read the library before either writes.
        barrier, read = threading.Barrier(2, timeout=5), PathsRepository.starting_between
        def read_then_wait(repository, *args):
            rows = read(repository, *args)
            barrier.wait()
            return rows
        errors = []
        def view():
            db = open_database(self.url)
            try:
                learn_paths(db, trips, start, end)
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        with patch.object(PathsRepository, "starting_between", read_then_wait):
            threads = [threading.Thread(target=view) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db.paths.summary_rows()), 1)

    def test_gap_takes_the_most_travelled_route_either_way_round(self):
        from geologs.services.paths import learn_path_history
        from geologs.services.paths import fill_gap
        east, north, work = shifted(HOME, 6000, 0), shifted(HOME, 0, 6000), shifted(HOME, 6000, 6000)
        drive = lambda via: [still(HOME, 30), line(HOME, via, 10), line(via, work, 10), still(work, 30)]
        for day, via in enumerate((east, north, east)):
            ingest(self.db, minute_fixes(drive(via), start=datetime(2026, 9, 27 + day, 13, tzinfo=timezone.utc)))
        self.assertEqual(learn_path_history(self.db)["by_mode"], {"driving": 3})
        # Home -> Work learned; Work -> Home asked.
        fill = fill_gap(self.db, [work[1], work[0]], [HOME[1], HOME[0]])
        self.assertEqual(fill["past_trips"], 2)
        self.assertLess(min(metres_between(east, {"lat": lat, "lon": lon}) for lon, lat in fill["coordinates"]), 20)
        # Nowhere near a known path: no route.
        self.assertIsNone(fill_gap(self.db, [LAKE[1], LAKE[0]], [work[1], work[0]]))

    def test_coverage_gap_is_only_the_time_no_stay_covers(self):
        # The same morning with nothing yet from the building: Home (quiet since its last fix,
        # with a Home Assistant fix at 17:22) ran until 26 s before the first fix away.
        owntracks = "owntracks:owntracks/AAECA792-3041-476E-A643-12321E9A9694"
        rows = [(owntracks, "16:40:00", 34.74314, -92.37475, 5), (owntracks, "17:11:26", 34.74314, -92.37475, 5),
                ("home_assistant:device_tracker.phone", "17:22:57", 34.74312, -92.37473, 9),
                (owntracks, "18:27:18", 34.74288, -92.37241, 38), (owntracks, "18:27:20", 34.74288, -92.37243, 27),
                (owntracks, "18:28:47", 34.74628, -92.37063, 2), (owntracks, "20:00:00", 34.72534, -92.34065, 14)]
        ingest(self.db, [{"source": source, "external_id": at, "timestamp": f"2026-10-13T{at}+00:00",
                            "lat": lat, "lon": lon, "accuracy_m": accuracy} for source, at, lat, lon, accuracy in rows])
        day = day_summary(self.db, "2026-10-13", "UTC")
        self.assertEqual([(stay["started_at"], stay["ended_at"]) for stay in day["stays"]],
                         [("2026-10-13T16:40:00+00:00", "2026-10-13T18:26:52+00:00")])
        # Not 17:22-18:27, which Home covers; the 91 min to 20:00 are unknown.
        self.assertEqual(day["coverage_gaps"], [{"start": "2026-10-13T18:28:47+00:00",
                                                 "end": "2026-10-13T20:00:00+00:00", "minutes": 91.2}])

    def test_rolling_in_fix_does_not_split_the_stay_it_starts(self):
        # Real Home Assistant (OwnTracks) fixes arriving home on 2026-10-07: the first is still
        # moving at 12 km/h, 53.5 m from where the phone settled at 12 m accuracy.
        source = "home_assistant:device_tracker.owntracks_phone"
        rows = [("03:36:01", 34.740353, -92.371355, 2), ("03:37:01", 34.742055, -92.372287, 2),
                ("03:38:01", 34.742961, -92.374196, 2), ("03:41:01", 34.742993, -92.374631, 10),
                ("03:44:01", 34.742994, -92.374673, 12), ("03:47:02", 34.743128, -92.374745, 12),
                ("03:50:02", 34.743126, -92.374763, 12), ("03:59:02", 34.743127, -92.374760, 12)]
        ingest(self.db, [{"source": source, "external_id": at, "timestamp": f"2026-10-08T{at}+00:00",
                            "lat": lat, "lon": lon, "accuracy_m": accuracy} for at, lat, lon, accuracy in rows])
        self.assertEqual([(stay["started_at"], stay["ended_at"]) for stay in stays(self.db, limit=None)],
                         [("2026-10-08T03:38:01+00:00", "2026-10-08T03:59:02+00:00")])

    def test_switching_trackers_mid_visit_is_one_visit(self):
        # The Home Assistant app stops at 15:00 and OwnTracks takes over two minutes later, still at home.
        app = minute_fixes([still(CAMPUS, 30), line(CAMPUS, HOME, 10), still(HOME, 60)], source="ha:app")
        owntracks = minute_fixes([still(HOME, 40), line(HOME, CAMPUS, 10), still(CAMPUS, 30)], source="ha:owntracks",
                                 start=datetime(2026, 9, 27, 14, 42, tzinfo=timezone.utc))
        ingest(self.db, app + owntracks)
        with patch("geologs.services.places.place_for_coordinate", side_effect=campus_label):
            day = day_summary(self.db, "2026-09-27", "UTC")
        home = [stay for stay in day["stays"] if metres_between(HOME, stay) < 100]
        self.assertEqual([(stay["started_at"], stay["ended_at"], stay["sources"]) for stay in home],
                         [("2026-09-27T13:39:00+00:00", "2026-09-27T15:21:00+00:00", ["ha:app", "ha:owntracks"])])
        self.assertEqual(len(day["trips"]), 2)

    def test_creeping_traffic_and_short_waits_are_not_quick_stops(self):
        queue = shifted(HOME, 4000, 0)
        crawl = line(queue, shifted(queue, 0, 60), 6)  # 60 m in six minutes: stop-and-go traffic
        light = still(shifted(HOME, 6000, 0), 2)  # a red light
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, queue, 5), crawl,
                                        line(crawl[-1], light[0], 3), light, line(light[0], shifted(HOME, 9000, 0), 4),
                                        still(shifted(HOME, 9000, 0), 30)]))
        self.assertEqual([stay["kind"] for stay in stays(self.db, limit=None)], ["stationary", "stationary"])

    def test_still_fixes_after_a_coverage_gap_are_not_a_quick_stop(self):
        fuel = shifted(HOME, 4000, 0)
        before = minute_fixes([still(HOME, 30), line(HOME, fuel, 5)])
        # Nothing for 20 minutes, then five still minutes and on to work.
        after = minute_fixes([still(fuel, 5), line(fuel, shifted(HOME, 9000, 0), 6), still(shifted(HOME, 9000, 0), 30)],
                             start=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc))
        ingest(self.db, before + after)
        self.assertNotIn("stop", [stay["kind"] for stay in stays(self.db, limit=None)])

    def test_a_pause_on_a_lake_loop_is_not_a_quick_stop(self):
        shore = shifted(LAKE, -500, 0)
        # 30 minutes at 9 km/h covers 4.5 km, 9 radians of a 500 m circle.
        first, second = laps(LAKE, 500, 30, 9), laps(LAKE, 500, 30, 9, start_angle=math.pi + 9)
        pause = [shifted(first[-1], 3 * math.sin(i), 3 * math.cos(i)) for i in range(5)]
        ingest(self.db, minute_fixes([still(HOME, 31), line(HOME, shore, 12), first, pause, second,
                                        line(second[-1], HOME, 12), still(HOME, 30)]))
        self.assertEqual([stay["kind"] for stay in reversed(stays(self.db, limit=None))],
                         ["stationary", "area", "stationary"])

    def test_adding_and_removing_stops_by_hand(self):
        fuel, work = shifted(HOME, 4000, 0), shifted(HOME, 9000, 0)
        save_settings(self.db, {"quick_stop_minutes": 0})
        # Two minutes at the pump: too short for any detector.
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, fuel, 5), still(fuel, 2), line(fuel, work, 6), still(work, 30)]))
        trips = lambda: day_summary(self.db, "2026-09-27", "UTC")["trips"]
        self.assertEqual(len(trips()), 1)
        overview = daily_overview(self.db, "2026-09-27", "UTC")
        added = save_stay_edit(self.db, {"action": "add", "source": "phone",
                                           "started_at": "2026-09-27T13:35:00+00:00", "ended_at": "2026-09-27T13:36:00+00:00"})
        self.assertLess(metres_between(fuel, added), 5)
        rows = list(reversed(stays(self.db, limit=None)))
        self.assertEqual([(row["kind"], row["id"]) for row in rows][1], ("manual", -added["id"]))
        self.assertEqual(len(trips()), 2)
        self.assertTrue(stored_daily_record(self.db, "2026-09-27")["stale"])
        self.assertNotEqual(daily_overview(self.db, "2026-09-27", "UTC")["totals"], overview["totals"])
        # Removing the work stay; undoing both edits restores what was detected.
        work_stay = rows[2]
        removed = save_stay_edit(self.db, {"action": "remove", "source": "phone",
                                             "started_at": work_stay["started_at"], "ended_at": work_stay["ended_at"]})
        self.assertEqual([row["kind"] for row in reversed(stays(self.db, limit=None))], ["stationary", "manual"])
        self.assertEqual(len(self.db.corrections.stay_edits(source="phone")), 2)
        for edit in (added, removed):
            delete_stay_edit(self.db, edit["id"])
        self.assertEqual(len(stays(self.db, limit=None)), 2)
        with self.assertRaises(ValueError):
            delete_stay_edit(self.db, added["id"])
        with self.assertRaises(ValueError):  # no samples in that window
            save_stay_edit(self.db, {"action": "add", "source": "phone",
                                       "started_at": "2026-09-28T13:00:00+00:00", "ended_at": "2026-09-28T13:05:00+00:00"})
        with self.assertRaises(ValueError):
            save_settings(self.db, {"quick_stop_minutes": 15})

    def fuel_day(self):
        """Home, a 10 km drive (50 km/h), half an hour there, and home again: 20 km driven."""
        away = shifted(HOME, 10000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, away, 12), still(away, 30), line(away, HOME, 12), still(HOME, 30)]))
        return away

    def test_fuel_cost_of_drives_with_your_own_price(self):
        from geologs.services.fuel import set_manual_price
        from geologs.services.fuel import fuel_cost_windows, fuel_costs
        self.fuel_day()
        self.assertIsNone(day_summary(self.db, "2026-09-27", "UTC")["fuel"])  # no economy yet
        save_settings(self.db, {"fuel_economy_l100km": 8, "fuel_price_source": "manual", "currency": "$"})
        set_manual_price(self.db, 1.10, "2026-09-01")
        day = day_summary(self.db, "2026-09-27", "UTC")
        drives = [trip for trip in day["trips"] if trip["mode"] == "driving"]
        self.assertEqual(len(drives), 2)
        litres = sum(trip["distance_km"] for trip in drives) * 8 / 100
        self.assertAlmostEqual(day["fuel"]["litres"], litres, delta=0.02)
        self.assertAlmostEqual(day["fuel"]["cost"], litres * 1.10, delta=0.02)
        # A later price applies from its date on; this day keeps the price it had.
        set_manual_price(self.db, 2.00, "2026-10-01")
        self.assertAlmostEqual(day_summary(self.db, "2026-09-27", "UTC")["fuel"]["cost"], litres * 1.10, delta=0.02)
        overview = daily_overview(self.db, "2026-09-27", "UTC")
        self.assertAlmostEqual(overview["totals"]["fuel_cost"], litres * 1.10, delta=0.02)
        self.assertIn("Fuel about $", daily_brief(overview)["summary"])
        costs = fuel_costs(self.db, 7, now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))
        self.assertEqual((costs["drives"], costs["covered_days"]), (2, 2))
        both = fuel_cost_windows(self.db, (7, 30), now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))
        self.assertEqual(both[7], costs)
        self.assertEqual((both[30]["drives"], both[30]["days"]), (2, 30))
        self.assertAlmostEqual(costs["cost_per_day"], costs["cost"] / 2, delta=0.01)
        for bad in ({"fuel_economy_l100km": 0.5}, {"fuel_grade": "jet"}, {"units": "nautical"}, {"currency": ""},
                    {"fuel_area": "MARS"}):
            with self.assertRaises(ValueError, msg=bad):
                save_settings(self.db, bad)

    def test_a_drive_you_were_driven_on_costs_you_no_fuel(self):
        from geologs.services.fuel import set_manual_price
        self.fuel_day()
        save_settings(self.db, {"fuel_economy_l100km": 8, "fuel_price_source": "manual"})
        set_manual_price(self.db, 1.10, "2026-09-01")
        day = day_summary(self.db, "2026-09-27", "UTC")
        first, second = sorted((trip for trip in day["trips"] if trip["mode"] == "driving"), key=lambda trip: trip["start"])
        save_mode_override(self.db, {"source": first["source"], "target": "trip", "mode": "passenger",
                                     "started_at": first["start"], "ended_at": first["end"]})
        day = day_summary(self.db, "2026-09-27", "UTC")
        ride = next(trip for trip in day["trips"] if trip["start"] == first["start"])
        self.assertEqual((ride["mode"], ride["fuel"]), ("passenger", None))
        self.assertAlmostEqual(day["fuel"]["litres"], second["distance_km"] * 8 / 100, delta=0.02)
        # Still travel: counted as a passenger ride, and said so.
        self.assertEqual(day["activity_totals"]["passenger"]["count"], 1)
        self.assertIn("as a passenger", " ".join(daily_brief(daily_overview(self.db, "2026-09-27", "UTC"))["timeline"]))

    def test_a_trip_with_long_holes_teaches_no_route(self):
        from geologs.domain.paths import learnable
        dense = {"route_segments": [[[0, 0], [1, 1]]], "route_gaps": [], "mode": "driving", "distance_km": 5,
                 "recorded_points": 30, "max_fix_gap_m": 230}
        self.assertTrue(learnable(dense))
        # A tracker that holds a position for several polls, then jumps: dense-looking, but it guessed the way.
        self.assertFalse(learnable({**dense, "max_fix_gap_m": 3200}))

    def test_a_fix_a_minute_on_a_highway_is_watched_but_a_leap_after_a_held_position_is_not(self):
        from geologs.domain.routes import guessed_stretch_m
        at = lambda clock, lat, lon: {"timestamp": f"2026-10-08T{clock}+00:00", "lat": lat, "lon": lon}
        # Real Home Assistant polls of a 2026-10-08 drive home: 1.7 km in 59 s on the highway.
        drive = [at("23:58:01", 34.74551, -92.34135), at("23:59:02", 34.74567, -92.34595),
                 at("23:59:59", 34.74688, -92.36467), at("00:00:59", 34.74741, -92.37126)]
        self.assertEqual(guessed_stretch_m(drive), 0)
        # The same position held for three polls, then 1.7 km a minute later: nobody saw the way.
        held = [at("23:57:00", 34.74567, -92.34595), at("23:58:00", 34.74567, -92.34595),
                at("23:59:00", 34.74567, -92.34595), at("23:59:59", 34.74688, -92.36467)]
        self.assertGreater(guessed_stretch_m(held), 1700)

    def test_short_legs_to_the_stays_do_not_stop_a_trip_teaching_its_route(self):
        from geologs.domain.paths import learnable, learned_way
        # Real shape of a 2026-10-12 drive: the phone reported from 202 m out, and stopped 56 m short.
        way = [[-87.90, 41.0], [-87.85, 41.0], [-87.80, 41.0]]
        drive = {"route": [{"kind": "gap", "coordinates": [[-87.9024, 41.0], way[0]]},
                           {"kind": "recorded", "coordinates": way},
                           {"kind": "gap", "coordinates": [way[-1], [-87.7993, 41.0]]}],
                 "mode": "driving", "distance_km": 8.4, "recorded_points": 28, "max_fix_gap_m": 226}
        self.assertTrue(learnable(drive))
        self.assertEqual(learned_way(drive), [[-87.9024, 41.0], *way, [-87.7993, 41.0]])  # it reaches both stays
        long_end = {**drive, "route": [{"kind": "gap", "coordinates": [[-87.95, 41.0], way[0]]}, *drive["route"][1:]]}
        self.assertFalse(learnable(long_end))  # a 4 km leg nobody recorded is a guess, not a route

    def test_a_gap_to_another_building_follows_the_route_to_the_campus(self):
        from geologs.domain.paths import STAY_MATCH_M, best_route
        # A drive home -> library was learned; today's ends at a building 163 m off its end.
        known = [{"mode": "driving", "started_at": "2026-10-12T22:24:00+00:00",
                  "coordinates": [[-87.90, 41.0], [-87.85, 41.0], [-87.85, 41.03]]}]
        last_fix, building = [-87.899, 41.0], [-87.848, 41.0315]
        self.assertIsNone(best_route(last_fix, building, known))  # a fix must lie on the route...
        fill = best_route(last_fix, building, known, reach_b=STAY_MATCH_M)  # ...a stay may lie beside it
        self.assertEqual((fill["past_trips"], fill["coordinates"][-1]), (1, building))

    def test_a_long_wait_between_far_fixes_is_a_gap_not_a_recorded_way(self):
        from geologs.domain.routes import split_at_jumps
        at = lambda minute, lat, lon: {"timestamp": f"2026-10-14T03:{minute:02d}:00+00:00", "lat": lat, "lon": lon}
        # Real OwnTracks fixes from 2026-10-13: 3.6 km in five minutes, then 560 m home.
        sparse = [at(40, 34.72701, -92.341763), at(45, 34.747564, -92.371794), at(53, 34.743145, -92.374742)]
        self.assertEqual([len(run) for run in split_at_jumps(sparse)], [1, 2])
        # A fix a minute is a recorded way at any real speed...
        dense = [at(minute, 41.0 + minute * .01, -87.9) for minute in range(5)]
        self.assertEqual([len(run) for run in split_at_jumps(dense)], [5])
        # ...but not 3.5 km in a minute after five polls of one position (Home Assistant, 2026-10-06).
        held = [at(33, 34.72763, -92.34182), at(37, 34.72763, -92.34182), at(38, 34.74697, -92.37221), at(39, 34.74384, -92.37464)]
        self.assertEqual([len(run) for run in split_at_jumps(held)], [2, 2])

    def test_a_filled_jump_replaces_the_chord_in_a_trips_distance(self):
        from geologs.domain.paths import with_fills
        from geologs.domain.routes import route_legs
        origin, destination = {"lat": 41.0, "lon": -87.9}, {"lat": 41.0, "lon": -87.8}
        legs = route_legs([[[-87.9, 41.0], [-87.89, 41.0]], [[-87.81, 41.0], [-87.8, 41.0]]], origin, destination, {1})
        self.assertEqual([(leg["kind"], leg.get("jump")) for leg in legs],
                         [("recorded", None), ("gap", True), ("recorded", None)])
        trip = {"route": legs, "distance_km": 8.4, "mode": "driving", "modes": {"driving": 3}}
        fill = {"coordinates": [[-87.89, 41.0], [-87.85, 41.01], [-87.81, 41.0]], "past_trips": 1, "mode": "driving", "distance_m": 7000}
        filled = with_fills(trip, {1: fill})
        self.assertAlmostEqual(filled["recorded_km"], 1.69, delta=.01)  # 8.4 km less the 6.7 km chord
        self.assertAlmostEqual(filled["distance_km"], 8.69, delta=.01)

    def test_a_sparse_trip_takes_the_known_route_between_its_places_that_its_fixes_follow(self):
        from geologs.domain.paths import path_bounds
        from geologs.services.paths import fill_trip
        home, campus = [-87.90, 41.0], [-87.80, 41.0]
        south = [home, [-87.90, 40.99], [-87.80, 40.99], campus]
        north = [home, [-87.90, 41.01], [-87.80, 41.01], campus]
        for day, way in enumerate([south, south, north], start=1):
            self.db.paths.add("phone", f"2026-10-0{day}T08:00:00+00:00", f"2026-10-0{day}T08:20:00+00:00",
                              "driving", 13000, path_bounds(way), json.dumps(way))
        # Three fixes, all within 200 m of the north road; the south one is travelled more.
        fixes = [[-87.899, 41.0105], [-87.85, 41.0115], [-87.81, 41.0095]]
        legs = [{"kind": "gap", "coordinates": [home, fixes[0]]}, {"kind": "gap", "coordinates": [fixes[0], fixes[1]], "jump": True},
                {"kind": "recorded", "coordinates": fixes[1:]}, {"kind": "gap", "coordinates": [fixes[-1], campus]}]
        trip = {"route": legs, "fixes": fixes, "distance_km": 9.0, "mode": "driving", "modes": {"driving": 2}, "recorded": True,
                "from_lon": home[0], "from_lat": home[1], "to_lon": campus[0], "to_lat": campus[1]}
        filled = fill_trip(self.db, trip)
        self.assertEqual([leg["kind"] for leg in filled["route"]], ["fill"])
        self.assertAlmostEqual(max(lat for _lon, lat in filled["route"][0]["coordinates"]), 41.01)  # the north road
        self.assertEqual((filled["recorded_km"], filled["filled_km"], filled["distance_km"]), (0.0, 10.61, 10.61))
        self.assertNotIn("fixes", filled)
        # A fix far off every known route: each leg is filled on its own, where one fits.
        stray = fill_trip(self.db, {**trip, "fixes": [*fixes, [-87.85, 41.05]]})
        self.assertNotEqual([leg["kind"] for leg in stray["route"]], ["fill"])

    def test_a_lone_fix_may_lie_farther_off_a_known_route_than_one_among_close_neighbours(self):
        from geologs.domain.paths import place_route
        home, campus = [-87.90, 41.0], [-87.80, 41.0]
        known = [{"mode": "driving", "started_at": "2026-10-12T22:24:00+00:00",
                  "coordinates": [home, [-87.90, 41.01], [-87.80, 41.01], campus]}]
        # 190 m off the road, 4 km from the fixes either side: a sparse tracker's fix a street over.
        self.assertIsNotNone(place_route(home, campus, [[-87.90, 41.01], [-87.85, 41.0117], [-87.80, 41.01]], known))
        # The same fix with neighbours 400 m away went another way.
        self.assertIsNone(place_route(home, campus, [[-87.855, 41.01], [-87.85, 41.0117], [-87.845, 41.01]], known))

    def test_a_trip_takes_a_route_travelled_its_way_round_and_passes_its_fixes_in_order(self):
        from geologs.domain.paths import place_route
        home, campus = [-87.90, 41.0], [-87.80, 41.0]
        # Home -> campus along 41.0100; campus -> home along 41.0105 (a one-way pair, 55 m apart).
        there = [home, [-87.90, 41.0100], [-87.80, 41.0100], campus]
        back = [campus, [-87.80, 41.0105], [-87.90, 41.0105], home]
        known = [{"mode": "driving", "started_at": "2026-10-11T08:00:00+00:00", "coordinates": there},
                 {"mode": "driving", "started_at": "2026-10-12T17:00:00+00:00", "coordinates": back}]
        # Going home, fixes that sit right on the way there still take the way back.
        fixes = [[-87.82, 41.0100], [-87.85, 41.0100], [-87.88, 41.0100]]
        fill = place_route(campus, home, fixes, known)
        self.assertAlmostEqual(max(lat for _lon, lat in fill["coordinates"]), 41.0105)
        # Fixes that go back on themselves did not follow either road.
        self.assertIsNone(place_route(campus, home, [fixes[1], fixes[0], fixes[2]], known))

    @staticmethod
    def off_way(point, way):
        """Metres from a [lon, lat] point to a [lon, lat] polyline."""
        from geologs.domain.paths import project_onto, to_xy
        return project_onto([to_xy(lon, lat, point[1]) for lon, lat in way], to_xy(point[0], point[1], point[1]))[0]

    @staticmethod
    def dense_way(points):
        """A way recorded as a phone does on the move: a fix every 100 m or so between these corners."""
        return [[a[0] + (b[0] - a[0]) * step / 8, a[1] + (b[1] - a[1]) * step / 8]
                for a, b in zip(points, points[1:]) for step in range(8)] + [points[-1]]

    def campus_tracks(self):
        """Two drives home -> campus along a road that bends north: each recorded part of the way."""
        home, campus = [-87.90, 41.0], [-87.80, 41.0]
        road = [[-87.899, 41.0], [-87.88, 41.0], [-87.87, 41.005], [-87.86, 41.01], [-87.84, 41.01],
                [-87.83, 41.01], [-87.82, 41.005], [-87.81, 41.0], [-87.801, 41.0]]
        dense = self.dense_way
        track = lambda started_at, pieces: {
            "source": "phone", "started_at": started_at, "ended_at": started_at, "mode": "driving",
            "from": home, "to": campus, "from_name": "Home", "to_name": "Campus", "pieces": pieces,
            "distance_m": 9000.0, "observed_m": sum(path_length(piece) for piece in pieces)}
        from geologs.domain.paths import path_length
        # The first recorded the way to 41.01 and on, then lost signal; the second only the far end.
        first = track("2026-10-01T08:00:00+00:00", [dense(road[:6]), [road[7]]])
        second = track("2026-10-02T08:00:00+00:00", [[road[1]], dense(road[4:])])
        return home, campus, road, first, second

    def test_trips_that_each_recorded_part_of_a_way_piece_it_together(self):
        from geologs.domain.place_routes import group
        home, campus, road, first, second = self.campus_tracks()
        (route,) = group([first, second])
        self.assertEqual([member["started_at"] for member in route["members"]], [first["started_at"], second["started_at"]])
        # The first trip's gap is the second's recording: the whole road, bends and all.
        way = [point for part in route["parts"] for point in part["coordinates"]]
        self.assertLess(max(self.off_way(point, way) for point in road), 10)
        self.assertGreater(route["observed_m"], .95 * route["distance_m"])

    def test_a_fix_a_minute_far_apart_is_watched_but_not_drawn_as_the_road(self):
        from geologs.services.paths import learn_path_history
        # A fix a minute at 60 km/h: 1 km between fixes, watched all the way.
        far = shifted(HOME, 6000, 0)
        ingest(self.db, minute_fixes([still(HOME, 30), line(HOME, far, 6), still(far, 30)]))
        (trip,) = anchored_trips(self.db, "2026-09-27T00:00:00+00:00", "2026-09-27T23:59:59+00:00")
        gaps = [leg for leg in trip["route"] if leg["kind"] == "gap"]
        # No straight kilometre is drawn as recorded road; each is a sparse gap, not a jump.
        self.assertTrue(all(leg["kind"] != "recorded" or len(leg["coordinates"]) < 2 or max(
            metres_between((a[1], a[0]), {"lat": b[1], "lon": b[0]}) for a, b in zip(leg["coordinates"], leg["coordinates"][1:])) < 750
            for leg in trip["route"]))
        self.assertTrue(any(leg.get("sparse") for leg in gaps) and not any(leg.get("jump") for leg in gaps))
        self.assertTrue(trip["coverage_complete"])
        self.assertEqual(learn_path_history(self.db)["learned"], 1)  # watched, so it teaches its way

    def test_the_path_under_a_day_leaves_out_its_trips(self):
        from geologs.services.day import _between_trips
        at = lambda minute, source="phone": {"source": source, "timestamp": f"2026-10-13T10:{minute:02d}:00+00:00"}
        trip = ("phone", datetime(2026, 10, 13, 10, 10, tzinfo=timezone.utc), datetime(2026, 10, 13, 10, 20, tzinfo=timezone.utc))
        points = [at(5), at(10), at(15), at(20), at(25), at(15, "other")]
        runs = _between_trips(points, [trip])
        # The trip's own fixes go (it draws its way), and nothing joins the stays on either side of it.
        self.assertEqual([[point["timestamp"][14:16] for point in run] for run in runs], [["05", "10"], ["20", "25"], ["15"]])

    def test_a_long_step_between_two_fixes_gives_way_to_a_trip_that_recorded_the_road(self):
        from geologs.domain.place_routes import group
        home, campus, road, first, second = self.campus_tracks()
        # Watched all the way, but 2.5 km between two fixes, straight across the bend at 41.01:
        # a sparse step, so two pieces (see split_at_steps) with the step counted as watched.
        coarse = {**first, "pieces": [self.dense_way(road[:3]), self.dense_way(road[6:])], "observed_m": 9000.0}
        bend = {**second, "pieces": [self.dense_way(road[2:7])], "observed_m": 4000.0}
        (route,) = group([coarse, bend])
        way = [point for part in route["parts"] for point in part["coordinates"]]
        self.assertLess(max(self.off_way(point, way) for point in road[2:7]), 10)

    def test_a_trip_on_another_road_is_another_way_and_each_direction_its_own(self):
        from geologs.domain.place_routes import best_place_route, group
        home, campus, road, first, second = self.campus_tracks()
        south = {**first, "started_at": "2026-10-03T08:00:00+00:00",
                 "pieces": [self.dense_way([[-87.899, 41.0], [-87.88, 40.99], [-87.82, 40.99], [-87.801, 41.0]])]}
        back = {**first, "started_at": "2026-10-04T17:00:00+00:00", "from": campus, "to": home,
                "pieces": [self.dense_way(list(reversed(road)))]}
        routes = group([first, second, south, back])
        self.assertEqual(sorted(len(route["members"]) for route in routes), [1, 1, 2])
        # Three fixes along the north road on the way home: the way back wins over the way there reversed...
        fixes = [[-87.83, 41.0101], [-87.86, 41.0099], [-87.88, 41.0001]]
        route, fit = best_place_route(routes, campus, home, fixes, "driving")
        self.assertEqual((route["members"][0]["started_at"], fit["reverse"]), (back["started_at"], False))
        # ...and without one, the way there serves, reversed; the south road never does.
        route, fit = best_place_route([item for item in routes if item is not route], campus, home, fixes, "driving")
        self.assertEqual((len(route["members"]), fit["reverse"]), (2, True))
        # A walk does not take a road learned by driving.
        self.assertIsNone(best_place_route([item for item in routes if item["mode"] == "driving" and item is not route
                                            and item["members"][0]["started_at"] == south["started_at"]],
                                           home, campus, fixes[::-1], "walking"))

    def test_a_sparse_trip_is_drawn_along_the_way_its_places_pieced_together(self):
        from geologs.services.paths import exclude_path, fill_trip
        from geologs.services.place_routes import regroup
        home, campus, road, first, second = self.campus_tracks()
        for track in (first, second):
            self.db.place_routes.add_track(track)
        regroup(self.db, [(home, campus)])
        fixes = [[-87.86, 41.0101], [-87.82, 41.0049]]
        legs = [{"kind": "gap", "coordinates": [home, fixes[0]]}, {"kind": "gap", "coordinates": fixes, "jump": True},
                {"kind": "gap", "coordinates": [fixes[1], campus]}]
        trip = {"source": "phone", "start": "2026-10-05T08:00:00+00:00", "route": legs, "fixes": fixes, "distance_km": 9.0,
                "mode": None, "modes": {}, "recorded": False,
                "from_lon": home[0], "from_lat": home[1], "to_lon": campus[0], "to_lat": campus[1]}
        filled = fill_trip(self.db, trip)
        # The whole way between the stays is the route, but the short legs onto it from each stay.
        self.assertEqual([leg["kind"] for leg in filled["route"]], ["gap", "fill", "gap"])
        self.assertEqual(filled["route"][1]["past_trips"], 2)
        self.assertEqual((filled["mode"], filled.get("mode_from_paths")), ("driving", True))
        self.assertLess(max(self.off_way(point, filled["route"][1]["coordinates"]) for point in road[1:-1]), 10)
        # A trip excluded leaves its way: the other recorded too little to stand alone.
        exclude_path(self.db, "phone", first["started_at"])
        self.assertEqual(self.db.place_routes.recent(), [])
        self.assertIsNone(self.db.place_routes.track("phone", first["started_at"]))

    def test_the_places_map_gets_the_routes_through_its_view_with_their_trips(self):
        from geologs.services.place_routes import regroup, route_map
        home, campus, road, first, second = self.campus_tracks()
        for track in (first, second):
            self.db.place_routes.add_track(track)
        regroup(self.db, [(home, campus)])
        shown = route_map(self.db, 40.9, -88.0, 41.1, -87.7)
        self.assertEqual([route["trips"] for route in shown["routes"]], [2])
        lines = shown["routes"][0]["lines"]
        # As [lat, lon] for the map, end to end.
        self.assertEqual((lines[0][0], lines[-1][-1]), ([home[1], home[0]], [campus[1], campus[0]]))
        self.assertEqual(route_map(self.db, 10, 10, 11, 11)["routes"], [])
        with self.assertRaises(ValueError):
            route_map(self.db, 41.1, -88.0, 40.9, -87.7)

    def test_an_excluded_route_is_never_learned_again_until_used_again(self):
        from geologs.services.paths import exclude_path, fill_gap, include_path, learn_path_history
        work = self.drive_to_work()
        learn_path_history(self.db)
        (path,) = self.db.paths.recent()
        listed = exclude_path(self.db, path["source"], path["started_at"])
        self.assertEqual((listed["paths"], [item["started_at"] for item in listed["excluded"]]), (0, [path["started_at"]]))
        self.assertEqual(learn_path_history(self.db)["paths"], 0)  # learning again leaves it out
        self.assertIsNone(fill_gap(self.db, [HOME[1], HOME[0]], [work[1], work[0]]))
        listed = include_path(self.db, path["source"], path["started_at"])
        self.assertEqual((listed["paths"], listed["excluded"]), (1, []))
        with self.assertRaises(ValueError):
            include_path(self.db, path["source"], path["started_at"])

    def test_fuel_prices_from_the_eia_for_your_region(self):
        from geologs.services.fuel import refresh_prices, fuel_status
        from geologs.domain.fuel import state_from_address
        self.assertEqual(state_from_address("16801 Chenal Pkwy, Little Rock, Arkansas 72223, United States"), "AR")
        self.assertEqual(state_from_address("16801 Chenal Pkwy, Little Rock, AR 72223, United States of America"), "AR")
        self.assertIsNone(state_from_address("10 Downing St, London SW1A 2AA, United Kingdom"))
        self.fuel_day()
        save_place(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1], "address": "12 Oak St, Little Rock, AR 72223, United States"})
        save_settings(self.db, {"fuel_economy_l100km": 8})
        with patch.dict(os.environ, {"EIA_API_KEY": ""}):
            self.assertEqual(refresh_prices(self.db)["reason"], "no_key")
        weeks = {"response": {"data": [{"period": "2026-09-28", "value": "3.785411784"}, {"period": "2026-09-21", "value": "7.570823568"}]}}
        response = Mock()
        response.read.return_value = json.dumps(weeks).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"EIA_API_KEY": "key"}), patch("geologs.integrations.eia.urllib.request.urlopen", return_value=response) as fetch:
            self.assertEqual(refresh_prices(self.db)["fetched"], 2)
            self.assertIn("facets%5Bduoarea%5D%5B%5D=R30", fetch.call_args.args[0])  # Arkansas is Gulf Coast
            self.assertIn("facets%5Bproduct%5D%5B%5D=EPMR", fetch.call_args.args[0])
            self.assertEqual(refresh_prices(self.db)["reason"], "fresh")  # weekly data, fetched at most twice a day
            status = fuel_status(self.db)
        self.assertEqual((status["area"], status["area_label"], status["state"]), ("R30", "Gulf Coast", "AR"))
        # $3.785/gal is $1/L from Sept 28; this Sept 27 day uses the week before: $2/L.
        day = day_summary(self.db, "2026-09-27", "UTC")
        self.assertAlmostEqual(day["fuel"]["cost"], day["fuel"]["litres"] * 2, delta=0.02)
        # Using your own price still shows what Automatic would charge.
        save_settings(self.db, {"fuel_price_source": "manual"})
        with patch.dict(os.environ, {"EIA_API_KEY": "key"}):
            status = fuel_status(self.db)
        self.assertEqual((status["eia"]["price_per_l"], status["price_per_l"]), (1.0, None))

    def test_search_visits_finds_named_places_by_word(self):
        costco, club = shifted(HOME, 5000, 0), shifted(HOME, -5000, 0)
        for day, place in ((25, costco), (26, club), (27, costco)):
            ingest(self.db, minute_fixes([still(place, 40)], start=datetime(2026, 9, day, 18, tzinfo=timezone.utc)))
        labels = {costco: {"name": "Costco Wholesale", "address": "1 Warehouse Way", "category": "commercial.supermarket",
                           "categories": ["commercial.supermarket"], "provider_place_id": "costco"},
                  club: {"name": "Sam's Club", "address": "9 Depot Road", "category": "commercial.supermarket",
                         "categories": ["commercial.supermarket"], "provider_place_id": "sams"}}
        def label(_store, lat, lon, _radius=None):
            return next(value for place, value in labels.items() if abs(place[0] - lat) + abs(place[1] - lon) < 1e-3)
        with patch("geologs.services.places.place_for_coordinate", side_effect=label):
            result = search_visits(self.db, "costco", timezone_name="America/Chicago")
            self.assertEqual([visit["date"] for visit in result["visits"]], ["2026-09-27", "2026-09-25"])
            self.assertEqual(result["visits"][0]["arrival"], "2026-09-27T13:00-05:00")
            self.assertEqual(result["visits"][0]["weekday"], "Sunday")
            self.assertEqual(result["places"][0]["name"], "Costco Wholesale")
            self.assertEqual(search_visits(self.db, "sams club")["total_visits"], 1)
            self.assertEqual(search_visits(self.db, "supermarket")["total_visits"], 3)
            self.assertEqual(search_visits(self.db, "warehouse costco")["total_visits"], 2)
            self.assertEqual(search_visits(self.db, "walmart")["visits"], [])
            response = dispatch(self.db, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "search_visits", "arguments": {"query": "Costco"}}})
        self.assertEqual(response["result"]["structuredContent"]["result"]["total_visits"], 2)
        with self.assertRaisesRegex(ValueError, "query is required"):
            search_visits(self.db, "  ")

    def test_a_sqlite_archive_is_copied_into_postgres(self):
        # The oldest archives GeoLogs kept in SQLite: points only, before observations were kept apart.
        import sqlite3
        from geologs.services import migrate_sqlite
        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE points (id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT, timestamp TEXT NOT NULL,
                lat REAL NOT NULL, lon REAL NOT NULL, accuracy_m REAL, speed_mps REAL, speed_origin TEXT, mode TEXT NOT NULL);
            CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
                radius_m REAL NOT NULL DEFAULT 150, address TEXT);
            CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, place_id INTEGER, active INTEGER NOT NULL,
                repeat INTEGER NOT NULL, cooldown_minutes INTEGER NOT NULL, created_at TEXT NOT NULL, last_triggered_at TEXT);
            CREATE TABLE events (id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL, point_id INTEGER NOT NULL,
                triggered_at TEXT NOT NULL, webhook_status TEXT);
            CREATE TABLE source_state (source TEXT PRIMARY KEY, point_id INTEGER NOT NULL);
            CREATE TABLE observed_stays (id INTEGER PRIMARY KEY, source TEXT NOT NULL, start_point_id INTEGER NOT NULL,
                end_point_id INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL, lat REAL NOT NULL,
                lon REAL NOT NULL, point_count INTEGER NOT NULL, ongoing INTEGER NOT NULL);
            CREATE TABLE stay_processing_state (source TEXT PRIMARY KEY, last_point_id INTEGER NOT NULL,
                last_timestamp TEXT NOT NULL, candidate TEXT NOT NULL);
            CREATE TABLE geocode_cache (cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, value TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE reverse_regions (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, center_lat REAL NOT NULL,
                center_lon REAL NOT NULL, radius_m REAL NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL);
            CREATE TABLE path_exclusions (id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL, mode TEXT NOT NULL, distance_m REAL NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO places VALUES (1, 'Market', 41, -87.9, 120, NULL);
            INSERT INTO reminders VALUES (1, 'Buy salt', 1, 1, 0, 1440, '2026-09-20T10:00:00+00:00', NULL);
        """)
        old.executemany("INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (minute + 1, "phone", None, f"2026-09-25T10:{minute:02d}:00+00:00", 41, -87.9, 5, None, None, "idle")
            for minute in (0, 10, 20)])
        old.commit()
        old.close()
        target = scratch_database(self)
        counts = migrate_sqlite(path, target)
        self.assertEqual((counts["points"], counts["point_observations"], counts["places"]), (3, 3, 1))
        archive = open_database(target)
        try:
            self.assertEqual([place["name"] for place in archive.places.saved()], ["Market"])
            self.assertEqual([item["text"] for item in archive.reminders.active()], ["Buy salt"])
            self.assertEqual([stay["point_count"] for stay in stays(archive)], [3])  # stays are found on first open
            # New rows continue after the copied ids instead of colliding with them.
            ingest(archive, {"source": "phone", "timestamp": "2026-09-25T10:30:00Z", "lat": 41, "lon": -87.9})
            ids = [point["id"] for point in archive.points.points(limit=None)]
            self.assertEqual((len(ids), len(set(ids))), (4, 4))
            self.assertGreater(ids[0], 3)
        finally:
            archive.close()
        with self.assertRaisesRegex(ValueError, "must be empty"):
            migrate_sqlite(path, target)

    def test_area_stay_migration_rebuilds_an_existing_archive(self):
        path = scratch_database(self)
        archive = open_database(path)
        ingest(archive, lake_run_day())
        archive.conn.execute("DELETE FROM observed_stays_v2 WHERE kind='area'")
        archive.commit()
        archive.close()
        older_schema(path, 12, 14, 17, 18, 19, 20, 21)
        migrated = open_database(path)
        try:
            self.assertEqual([row["kind"] for row in reversed(stays(migrated, limit=None))], ["stationary", "area", "stationary"])
            self.assertEqual(migrated._missing_migrations(STAY_REBUILDS), [])
        finally:
            migrated.close()


CAMPUS = shifted(HOME, 6000, 3000)
LAB = shifted(CAMPUS, 400, 0)


def campus_label(_store, lat, lon, _radius=None):
    from geologs.domain.geo import metres
    if metres(lat, lon, *CAMPUS) < 1500:
        return {"name": "University", "address": "Campus", "category": "education.university",
                "categories": ["education.university"], "provider_place_id": "university"}
    if metres(lat, lon, *HOME) < 200:
        return {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
    return {"name": "Taco Place", "address": "", "category": "catering", "categories": ["catering"], "provider_place_id": "taco"}


def anchored_trips_for_day(db):
    from geologs.services.stays import anchored_trips
    return anchored_trips(db, "2026-09-27T00:00:00+00:00", "2026-09-27T23:59:59+00:00")


def metres_from(row, place):
    from geologs.domain.geo import metres
    return metres(row["lat"], row["lon"], place[0], place[1])


def process_stays_rebuild(db):
    from geologs.services.stays import process_stays
    process_stays(db, rebuild=True)
    db.commit()


class AmbientTests(unittest.TestCase):
    """History's hero: the town, its weather, and a photo of it in that weather, each on its own."""

    def setUp(self):
        from geologs.services import ambient as module
        self.module = module
        module._weather.clear()
        module._reported.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.url = scratch_database(self)
        self.db = open_database(self.url)
        self.place = {"city": "Little Rock", "region": "Arkansas", "country": "United States", "country_code": "US"}
        self.rain = {"current": {"time": "2026-10-17T15:30", "code": 63, "is_day": True, "temperature_c": 14.0,
                                 "feels_like_c": 12.0, "humidity": 90, "wind_kmh": 20.0, "precipitation_mm": 1.2},
                     "day": {"date": "2026-10-17", "code": 63, "high_c": 16.0, "low_c": 10.0}}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def photo(self, photo_id):
        return {"id": photo_id, "url": "https://images.unsplash.com/" + photo_id, "photographer": "Ann",
                "download_location": f"https://api.unsplash.com/photos/{photo_id}/download?ixid=1"}

    def test_weather_codes_name_the_sky_and_the_photo_to_find(self):
        from geologs.domain.weather import condition
        self.assertEqual(condition(63)["photo_word"], "rain")
        self.assertEqual(condition(75)["label"], "Heavy snow")
        self.assertEqual(condition(0, is_day=False)["icon"], "clear-night")
        self.assertEqual(condition(95, is_day=False)["icon"], "storm")
        self.assertEqual(condition(None)["condition"], "cloudy")

    def test_time_of_day_follows_the_sun(self):
        from geologs.domain.weather import time_of_day
        rise, set_ = "2026-10-17T07:16", "2026-10-17T18:31"
        self.assertEqual([time_of_day(clock, rise, set_) for clock in ("05:00", "08:00", "13:00", "17:30", "19:30")],
                         ["night", "morning", "day", "evening", "night"])
        self.assertEqual(time_of_day("2026-10-17T09:00"), "morning")

    def test_photos_are_picked_at_random_without_repeating(self):
        photos = [self.photo(f"p{index}") for index in range(12)]
        with patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": "key"}), \
                patch.object(self.module.localities, "locality", return_value=self.place), \
                patch.object(self.module.forecasts, "today", return_value=self.rain), \
                patch.object(self.module.unsplash, "search", return_value=photos) as search, \
                patch.object(self.module.unsplash, "report_download"):
            shown = [self.module.ambient(self.db, 34.7, -92.3, None, "UTC")["photo"]["id"] for _ in range(10)]
        search.assert_called_once()  # one search, cached
        self.assertEqual(len(set(shown)), 10)  # each of the top ten once before any repeats
        self.assertLessEqual(set(shown), {f"p{index}" for index in range(10)})

    def test_city_in_its_weather_falls_back_to_broader_searches(self):
        searches = []

        def search(query, count):
            searches.append(query)
            return [self.photo("wet-rock")] if query == "Little Rock rain day" else []
        afternoon = datetime(2026, 10, 17, 15, 30, tzinfo=ZoneInfo("America/Chicago"))
        with patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": "key"}), \
                patch.object(self.module, "_local_now", return_value=afternoon), \
                patch.object(self.module.localities, "locality", return_value=self.place) as locality, \
                patch.object(self.module.forecasts, "today", return_value=self.rain) as forecast, \
                patch.object(self.module.unsplash, "search", side_effect=search), \
                patch.object(self.module.unsplash, "report_download") as reported:
            value = self.module.ambient(self.db, 34.74651, -92.28962, None, "America/Chicago")
            again = self.module.ambient(self.db, 34.7467, -92.2898, None, "America/Chicago")
            for _ in range(100):  # the download report is sent in the background
                if reported.called:
                    break
                __import__("time").sleep(0.01)
        self.assertEqual(searches, ["Little Rock Arkansas rain day", "Little Rock rain day"])  # the second view reuses both
        self.assertEqual((value["place"]["city"], value["weather"]["label"], value["weather"]["icon"]),
                         ("Little Rock", "Rain", "rain"))
        self.assertEqual((value["photo"]["id"], value["photo"]["query"], value["time_of_day"]),
                         ("wet-rock", "Little Rock rain day", "day"))
        self.assertNotIn("download_location", value["photo"])
        self.assertEqual(again["photo"]["id"], "wet-rock")
        # Positions go out rounded to about a kilometre; each is asked once.
        locality.assert_called_once_with(34.75, -92.29)
        forecast.assert_called_once_with(34.75, -92.29, "America/Chicago")
        reported.assert_called_once_with("https://api.unsplash.com/photos/wet-rock/download?ixid=1")

    def test_each_part_is_left_out_on_its_own(self):
        with patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": ""}), \
                patch.object(self.module.localities, "locality", side_effect=OSError("offline")), \
                patch.object(self.module.forecasts, "today", return_value=self.rain), \
                patch.object(self.module.unsplash, "search") as search:
            value = self.module.ambient(self.db, 34.7, -92.3, None, "UTC")
        search.assert_not_called()
        self.assertEqual((value["place"], value["photo"], value["photos"]), (None, None, False))
        self.assertEqual(value["weather"]["condition"], "rain")

    def test_a_past_day_uses_its_own_weather(self):
        past = {"current": None, "day": {"date": "2026-01-25", "code": 75, "high_c": -4.7, "low_c": -12.2}}
        with patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": ""}), \
                patch.object(self.module.localities, "locality", return_value=self.place), \
                patch.object(self.module.forecasts, "past_day", return_value=past) as past_day:
            value = self.module.ambient(self.db, 40.71, -74.0, "2026-01-25", "America/New_York")
        self.assertEqual(past_day.call_args.args[:4], (40.71, -74.0, "America/New_York", "2026-01-25"))
        self.assertEqual((value["today"], value["weather"]["label"]), (False, "Heavy snow"))
        with self.assertRaises(ValueError):
            self.module.ambient(self.db, 40.71, -74.0, "2999-01-01", "UTC")
        with self.assertRaises(ValueError):
            self.module.ambient(self.db, 95, 0)

    def test_unsplash_photos_carry_referral_links(self):
        from geologs.integrations import unsplash
        result = {"results": [{"id": "a", "urls": {"raw": "https://images.unsplash.com/a?ixid=1", "small": "s"},
                               "user": {"name": "Ann", "links": {"html": "https://unsplash.com/@ann"}},
                               "links": {"html": "https://unsplash.com/photos/a",
                                         "download_location": "https://api.unsplash.com/photos/a/download"}}]}
        response = Mock()
        response.read.return_value = json.dumps(result).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": "key"}), \
                patch("geologs.integrations.unsplash.urllib.request.urlopen", return_value=response) as fetch:
            photos = unsplash.search("Little Rock rain")
            unsplash.report_download("https://evil.example/photos/a/download")
        self.assertEqual(fetch.call_count, 1)  # only Unsplash's own download endpoint is reported to
        self.assertEqual(fetch.call_args.args[0].get_header("Authorization"), "Client-ID key")
        self.assertEqual(photos[0]["photographer_url"], "https://unsplash.com/@ann?utm_source=geologs&utm_medium=referral")
        self.assertTrue(photos[0]["url"].startswith("https://images.unsplash.com/a?ixid=1&w=2400"))


class LateFixTests(unittest.TestCase):
    """A fix that arrives late resumes stay detection from a checkpoint, with the same result as
    reading every fix again in time order."""

    def setUp(self):
        self.url = scratch_database(self)
        self.db = open_database(self.url)

    def tearDown(self):
        self.db.close()

    @staticmethod
    def long_day():
        """7.5 hours: home, the market, a lake run, home, the market again, home (indices noted)."""
        market, shore, run = shifted(HOME, 3000, 2000), shifted(LAKE, -500, 0), laps(LAKE, 500, 60, 9)
        return minute_fixes([still(HOME, 90),             # 0-89 home
                             line(HOME, market, 10),      # 90-99 drive
                             still(market, 40),           # 100-139 market
                             line(market, shore, 14),     # 140-153 drive
                             run,                         # 154-213 run around the lake
                             line(run[-1], HOME, 12),     # 214-225 drive
                             still(HOME, 120),            # 226-345 home
                             line(HOME, market, 10),      # 346-355 drive
                             still(market, 25),           # 356-380 market
                             line(market, HOME, 10),      # 381-390 drive
                             still(HOME, 60)],            # 391-450 home
                            start=datetime(2026, 9, 27, 6, tzinfo=timezone.utc))

    def snapshot(self):
        stays_found = self.db.rows("""SELECT source,kind,started_at,ended_at,ROUND(lat::numeric,7) AS lat,
            ROUND(lon::numeric,7) AS lon,point_count,ongoing,activity,ROUND(distance_m::numeric,1) AS distance_m,
            ROUND(radius_m::numeric,1) AS radius_m,went_elsewhere,start_observation_id,end_observation_id
            FROM observed_stays_v2 ORDER BY started_at,kind""")
        timeline = self.db.rows("""SELECT t.observation_id,t.role,s.start_observation_id AS stay
            FROM timeline_fixes t LEFT JOIN observed_stays_v2 s ON s.id=t.stay_id ORDER BY t.observation_id""")
        return stays_found, timeline, self.db.stays.state("phone")

    def check(self, *late_ranges):
        fixes = self.long_day()
        late = {index for first, last in late_ranges for index in range(first, last + 1)}
        on_time = [fix for index, fix in enumerate(fixes) if index not in late]
        for start in range(0, len(on_time), 5):  # a phone sending every few minutes
            ingest(self.db, on_time[start:start + 5])
        with patch.object(self.db.stays, "delete_source", side_effect=AssertionError("found every stay again")):
            for index in sorted(late):
                ingest(self.db, fixes[index])
        live = self.snapshot()
        process_stays(self.db, rebuild=True)
        self.db.commit()
        self.assertEqual(live, self.snapshot())
        return live

    def test_a_late_fix_during_a_stay(self):
        self.check((30, 40))

    def test_late_fixes_during_a_drive(self):
        stays_found, _, _ = self.check((95, 99))
        self.assertTrue(any(stay["kind"] == "stationary" for stay in stays_found))

    def test_most_of_a_stop_arriving_late(self):
        self.check((105, 135))

    def test_late_fixes_during_a_run(self):
        stays_found, _, _ = self.check((160, 170))
        self.assertIn("area", {stay["kind"] for stay in stays_found})

    def test_late_fixes_in_the_last_hour_and_earlier_at_once(self):
        self.check((50, 52), (200, 230), (370, 375))

    def test_a_fix_older_than_every_checkpoint_finds_every_stay_again(self):
        fixes = self.long_day()
        for start in range(1, len(fixes), 5):
            ingest(self.db, fixes[start:start + 5])
        ingest(self.db, fixes[0])  # before the first checkpoint: the source starts over
        live = self.snapshot()
        process_stays(self.db, rebuild=True)
        self.db.commit()
        self.assertEqual(live, self.snapshot())

    def test_checkpoints_are_hourly_for_a_week_then_daily(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        week = minute_fixes([still(HOME, 60 * 24 * 10)], start=start, hold=1)[::10]  # ten days, a fix every 10 minutes
        for first in range(0, len(week), 500):
            ingest(self.db, week[first:first + 500], process_analysis=False)
        process_stays(self.db, rebuild=True)
        self.db.commit()
        taken = [row["last_timestamp"] for row in self.db.rows(
            "SELECT last_timestamp FROM stay_checkpoints WHERE source='phone' ORDER BY last_timestamp")]
        newest = datetime.fromisoformat(taken[-1])
        older = [stamp for stamp in taken if datetime.fromisoformat(stamp) < newest - timedelta(days=7)]
        recent = [stamp for stamp in taken if datetime.fromisoformat(stamp) >= newest - timedelta(days=7)]
        self.assertEqual(len(older), len({stamp[:10] for stamp in older}))  # one a day
        self.assertGreaterEqual(len(recent), 7 * 24 - 1)  # about one an hour


class LiveServerCase(unittest.TestCase):
    """A real server on a free port, with a scratch database and known tokens."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.tmp = tempfile.TemporaryDirectory()
        self.db = scratch_database(self)
        self.env = patch.dict(os.environ, {"GEOLOGS_DATABASE_URL": self.db, "GEOLOGS_TOKEN": "main-token",
                                           "GEOLOGS_INGEST_TOKEN": "phone-token"})
        self.env.start()
        for name in ("GEOLOGS_TOKEN_FILE", "GEOLOGS_WEBHOOK_URL"):
            os.environ.pop(name, None)
        self.pool = open_pool(self.db, size=4)
        self.server = Server(("127.0.0.1", 0), self.pool)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.pool.close()
        self.env.stop()
        self.tmp.cleanup()

    def send(self, method, path, body=None, headers=None, form=False):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        elif form:
            headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if not data:
            return response.status, None, response
        try:
            return response.status, json.loads(data), response
        except ValueError:
            return response.status, data, response

    def points(self):
        db = open_database(self.db)
        try:
            return db.points.points(limit=None)
        finally:
            db.close()



class IngestTests(LiveServerCase):
    """Tracking apps posting to a running server, each in its own format."""

    def raw_request(self, request, wait=3):
        """Send raw bytes; return what the server answers before it closes or `wait` passes (None)."""
        import socket
        connection = socket.create_connection(self.server.server_address[:2])
        connection.settimeout(wait)
        try:
            connection.sendall(request)
            return connection.recv(4096)
        except socket.timeout:
            return None
        finally:
            connection.close()

    def test_an_unexpected_failure_is_logged_not_shown(self):
        import contextlib
        bearer = {"Authorization": "Bearer main-token"}
        log = io.StringIO()
        with patch("geologs.presentation.http.places_provider", side_effect=RuntimeError("/srv/secret.db is locked")), \
                contextlib.redirect_stderr(log):
            status, body, _ = self.send("GET", "/api/status", headers=bearer)
        self.assertEqual((status, body), (500, {"error": "internal error"}))
        self.assertIn("/srv/secret.db is locked", log.getvalue())  # the server's owner still sees why

    def test_a_request_mistake_is_said_plainly(self):
        from geologs.presentation.errors import input_message
        bearer = {"Authorization": "Bearer main-token"}
        self.assertEqual(self.send("POST", "/api/places", {"name": "Market", "lat": 41, "lon": -87.9}, bearer)[0], 200)
        status, body, _ = self.send("POST", "/api/places", {"name": "market", "lat": 41.1, "lon": -87.9}, bearer)
        self.assertEqual((status, body), (400, {"error": "you already have a place named market"}))
        self.assertEqual(self.send("POST", "/api/places/delete", {}, bearer)[1], {"error": "missing field: id"})
        # Whatever else a database refuses is never shown in its own words.
        self.assertEqual(input_message(psycopg.errors.UniqueViolation('duplicate key value violates unique constraint "places_name_ci"')),
                         "that conflicts with something already saved")

    def test_a_bad_content_length_is_refused_without_waiting_for_the_body(self):
        # Reading a negative length would wait until the client hangs up, on a route anyone may call.
        for path in ("/api/session", "/api/points", "/api/ingest/owntracks"):
            for length in (b"-1", b"12abc", "\N{SUPERSCRIPT TWO}".encode()):
                with self.subTest(path=path, length=length):
                    reply = self.raw_request(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                             b"Authorization: Bearer main-token\r\nContent-Type: application/json\r\n"
                                             b"Content-Length: " + length + b"\r\n\r\n{}")
                    self.assertIsNotNone(reply, "the server is still waiting")
                    self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:40])

    def test_a_client_that_stops_sending_is_let_go(self):
        self.assertIsNotNone(Handler.timeout)  # the default would wait forever
        self.assertLessEqual(Handler.timeout, 60)
        with patch.object(Handler, "timeout", 0.3):
            # Headers that never finish: the connection is closed instead of holding a thread.
            self.assertEqual(self.raw_request(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n", wait=3), b"")

    def test_owntracks_http_mode_with_basic_auth_and_queued_fixes(self):
        basic = {"Authorization": "Basic " + __import__("base64").b64encode(b"alex:phone-token").decode(),
                 "X-Limit-U": "alex", "X-Limit-D": "pixel"}
        an_hour_ago = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        db = open_database(self.db)
        place = save_place(db, {"name": "Market", "lat": 41.0, "lon": -87.9})
        create_reminder(db, {"text": "Buy salt", "place_id": place["id"]})
        db.close()
        # A fix queued while the phone was offline arrives an hour late, with its own time.
        fix = {"_type": "location", "lat": 41.0, "lon": -87.9, "tst": an_hour_ago, "acc": 12, "vel": 36, "tid": "px",
               "created_at": an_hour_ago + 3600}
        status, reply, _ = self.send("POST", "/api/ingest/owntracks", fix, basic)
        self.assertEqual((status, reply), (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", fix, basic)[:2], (200, []))  # a retry
        [point] = self.points()
        self.assertEqual((point["source"], point["external_id"], point["accuracy_m"]), ("owntracks:alex/pixel", str(an_hour_ago), 12))
        self.assertEqual(datetime.fromisoformat(point["timestamp"]).timestamp(), an_hour_ago)
        db = open_database(self.db)
        self.assertEqual(db.reminders.events(), [])  # too late to announce an arrival
        self.assertAlmostEqual(db.points.observations()[0]["speed_mps"], 10, places=3)  # 36 km/h
        db.close()
        # Other message types, and an empty body, are acknowledged and ignored.
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", {"_type": "transition", "event": "enter"}, basic)[:2], (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", b"", basic)[:2], (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", {"_type": "encrypted", "data": "x"}, basic)[0], 400)
        # Without X-Limit headers, the topic names the device.
        self.send("POST", "/api/ingest/owntracks", {**fix, "tst": an_hour_ago + 60, "topic": "owntracks/alex/ipad"}, {"Authorization": basic["Authorization"]})
        self.assertIn("owntracks:alex/ipad", {point["source"] for point in self.points()})
        wrong = {"Authorization": "Basic " + __import__("base64").b64encode(b"alex:nope").decode()}
        status, _, response = self.send("POST", "/api/ingest/owntracks", fix, wrong)
        self.assertEqual((status, response.getheader("WWW-Authenticate")), (401, 'Basic realm="GeoLogs"'))

    def test_overland_batch_skips_bad_points_and_answers_ok(self):
        feature = lambda lon, lat, when, speed=-1: {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                                                      "properties": {"timestamp": when, "horizontal_accuracy": 8, "speed": speed,
                                                                     "device_id": "iphone"}}
        batch = {"locations": [feature(-87.9, 41.0, "2026-09-27T13:00:00Z", 1.5), feature(-87.9, 41.001, "2026-09-27T13:01:00Z"),
                               feature(-87.9, 95.0, "2026-09-27T13:02:00Z"), {"type": "Feature"}]}
        status, reply, _ = self.send("POST", "/api/ingest/overland?token=phone-token", batch)
        self.assertEqual((status, reply), (200, {"result": "ok"}))
        stored = sorted(self.points(), key=lambda point: point["timestamp"])
        self.assertEqual([(point["source"], point["lat"]) for point in stored], [("overland:iphone", 41.0), ("overland:iphone", 41.001)])
        self.assertEqual(self.send("POST", "/api/ingest/overland?token=phone-token", {"nope": 1})[0], 400)

    def test_osmand_protocol_from_traccar_client(self):
        when = int(datetime(2026, 9, 27, 13, tzinfo=timezone.utc).timestamp())
        status, _, _ = self.send("GET", f"/api/ingest/osmand?id=pixel&lat=41&lon=-87.9&timestamp={when}&speed=10&accuracy=5&token=phone-token")
        self.assertEqual(status, 200)
        # Traccar Client 9 posts JSON, speed already in m/s.
        body = {"device_id": "pixel", "location": {"timestamp": "2026-09-27T13:01:00Z",
                                                   "coords": {"latitude": 41.001, "longitude": -87.9, "accuracy": 6, "speed": 2}}}
        self.assertEqual(self.send("POST", "/api/ingest/osmand?token=phone-token", body)[0], 200)
        db = open_database(self.db)
        speeds = {row["lat"]: row["speed_mps"] for row in db.points.observations(limit=None)}
        db.close()
        self.assertEqual({point["source"] for point in self.points()}, {"osmand:pixel"})
        self.assertAlmostEqual(speeds[41.0], 5.14444, places=4)  # 10 knots

    def test_device_tokens_send_locations_and_nothing_else(self):
        auth = {"Authorization": "Bearer main-token"}
        status, created, _ = self.send("POST", "/api/ingest-tokens", {"name": "Alex's iPhone"}, auth)
        self.assertEqual(status, 200)
        token = created["token"]
        self.assertTrue(token.startswith("glp_") and created["hint"] == token[:8])
        # Listed without the token or its hash; the .env token is reported alongside.
        _, listing, _ = self.send("GET", "/api/ingest-tokens", headers=auth)
        self.assertEqual(listing["env_token"], True)
        self.assertNotIn("token", listing["tokens"][0])
        self.assertNotIn("token_hash", listing["tokens"][0])
        self.assertNotIn(token, json.dumps(listing))
        # Every way a tracker app sends a token works.
        basic = base64.b64encode(f"alex:{token}".encode()).decode()
        for path, headers in (("/api/ingest/generic?device=a", {"Authorization": f"Bearer {token}"}),
                              ("/api/ingest/generic?device=b", {"Authorization": f"Basic {basic}"}),
                              (f"/api/ingest/generic?device=c&token={token}", {})):
            status, _, _ = self.send("POST", path, {"lat": 41, "lon": -87.9}, headers)
            self.assertEqual(status, 200, path)
        _, listing, _ = self.send("GET", "/api/ingest-tokens", headers=auth)
        self.assertEqual(listing["tokens"][0]["last_source"], "generic:c")
        self.assertIsNotNone(listing["tokens"][0]["last_used_at"])
        # It cannot read anything or manage tokens.
        for method, path in (("GET", "/api/sources"), ("GET", "/api/ingest-tokens"), ("POST", "/api/ingest-tokens")):
            status, _, _ = self.send(method, path, {"name": "x"} if method == "POST" else None, {"Authorization": f"Bearer {token}"})
            self.assertEqual(status, 401, path)
        # Revoked, it is refused at once.
        self.send("POST", "/api/ingest-tokens/delete", {"id": created["id"]}, auth)
        status, _, _ = self.send("POST", "/api/ingest/generic?device=a", {"lat": 41, "lon": -87.9}, {"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 401)

    def test_a_locked_device_token_sends_only_as_its_trackers(self):
        auth = {"Authorization": "Bearer main-token"}
        _, ha, _ = self.send("POST", "/api/ingest-tokens", {"name": "Home Assistant", "source": "home_assistant:"}, auth)
        bearer = {"Authorization": f"Bearer {ha['token']}"}
        status, _, _ = self.send("POST", "/api/ingest/generic", {"source": "home_assistant:device_tracker.phone", "lat": 41, "lon": -87.9}, bearer)
        self.assertEqual(status, 200)
        status, body, _ = self.send("POST", "/api/ingest/generic", {"source": "owntracks:alex/phone", "lat": 41, "lon": -87.9}, bearer)
        self.assertEqual((status, body["error"]), (403, "This token can only send as home_assistant: trackers, not owntracks:alex/phone"))
        status, _, _ = self.send("POST", "/api/points", {"source": "manual", "lat": 41, "lon": -87.9}, bearer)
        self.assertEqual(status, 403)
        self.assertEqual([point["source"] for point in self.points()], ["home_assistant:device_tracker.phone"])
        _, sources, _ = self.send("GET", "/api/sources", headers=auth)
        refused = next(device for device in sources["devices"] if device["device"] == "owntracks:alex/phone")
        self.assertEqual(refused["last_status"], 403)

    def test_device_tokens_alone_close_an_open_server(self):
        db = open_database(self.db)
        token = create_ingest_token(db, {"name": "Phone"})["token"]
        db.close()
        with patch.dict(os.environ, {"GEOLOGS_TOKEN": "", "GEOLOGS_INGEST_TOKEN": ""}):
            status, _, _ = self.send("POST", "/api/ingest/generic?device=x", {"lat": 41, "lon": -87.9})
            self.assertEqual(status, 401)
            status, _, _ = self.send("POST", "/api/ingest/generic?device=x", {"lat": 41, "lon": -87.9}, {"Authorization": f"Bearer {token}"})
            self.assertEqual(status, 200)

    def test_home_assistant_blueprint_is_public_and_its_payload_is_accepted(self):
        status, body, response = self.send("GET", "/ha/geologs-location.yaml")
        self.assertEqual((status, response.getheader("Content-Type")), (200, "text/yaml; charset=utf-8"))
        self.assertIn(b"rest_command.geologs_location", body)
        self.assertIn(b'source: "home_assistant:{{ trigger.entity_id }}"', body)
        # What the REST command sends, as rendered by Home Assistant (no accuracy on this tracker).
        sent = {"source": "home_assistant:device_tracker.phone", "timestamp": "2026-10-11T18:00:00.123456+00:00",
                "external_id": "2026-10-11T18:00:00.123456+00:00", "lat": 34.74, "lon": -92.37, "accuracy": None}
        for _ in range(2):  # Home Assistant may resend after a restart; the repeat is recognised
            status, _, _ = self.send("POST", "/api/ingest/generic", sent, {"Authorization": "Bearer phone-token"})
            self.assertEqual(status, 200)
        self.assertEqual([(point["source"], point["lat"]) for point in self.points()],
                         [("home_assistant:device_tracker.phone", 34.74)])

    def test_forgetting_a_failed_device_and_removing_a_tracker(self):
        self.send("POST", "/api/ingest/generic?device=old", {"lat": 1, "lon": 2}, {"Authorization": "Bearer wrong"})
        self.send("POST", "/api/ingest/generic?device=phone", {"lat": 41, "lon": -87.9}, {"Authorization": "Bearer phone-token"})
        auth = {"Authorization": "Bearer main-token"}
        self.send("POST", "/api/ingest/generic?device=tablet&token=main-token", {"lat": "x"})
        _, sources, _ = self.send("GET", "/api/sources", headers=auth)
        # A request with a wrong token is no one's, so it is not listed (generic:old).
        self.assertEqual(sorted(device["device"] for device in sources["devices"]), ["generic:phone", "generic:tablet"])
        status, _, _ = self.send("POST", "/api/ingest-devices/delete", {"kind": "generic", "device": "generic:tablet"}, auth)
        self.assertEqual(status, 200)
        _, sources, _ = self.send("GET", "/api/sources", headers=auth)
        self.assertEqual([device["device"] for device in sources["devices"]], ["generic:phone"])
        # Removing a tracker previews first, then deletes its samples and its place in the priority list.
        self.send("POST", "/api/settings", {"source_priority": ["generic:phone", "other"]}, auth)
        _, preview, _ = self.send("POST", "/api/sources/delete", {"source": "generic:phone"}, auth)
        self.assertEqual((preview["samples"], preview["deleted"], len(self.points())), (1, False, 1))
        _, done, _ = self.send("POST", "/api/sources/delete", {"source": "generic:phone", "confirm": True}, auth)
        self.assertTrue(done["deleted"])
        self.assertEqual(self.points(), [])
        _, settings, _ = self.send("GET", "/api/settings", headers=auth)
        self.assertEqual(settings["source_priority"], ["other"])
        status, _, _ = self.send("POST", "/api/sources/delete", {"source": "generic:phone"}, auth)
        self.assertEqual(status, 400)

    def test_generic_form_query_and_json(self):
        headers = {"Authorization": "Bearer phone-token"}
        form = b"lat=41&lon=-87.9&timestamp=1790514000000&accuracy=7&speed_kmh=18&device=tasker"
        status, reply, _ = self.send("POST", "/api/ingest/generic", form, headers, form=True)
        self.assertEqual((status, reply), (200, {"inserted": 1, "duplicates": 0, "skipped": 0}))
        status, reply, _ = self.send("POST", "/api/ingest/generic?source=shortcut", [
            {"latitude": 41.001, "longitude": -87.9, "time": "2026-09-27T13:05:00-05:00"}, {"latitude": "x"}], headers)
        self.assertEqual(reply, {"inserted": 1, "duplicates": 0, "skipped": 1})
        stored = {point["source"]: point for point in self.points()}
        self.assertEqual(set(stored), {"generic:tasker", "shortcut"})
        self.assertEqual(stored["generic:tasker"]["timestamp"], "2026-09-27T13:00:00+00:00")  # epoch milliseconds
        self.assertEqual(self.send("POST", "/api/ingest/nmea", {}, headers)[0], 404)

    def test_ingest_token_can_send_locations_but_not_read_them(self):
        phone = {"Authorization": "Bearer phone-token"}
        status, reply, _ = self.send("POST", "/api/points", {"source": "phone", "lat": 41, "lon": -87.9,
                                                             "timestamp": "2026-09-27T13:00:00Z"}, phone)
        self.assertEqual((status, reply["inserted"]), (200, 1))
        self.assertEqual(self.send("GET", "/api/points", headers=phone)[0], 401)
        self.assertEqual(self.send("GET", "/api/current?token=phone-token")[0], 401)
        self.assertEqual(self.send("POST", "/api/settings", {"timezone": "UTC"}, phone)[0], 401)
        self.assertEqual(self.send("GET", "/api/points", headers={"Authorization": "Bearer main-token"})[0], 200)
        # The main token works for senders as well.
        self.assertEqual(self.send("GET", "/api/ingest/osmand?id=x&lat=41&lon=-87.9&token=main-token")[0], 200)
        self.assertEqual(self.send("GET", "/api/ingest/osmand?id=x&lat=41&lon=-87.9")[0], 401)

    def test_settings_can_see_which_devices_get_through_and_why_not(self):
        main = {"Authorization": "Bearer main-token"}
        self.assertEqual(self.send("GET", "/api/sources", headers=main)[1]["devices"], [])
        self.send("POST", "/api/ingest/owntracks", {"_type": "location", "lat": 41, "lon": -87.9, "tst": 1790514000},
                  {"Authorization": "Bearer phone-token", "X-Limit-U": "alex", "X-Limit-D": "pixel"})
        # A phone with the wrong token is no one's: with many users, listing it would show one
        # person's device names to another.
        self.send("POST", "/api/ingest/owntracks", {"_type": "location", "lat": 41, "lon": -87.9, "tst": 1790514060},
                  {"Authorization": "Bearer wrong", "X-Limit-U": "alex", "X-Limit-D": "ipad"})
        self.send("POST", "/api/ingest/overland?token=wrong", {"locations": []})
        self.send("POST", "/api/ingest/generic?token=phone-token", [{"lat": 41.001, "lon": -87.9, "device": "tasker"}, {"lat": "x"}])
        status, value, _ = self.send("GET", "/api/sources", headers=main)
        self.assertEqual((status, value["ingest_token"]), (200, True))
        devices = {(item["kind"], item["device"]): item for item in value["devices"]}
        pixel = devices[("owntracks", "owntracks:alex/pixel")]
        self.assertEqual((pixel["last_status"], pixel["received"], pixel["last_message"]), (200, 1, None))
        self.assertNotIn(("owntracks", "owntracks:alex/ipad"), devices)
        self.assertNotIn(("overland", ""), devices)
        self.assertEqual(devices[("generic", "generic:tasker")]["last_message"], "1 malformed fix skipped")
        sources = {item["source"]: item for item in value["sources"]}
        self.assertEqual(set(sources), {"owntracks:alex/pixel", "generic:tasker"})
        self.assertEqual((sources["owntracks:alex/pixel"]["fixes"], sources["owntracks:alex/pixel"]["last_fix"]),
                         (1, "2026-09-27T13:00:00+00:00"))
        self.assertEqual(sources["generic:tasker"]["fixes_24h"], 1)
        # The sender token cannot read any of this.
        self.assertEqual(self.send("GET", "/api/sources", headers={"Authorization": "Bearer phone-token"})[0], 401)

    def test_unreachable_database_is_reported_not_a_dropped_connection(self):
        with patch.object(self.pool, "open", side_effect=RuntimeError("password authentication failed for user \"geologs\"")):
            status, value, _ = self.send("GET", "/api/status", headers={"Authorization": "Bearer main-token"})
        self.assertEqual((status, value["error"]), (503, 'Database unavailable: password authentication failed for user "geologs"'))


WEB = Path(__file__).resolve().parent.parent / "src" / "geologs" / "presentation" / "web"


class PathSettingsTests(LiveServerCase):
    """Learned routes in Settings: what the library holds, and learning again on request."""

    def test_learned_routes_are_listed_and_learned_again_on_request(self):
        bearer = {"Authorization": "Bearer main-token"}
        corner, work = shifted(HOME, 6000, 0), shifted(HOME, 6000, 6000)
        db = open_database(self.db)
        try:
            ingest(db, minute_fixes([still(HOME, 30), line(HOME, corner, 10), line(corner, work, 10), still(work, 30)]))
        finally:
            db.close()
        self.assertEqual(self.send("GET", "/api/paths", headers=bearer)[1]["paths"], 0)
        status, learned, _ = self.send("POST", "/api/paths/learn", {}, bearer)
        self.assertEqual((status, learned["trips"], learned["learned"], learned["paths"]), (200, 1, 1, 1))
        (path,) = self.send("GET", "/api/paths", headers=bearer)[1]["recent"]
        self.assertEqual((path["source"], path["mode"], path["started_at"]), ("phone", "driving", "2026-09-27T13:29:00+00:00"))
        self.assertNotIn("coordinates", path)


class DeploymentTests(unittest.TestCase):
    """What the Docker deployment hands the server."""

    def test_every_setting_the_server_reads_reaches_its_container(self):
        # The app container gets an explicit list of variables: one left off (EIA_API_KEY was)
        # is silently missing however it is set in .env.
        root = Path(__file__).resolve().parent.parent
        read = set(re.findall(r'_env\("([A-Z_]+)"', (root / "src/geologs/config.py").read_text()))
        compose = (root / "docker-compose.yaml").read_text()
        app = compose[compose.index("\n  app:"):]
        passed = set(re.findall(r"^\s+([A-Z_]+):", app[app.index("environment:"):app.index("ports:")], re.M))
        # Set by the image (host, port), read from the mounted token file, or only for the command line.
        elsewhere = {"GEOLOGS_HOST", "GEOLOGS_PORT", "GEOLOGS_INGEST_TOKEN", "GEOLOGS_ENV_FILE"}
        self.assertEqual(read - passed - elsewhere, set())


class ServerCapacityTests(LiveServerCase):
    """Requests share a few database connections; a burst of clients is served, not refused."""

    def test_requests_give_their_connection_back_even_when_they_fail(self):
        bearer = {"Authorization": "Bearer main-token"}
        for _ in range(3 * 4):  # three times the pool, so a single unreturned connection would block
            self.assertEqual(self.send("GET", "/api/current", headers=bearer)[0], 200)
            self.assertEqual(self.send("POST", "/api/places/delete", {}, bearer)[0], 400)
            self.assertEqual(self.send("POST", "/api/points", {"lat": 999}, bearer)[0], 400)
        # Nothing half-written was left on a connection for the next request to commit.
        self.assertEqual(self.points(), [])

    def test_a_burst_of_clients_is_served(self):
        from concurrent.futures import ThreadPoolExecutor
        bearer = {"Authorization": "Bearer main-token"}
        self.assertGreaterEqual(Server.request_queue_size, 128)
        with ThreadPoolExecutor(40) as clients:
            statuses = list(clients.map(lambda _: self.send("GET", "/api/current", headers=bearer)[0], range(200)))
        self.assertEqual(set(statuses), {200})

    def test_a_request_waits_for_a_connection_then_says_the_database_is_busy(self):
        import threading
        busy = open_pool(self.db, size=2, timeout=0.5)
        server = Server(("127.0.0.1", 0), busy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        held = [busy.open(), busy.open()]
        try:
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", "/api/current", headers={"Authorization": "Bearer main-token"})
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertIn("Database unavailable", body["error"])
        finally:
            for db in held:
                db.close()
            server.shutdown()
            server.server_close()
            busy.close()


class AccountTests(LiveServerCase):
    """Signing in with an email and password, and the tokens that stand for a user."""

    def setUp(self):
        super().setUp()
        from geologs.services import accounts
        accounts._attempts.clear()
        db = open_database(self.db)
        try:
            self.bob = accounts.add_user(db, "Bob@example.com", "correct horse battery", name="Bob")
            self.bob_token = accounts.create_api_token(db, self.bob["id"], "laptop")["token"]
        finally:
            db.close()

    def sign_in(self, email, password):
        status, body, response = self.send("POST", "/api/session", {"email": email, "password": password})
        cookie = (response.getheader("Set-Cookie") or "").split(";", 1)[0]
        return status, body, cookie

    def test_a_user_signs_in_and_out_with_an_email_and_password(self):
        status, body, cookie = self.sign_in("bob@EXAMPLE.com", "correct horse battery")
        self.assertEqual((status, body["user"]["email"], body["user"]["role"]), (200, "Bob@example.com", "user"))
        self.assertTrue(cookie.startswith("geologs_session=gls_"))
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[1]["user"]["name"], "Bob")
        self.assertEqual(self.send("DELETE", "/api/session", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[0], 401)  # the session is gone, not just the cookie

    def test_a_wrong_password_and_an_unknown_email_read_the_same(self):
        wrong = self.sign_in("bob@example.com", "not the password")
        unknown = self.sign_in("nobody@example.com", "not the password")
        self.assertEqual((wrong[0], wrong[1]), (unknown[0], unknown[1]))
        self.assertEqual(wrong[:2], (401, {"error": "wrong email or password"}))

    def test_sign_in_waits_after_too_many_attempts(self):
        for _ in range(10):
            self.assertEqual(self.sign_in("bob@example.com", "guess guess guess")[0], 401)
        self.assertEqual(self.sign_in("bob@example.com", "correct horse battery")[0], 429)

    def test_a_disabled_user_is_signed_out_everywhere(self):
        _, _, cookie = self.sign_in("bob@example.com", "correct horse battery")
        db = open_database(self.db)
        try:
            db.users.set_disabled(self.bob["id"], "2026-10-01T00:00:00+00:00")
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.send("GET", "/api/me", headers={"Authorization": "Bearer " + self.bob_token})[0], 401)
        self.assertEqual(self.sign_in("bob@example.com", "correct horse battery")[0], 401)

    def test_the_owner_still_signs_in_with_the_server_token(self):
        status, body, response = self.send("POST", "/api/session", {"token": "main-token"})
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertEqual((status, body["user"]["id"]), (200, 1))
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[1]["user"]["role"], "admin")
        # A browser signed in before accounts keeps working, as the owner.
        legacy = "geologs_session=" + Handler.session_value("main-token")
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": legacy})[1]["user"]["id"], 1)
        self.assertEqual(self.send("POST", "/api/session", {"token": "wrong"})[0], 401)

    def test_a_personal_token_reads_and_writes_as_its_user(self):
        bob = {"Authorization": "Bearer " + self.bob_token}
        self.assertEqual(self.send("GET", "/api/me", headers=bob)[1]["user"]["email"], "Bob@example.com")
        self.assertEqual(self.send("POST", "/api/places", {"name": "Bob's", "lat": 35, "lon": -90}, bob)[0], 200)
        main = {"Authorization": "Bearer main-token"}
        self.assertEqual(self.send("GET", "/api/places", headers=main)[1], [])
        self.assertEqual(self.send("GET", "/api/me", headers={"Authorization": "Bearer gla_madeup"})[0], 401)

    def test_the_account_page_shows_and_ends_your_browsers(self):
        _, _, laptop = self.sign_in("bob@example.com", "correct horse battery")
        _, _, phone = self.sign_in("bob@example.com", "correct horse battery")
        page = self.send("GET", "/api/account", headers={"Cookie": laptop})[1]
        self.assertEqual((page["user"]["email"], page["user"]["has_password"]), ("Bob@example.com", True))
        self.assertEqual(sorted(item["current"] for item in page["sessions"]), [False, True])
        other = next(item["id"] for item in page["sessions"] if not item["current"])
        self.assertEqual(self.send("POST", "/api/account/sessions/end", {"id": other}, {"Cookie": laptop})[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": phone})[0], 401)
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": laptop})[0], 200)

    def test_changing_your_password_needs_the_old_one_and_signs_out_other_browsers(self):
        _, _, laptop = self.sign_in("bob@example.com", "correct horse battery")
        _, _, phone = self.sign_in("bob@example.com", "correct horse battery")
        wrong = self.send("POST", "/api/account/password", {"current": "nope", "password": "a new long password"}, {"Cookie": laptop})
        self.assertEqual(wrong[:2], (400, {"error": "your current password is wrong"}))
        self.assertEqual(self.send("POST", "/api/account/password", {"current": "correct horse battery", "password": "short"},
                                   {"Cookie": laptop})[0], 400)
        changed = self.send("POST", "/api/account/password", {"current": "correct horse battery", "password": "a new long password"},
                            {"Cookie": laptop})
        self.assertEqual(changed[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": laptop})[0], 200)  # this browser stays in
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": phone})[0], 401)
        self.assertEqual(self.sign_in("bob@example.com", "a new long password")[0], 200)

    def test_the_owner_without_a_password_may_set_one_and_an_email(self):
        owner = {"Authorization": "Bearer main-token"}
        self.assertFalse(self.send("GET", "/api/account", headers=owner)[1]["user"]["has_password"])
        self.assertEqual(self.send("POST", "/api/account/email", {"email": "bob@example.com"}, owner)[0], 400)  # taken
        self.assertEqual(self.send("POST", "/api/account/email", {"email": "me@example.com"}, owner)[1]["user"]["email"], "me@example.com")
        self.assertEqual(self.send("POST", "/api/account/password", {"password": "the owner's password"}, owner)[0], 200)
        self.assertEqual(self.sign_in("me@example.com", "the owner's password")[1]["user"]["id"], 1)

    def test_your_name_and_api_tokens(self):
        _, _, cookie = self.sign_in("bob@example.com", "correct horse battery")
        self.assertEqual(self.send("POST", "/api/account", {"name": "Robert"}, {"Cookie": cookie})[1]["name"], "Robert")
        made = self.send("POST", "/api/account/tokens", {"name": "Claude Desktop"}, {"Cookie": cookie})[1]
        self.assertTrue(made["token"].startswith("gla_"))
        self.assertEqual(self.send("GET", "/api/me", headers={"Authorization": "Bearer " + made["token"]})[1]["user"]["name"], "Robert")
        listed = self.send("GET", "/api/account", headers={"Cookie": cookie})[1]["api_tokens"]
        self.assertEqual(sorted(item["name"] for item in listed), ["Claude Desktop", "laptop"])
        self.assertNotIn(made["token"], json.dumps(listed))
        self.assertEqual(self.send("POST", "/api/account/tokens/delete", {"id": made["id"]}, {"Cookie": cookie})[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers={"Authorization": "Bearer " + made["token"]})[0], 401)

    def test_the_sign_in_screen_learns_what_it_can_offer(self):
        self.assertEqual(self.send("GET", "/api/sign-in-options")[1], {"email": False, "token": True, "signup": False})

    def test_passwords_and_tokens_are_kept_only_as_hashes(self):
        with psycopg.connect(self.db) as raw:
            stored = " ".join(str(value) for row in raw.execute(
                "SELECT u.password_hash, t.token_hash FROM users u JOIN api_tokens t ON t.user_id=u.id").fetchall() for value in row)
        self.assertNotIn("correct horse battery", stored)
        self.assertNotIn(self.bob_token, stored)
        self.assertTrue(stored.startswith("scrypt$"))


class MailTests(LiveServerCase):
    """Invites, password resets and address checks, by email through an outbox."""

    def setUp(self):
        super().setUp()
        from geologs.services import accounts, mail
        accounts._attempts.clear()
        self.mail_env = patch.dict(os.environ, {"GEOLOGS_PUBLIC_URL": "https://geologs.example.com/",
                                                "GEOLOGS_SMTP_HOST": "smtp.example.com",
                                                "GEOLOGS_SMTP_FROM": "GeoLogs <geologs@example.com>"})
        self.mail_env.start()
        self.addCleanup(self.mail_env.stop)
        self.sent = []
        sender = patch("geologs.integrations.email.send", side_effect=lambda to, subject, body: self.sent.append((to, subject, body)))
        sender.start()
        self.addCleanup(sender.stop)
        self.mail = mail
        db = open_database(self.db)
        try:
            self.bob = accounts.add_user(db, "bob@example.com", "correct horse battery", name="Bob")
        finally:
            db.close()

    def deliver(self):
        db = open_database(self.db)
        try:
            return self.mail.deliver_due(db)
        finally:
            db.close()

    def link_token(self, kind):
        to, subject, body = self.sent[-1]
        return re.search(r"https://geologs\.example\.com/#" + kind + r"=(\S+)", body).group(1)

    def sign_in(self, password):
        status, _, response = self.send("POST", "/api/session", {"email": "bob@example.com", "password": password})
        return status, (response.getheader("Set-Cookie") or "").split(";", 1)[0]

    def test_a_forgotten_password_is_reset_from_an_emailed_link(self):
        _, old_session = self.sign_in("correct horse battery")
        self.assertEqual(self.send("POST", "/api/password/forgot", {"email": "BOB@example.com"})[1], {"ok": True})
        self.assertEqual(self.deliver(), {"sent": 1, "failed": 0})
        to, subject, body = self.sent[-1]
        self.assertEqual((to, subject), ("bob@example.com", "Reset your GeoLogs password"))
        token = self.link_token("reset")
        status, body, _ = self.send("POST", "/api/password/reset", {"token": token, "password": "a brand new password"})
        self.assertEqual((status, body["user"]["email"]), (200, "bob@example.com"))
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": old_session})[0], 401)  # signed out everywhere
        self.assertEqual(self.sign_in("correct horse battery")[0], 401)
        self.assertEqual(self.sign_in("a brand new password")[0], 200)
        self.assertEqual(self.send("POST", "/api/password/reset", {"token": token, "password": "another password!"})[0], 400)
        with psycopg.connect(self.db) as raw:
            row = raw.execute("SELECT u.email_verified_at, o.body FROM users u, outbox o WHERE u.id=%s", (self.bob["id"],)).fetchone()
        self.assertIsNotNone(row[0])  # the link reached the address
        self.assertEqual(row[1], "")  # a sent message's text, with its link, is not kept

    def test_asking_for_a_reset_says_the_same_whether_or_not_there_is_an_account(self):
        self.assertEqual(self.send("POST", "/api/password/forgot", {"email": "nobody@example.com"})[:2], (200, {"ok": True}))
        self.assertEqual(self.deliver(), {"sent": 0, "failed": 0})
        for _ in range(5):
            self.assertEqual(self.send("POST", "/api/password/forgot", {"email": "bob@example.com"})[0], 200)
        self.assertEqual(self.send("POST", "/api/password/forgot", {"email": "bob@example.com"})[0], 429)

    def test_a_reset_link_expires(self):
        self.send("POST", "/api/password/forgot", {"email": "bob@example.com"})
        self.deliver()
        with psycopg.connect(self.db) as raw:
            raw.execute("UPDATE email_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
        status, body, _ = self.send("POST", "/api/password/reset", {"token": self.link_token("reset"), "password": "a brand new password"})
        self.assertEqual(status, 400)
        self.assertIn("expired", body["error"])

    def test_an_admin_invites_someone_who_then_makes_an_account(self):
        admin = {"Authorization": "Bearer main-token"}
        status, made, _ = self.send("POST", "/api/invites", {"email": "carol@example.com"}, admin)
        self.assertEqual((status, made["emailed"], made["role"]), (200, True, "user"))
        self.assertTrue(made["link"].startswith("https://geologs.example.com/#invite=gli_"))
        bob = self.send("POST", "/api/session", {"email": "bob@example.com", "password": "correct horse battery"})[2]
        bob_cookie = bob.getheader("Set-Cookie").split(";", 1)[0]
        self.assertEqual(self.send("POST", "/api/invites", {"email": "dave@example.com"}, {"Cookie": bob_cookie})[0], 403)
        self.assertEqual(self.send("POST", "/api/invites", {"email": "bob@example.com"}, admin)[0], 400)  # already has an account
        self.deliver()
        token = self.link_token("invite")
        self.assertEqual(self.send("GET", "/api/invite?token=" + token)[1]["email"], "carol@example.com")
        self.assertEqual([item["email"] for item in self.send("GET", "/api/invites", headers=admin)[1]], ["carol@example.com"])
        status, body, response = self.send("POST", "/api/invites/accept", {"token": token, "password": "carol's password", "name": "Carol"})
        self.assertEqual((status, body["user"]["role"]), (200, "user"))
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[1]["user"]["name"], "Carol")  # signed in
        self.assertEqual(self.send("POST", "/api/invites/accept", {"token": token, "password": "carol's password"})[0], 400)
        self.assertEqual(self.send("GET", "/api/invite?token=" + token)[0], 404)

    def test_an_address_is_confirmed_from_an_emailed_link(self):
        _, cookie = self.sign_in("correct horse battery")
        self.assertEqual(self.send("POST", "/api/email/verify/send", {}, {"Cookie": cookie})[1], {"sent": True})
        self.deliver()
        status, body, _ = self.send("POST", "/api/email/verify", {"token": self.link_token("verify")})
        self.assertEqual(status, 200)
        self.assertEqual(self.send("POST", "/api/email/verify/send", {}, {"Cookie": cookie})[1], {"sent": False})  # already confirmed

    def test_a_failing_mail_server_is_tried_again_later_then_given_up(self):
        self.send("POST", "/api/password/forgot", {"email": "bob@example.com"})
        with patch("geologs.integrations.email.send", side_effect=OSError("connection refused")):
            now = datetime.now(timezone.utc)
            db = open_database(self.db)
            try:
                for attempt in range(self.mail.MAX_ATTEMPTS):
                    self.assertEqual(self.mail.deliver_due(db, now + timedelta(hours=attempt * 6))["failed"], 1)
                self.assertEqual(self.mail.deliver_due(db, now + timedelta(days=3))["failed"], 0)  # given up
            finally:
                db.close()
        with psycopg.connect(self.db) as raw:
            attempts, failed, body, error = raw.execute("SELECT attempts,failed_at,body,last_error FROM outbox").fetchone()
        self.assertEqual((attempts, body), (self.mail.MAX_ATTEMPTS, ""))
        self.assertIsNotNone(failed)
        self.assertIn("connection refused", error)

    def test_without_a_mail_server_an_invite_link_is_still_given_to_the_admin(self):
        with patch.dict(os.environ, {"GEOLOGS_SMTP_HOST": ""}):
            made = self.send("POST", "/api/invites", {"email": "erin@example.com"}, {"Authorization": "Bearer main-token"})[1]
            self.assertEqual(self.send("POST", "/api/password/forgot", {"email": "bob@example.com"})[1], {"ok": True})
            self.assertFalse(self.send("GET", "/api/me", headers={"Authorization": "Bearer main-token"})[1]["can_email"])
        self.assertFalse(made["emailed"])
        self.assertTrue(made["link"].startswith("https://geologs.example.com/#invite="))
        self.assertEqual(self.deliver(), {"sent": 0, "failed": 0})



class SmtpTests(unittest.TestCase):
    """What GeoLogs says to the mail server."""

    def test_mail_goes_through_starttls_with_the_configured_login(self):
        from geologs.integrations import email
        settings = {"GEOLOGS_SMTP_HOST": "smtp.example.com", "GEOLOGS_SMTP_FROM": "GeoLogs <geologs@example.com>",
                    "GEOLOGS_SMTP_USERNAME": "geologs", "GEOLOGS_SMTP_PASSWORD": "secret"}
        with patch.dict(os.environ, settings), patch("geologs.integrations.email.smtplib.SMTP") as smtp:
            email.send("carol@example.com", "Hello", "Body text")
        server = smtp.return_value  # a real SMTP object is its own context manager
        self.assertEqual(smtp.call_args.args[:2], ("smtp.example.com", 587))
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("geologs", "secret")
        message = server.send_message.call_args.args[0]
        self.assertEqual((message["To"], message["From"], message["Subject"]),
                         ("carol@example.com", "GeoLogs <geologs@example.com>", "Hello"))

    def test_ssl_and_a_custom_port(self):
        from geologs.integrations import email
        settings = {"GEOLOGS_SMTP_HOST": "smtp.example.com", "GEOLOGS_SMTP_FROM": "geologs@example.com",
                    "GEOLOGS_SMTP_SECURITY": "ssl"}
        with patch.dict(os.environ, settings), patch("geologs.integrations.email.smtplib.SMTP_SSL") as smtp:
            email.send("carol@example.com", "Hello", "Body text")
        self.assertEqual(smtp.call_args.args[:2], ("smtp.example.com", 465))
        smtp.return_value.login.assert_not_called()


class AdminTests(LiveServerCase):
    """The admin portal: accounts, sign-up, the audit log and the server's health; never locations."""

    def setUp(self):
        super().setUp()
        from geologs.services import accounts
        accounts._attempts.clear()
        self.admin = {"Authorization": "Bearer main-token"}  # the owner, an admin
        db = open_database(self.db)
        try:
            self.bob = accounts.add_user(db, "bob@example.com", "correct horse battery", name="Bob")
            self.bob_auth = {"Authorization": "Bearer " + accounts.create_api_token(db, self.bob["id"], "t")["token"]}
        finally:
            db.close()
        fixes = [{"source": "phone", "lat": 41.4321, "lon": -87.6543, "timestamp": f"2026-10-01T12:{minute:02d}:00Z"}
                 for minute in range(20)]
        self.send("POST", "/api/points", fixes, self.bob_auth)

    def count(self):
        """Count every user's archive, as the background worker does."""
        from geologs.services.admin import refresh_user_stats
        db = open_database(self.db)
        try:
            for user_id in db.users.ids():
                db.as_user(user_id)
                refresh_user_stats(db, force=True)
        finally:
            db.close()

    def test_only_admins_reach_the_portal(self):
        for path in ("/api/admin/users", "/api/admin/audit", "/api/admin/health"):
            self.assertEqual(self.send("GET", path, headers=self.bob_auth)[0], 403, path)
            self.assertEqual(self.send("GET", path, headers=self.admin)[0], 200, path)
        self.assertEqual(self.send("POST", "/api/admin/users/role", {"id": self.bob["id"], "role": "admin"}, self.bob_auth)[0], 403)

    def test_admins_see_counts_but_never_locations(self):
        self.count()
        users = {row["email"]: row for row in self.send("GET", "/api/admin/users", headers=self.admin)[1]}
        bob = users["bob@example.com"]
        self.assertEqual((bob["fixes"], bob["sources"], bob["api_tokens"]), (20, 1, 1))
        self.assertEqual(bob["last_fix"], "2026-10-01T12:19:00+00:00")
        text = json.dumps(users)
        self.assertNotIn("41.4321", text)
        self.assertNotIn("-87.6543", text)
        self.assertNotIn("password_hash", text)

    def test_counts_follow_new_fixes(self):
        from geologs.services.admin import refresh_user_stats
        db = open_database(self.db, user_id=self.bob["id"])
        try:
            self.assertEqual(refresh_user_stats(db)["fixes"], 20)
            self.assertIsNone(refresh_user_stats(db))  # nothing new: not counted again
            self.send("POST", "/api/points", {"source": "phone", "lat": 41.4321, "lon": -87.6543,
                                              "timestamp": "2026-10-01T13:00:00Z"}, self.bob_auth)
            self.assertIsNone(refresh_user_stats(db))  # new, but counted moments ago
            with psycopg.connect(self.db) as raw:
                raw.execute("UPDATE user_stats SET updated_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(refresh_user_stats(db)["fixes"], 21)
        finally:
            db.close()

    def test_roles_and_the_last_admin(self):
        self.assertEqual(self.send("POST", "/api/admin/users/role", {"id": 1, "role": "user"}, self.admin)[0], 400)
        self.assertEqual(self.send("POST", "/api/admin/users/role", {"id": self.bob["id"], "role": "admin"}, self.admin)[1]["role"], "admin")
        self.assertEqual(self.send("GET", "/api/admin/users", headers=self.bob_auth)[0], 200)  # Bob is an admin now
        self.assertEqual(self.send("POST", "/api/admin/users/role", {"id": self.bob["id"], "role": "user"}, self.admin)[0], 200)

    def test_disabling_signs_a_user_out_and_spares_the_owner(self):
        self.assertEqual(self.send("POST", "/api/admin/users/disable", {"id": self.bob["id"]}, self.admin)[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers=self.bob_auth)[0], 401)
        self.assertEqual(self.send("POST", "/api/admin/users/disable", {"id": 1}, self.admin)[0], 400)
        self.assertEqual(self.send("POST", "/api/admin/users/disable", {"id": self.bob["id"], "disabled": False}, self.admin)[0], 200)
        self.assertEqual(self.send("GET", "/api/me", headers=self.bob_auth)[0], 200)

    def test_a_reset_link_for_a_user(self):
        with patch.dict(os.environ, {"GEOLOGS_PUBLIC_URL": "https://geologs.example.com"}):
            made = self.send("POST", "/api/admin/users/reset", {"id": self.bob["id"]}, self.admin)[1]
        self.assertFalse(made["emailed"])  # no mail server here: the admin passes the link on
        status, _, _ = self.send("POST", "/api/password/reset", {"token": made["token"], "password": "reset by an admin"})
        self.assertEqual(status, 200)

    def test_deleting_a_user_erases_everything_of_theirs(self):
        wrong = self.send("POST", "/api/admin/users/delete", {"id": self.bob["id"], "email": "someone@else"}, self.admin)
        self.assertEqual(wrong[0], 400)
        self.assertEqual(self.send("POST", "/api/admin/users/delete", {"id": 1, "email": ""}, self.admin)[0], 400)
        self.assertEqual(self.send("POST", "/api/admin/users/delete", {"id": self.bob["id"], "email": "BOB@example.com"}, self.admin)[0], 200)
        with psycopg.connect(self.db) as raw:
            left = raw.execute("""SELECT (SELECT COUNT(*) FROM point_observations WHERE user_id=%s)
                + (SELECT COUNT(*) FROM points WHERE user_id=%s) + (SELECT COUNT(*) FROM api_tokens WHERE user_id=%s)""",
                               (self.bob["id"],) * 3).fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(self.send("GET", "/api/me", headers=self.bob_auth)[0], 401)

    def test_admins_see_about_how_much_of_the_database_each_user_takes(self):
        self.count()
        users = {row["email"]: row for row in self.send("GET", "/api/admin/users", headers=self.admin)[1]}
        self.assertGreater(users["bob@example.com"]["bytes"], 0)
        with psycopg.connect(self.db) as raw:
            total = raw.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        self.assertLess(sum(row["bytes"] or 0 for row in users.values()), total)

    def test_deleting_a_users_location_data_keeps_the_account(self):
        place = self.send("POST", "/api/places", {"name": "Work", "lat": 41.4321, "lon": -87.6543}, self.bob_auth)[1]
        self.assertEqual(self.send("POST", "/api/admin/users/delete-locations",
                                   {"id": self.bob["id"], "email": "someone@else"}, self.admin)[0], 400)
        status, done, _ = self.send("POST", "/api/admin/users/delete-locations",
                                    {"id": self.bob["id"], "email": "bob@example.com"}, self.admin)
        self.assertEqual((status, done["fixes"]), (200, 20))
        with psycopg.connect(self.db) as raw:
            left = raw.execute("""SELECT (SELECT COUNT(*) FROM point_observations WHERE user_id=%s)
                + (SELECT COUNT(*) FROM points WHERE user_id=%s) + (SELECT COUNT(*) FROM observed_stays_v2 WHERE user_id=%s)""",
                               (self.bob["id"],) * 3).fetchone()[0]
            owner = raw.execute("SELECT COUNT(*) FROM users WHERE id=1").fetchone()[0]
        self.assertEqual((left, owner), (0, 1))
        # Bob can still sign in, keeps his place, and his counts say so at once.
        self.assertEqual(self.send("GET", "/api/me", headers=self.bob_auth)[0], 200)
        self.assertEqual([p["id"] for p in self.send("GET", "/api/places", headers=self.bob_auth)[1]], [place["id"]])
        users = {row["email"]: row for row in self.send("GET", "/api/admin/users", headers=self.admin)[1]}
        self.assertEqual(users["bob@example.com"]["fixes"], 0)
        entry = self.send("GET", "/api/admin/audit", headers=self.admin)[1][0]
        self.assertEqual((entry["action"], entry["target_email"], entry["details"]), ("delete locations", "bob@example.com", {"fixes": 20}))
        # The admin's own requests still work as the admin.
        self.assertEqual(self.send("GET", "/api/admin/users", headers=self.admin)[0], 200)
        self.assertEqual(self.send("POST", "/api/admin/users/delete-locations",
                                   {"id": self.bob["id"], "email": "bob@example.com"}, self.bob_auth)[0], 403)

    def test_open_sign_up_is_off_until_an_admin_turns_it_on(self):
        new = {"email": "erin@example.com", "password": "erin's password", "name": "Erin"}
        self.assertEqual(self.send("POST", "/api/signup", new)[0], 403)
        self.assertEqual(self.send("POST", "/api/admin/signup", {"mode": "open"}, self.admin)[1], {"signup": "open"})
        self.assertTrue(self.send("GET", "/api/sign-in-options")[1]["signup"])
        status, body, response = self.send("POST", "/api/signup", new)
        self.assertEqual((status, body["user"]["role"]), (200, "user"))
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertEqual(self.send("GET", "/api/me", headers={"Cookie": cookie})[1]["user"]["name"], "Erin")
        self.assertEqual(self.send("POST", "/api/signup", new)[0], 400)  # that email has an account now

    def test_every_admin_action_is_logged(self):
        self.send("POST", "/api/admin/users/role", {"id": self.bob["id"], "role": "admin"}, self.admin)
        self.send("POST", "/api/invites", {"email": "carol@example.com"}, self.admin)
        self.send("POST", "/api/admin/signup", {"mode": "open"}, self.admin)
        log = self.send("GET", "/api/admin/audit", headers=self.admin)[1]
        self.assertEqual([entry["action"] for entry in log], ["sign-up", "invite", "role"])
        self.assertEqual((log[2]["target_email"], log[2]["details"]), ("bob@example.com", {"from": "user", "to": "admin"}))
        self.assertEqual(log[1]["target_email"], "carol@example.com")

    def test_the_server_page_shows_failed_attempts_jobs_and_the_database(self):
        self.send("POST", "/api/ingest/generic?device=stranger", {"lat": 1, "lon": 2}, {"Authorization": "Bearer wrong"})
        health = self.send("GET", "/api/admin/health", headers=self.admin)[1]
        self.assertEqual([(item["device"], item["status"]) for item in health["failed_attempts"]], [("generic:stranger", 401)])
        self.assertGreater(health["database"]["total"], 0)
        self.assertEqual((health["signup"], health["email"]["can_send"]), ("invite", False))
        self.send("POST", "/api/admin/failures/clear", {}, self.admin)
        self.assertEqual(self.send("GET", "/api/admin/health", headers=self.admin)[1]["failed_attempts"], [])


class TwoFactorTests(LiveServerCase):
    """Two-factor sign-in with an authenticator app, and recovery codes."""

    def setUp(self):
        super().setUp()
        from geologs.services import accounts
        accounts._attempts.clear()
        db = open_database(self.db)
        try:
            accounts.add_user(db, "bob@example.com", "correct horse battery")
        finally:
            db.close()
        _, _, response = self.send("POST", "/api/session", {"email": "bob@example.com", "password": "correct horse battery"})
        self.cookie = {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0]}

    def code(self, offset=0):
        import time as clock
        from geologs.domain.two_factor import STEP_SECONDS, code_at
        return code_at(self.secret, int(clock.time()) // STEP_SECONDS + offset)

    def sign_in(self, code=None):
        body = {"email": "bob@example.com", "password": "correct horse battery"}
        if code is not None:
            body["code"] = code
        return self.send("POST", "/api/session", body)[:2]

    def turn_on(self):
        self.secret = self.send("POST", "/api/account/2fa/begin", {}, self.cookie)[1]["secret"]
        return self.send("POST", "/api/account/2fa/confirm", {"code": self.code()}, self.cookie)[1]["recovery_codes"]

    def test_signing_in_asks_for_a_code_that_works_once(self):
        self.turn_on()
        self.assertEqual(self.sign_in(), (401, {"error": "enter the code from your authenticator app", "needs_code": True}))
        self.assertEqual(self.sign_in("000000")[0], 401)
        code = self.code(1)  # the next step: unused, and within the drift allowed
        self.assertEqual(self.sign_in(code)[0], 200)
        self.assertEqual(self.sign_in(code)[0], 401)  # a code works once
        self.assertTrue(self.send("GET", "/api/account", headers=self.cookie)[1]["two_factor"]["enabled"])

    def test_a_recovery_code_works_once(self):
        codes = self.turn_on()
        self.assertEqual(len(codes), 8)
        self.assertEqual(self.sign_in(codes[0].upper())[0], 200)
        self.assertEqual(self.sign_in(codes[0])[0], 401)
        self.assertEqual(self.send("GET", "/api/account", headers=self.cookie)[1]["two_factor"]["recovery_codes_left"], 7)

    def test_turning_it_off_needs_the_password(self):
        self.turn_on()
        self.assertEqual(self.send("POST", "/api/account/2fa/stop", {"current": "nope"}, self.cookie)[0], 400)
        self.assertEqual(self.send("POST", "/api/account/2fa/stop", {"current": "correct horse battery"}, self.cookie)[0], 200)
        self.assertEqual(self.sign_in()[0], 200)

    def test_a_wrong_code_does_not_turn_it_on(self):
        self.secret = self.send("POST", "/api/account/2fa/begin", {}, self.cookie)[1]["secret"]
        self.assertEqual(self.send("POST", "/api/account/2fa/confirm", {"code": "123456"}, self.cookie)[0], 400)
        self.assertEqual(self.sign_in()[0], 200)  # still off


class IsolationTests(LiveServerCase):
    """Two users with the same tracker name and times, in different towns, never see each other.

    Each asks every read route and MCP tool, naming the other's coordinates and ids, and finds
    nothing of theirs; writes aimed at the other's rows change nothing.
    """
    ALPHA, BRAVO = (41.1234, -87.4321), (35.5678, -90.8765)
    # Routes that ask outside services (the places provider, the weather), which these tests leave alone.
    OUTSIDE = {"/api/geocode", "/api/geocode/place", "/api/reverse", "/api/nearby-places", "/api/ambient"}

    def setUp(self):
        super().setUp()
        from geologs.services import accounts
        db = open_database(self.db)
        try:
            bob = accounts.add_user(db, "bob@example.com", "correct horse battery")
            self.bob = {"Authorization": "Bearer " + accounts.create_api_token(db, bob["id"], "test")["token"]}
        finally:
            db.close()
        self.alice = {"Authorization": "Bearer main-token"}  # the owner
        self.ids = {}
        for who, headers, (lat, lon), word in (("alice", self.alice, self.ALPHA, "Alpha"), ("bob", self.bob, self.BRAVO, "Bravo")):
            fixes = [{"source": "phone", "external_id": f"fix-{minute}", "lat": lat, "lon": lon, "accuracy_m": 5,
                      "timestamp": f"2026-10-01T12:{minute:02d}:00Z"} for minute in range(40)]
            self.assertEqual(self.send("POST", "/api/points", fixes, headers)[1]["inserted"], 40)
            place = self.send("POST", "/api/places", {"name": f"{word} Secret", "lat": lat, "lon": lon}, headers)[1]
            reminder = self.send("POST", "/api/reminders", {"text": f"{word.lower()} secret note", "place_id": place["id"]}, headers)[1]
            label = self.send("POST", "/api/place-labels", {"name": f"{word} Label", "lat": lat, "lon": lon,
                                                            "address": f"{word} Street"}, headers)[1]
            token = self.send("POST", "/api/ingest-tokens", {"name": f"{word} phone"}, headers)[1]
            self.ids[who] = {"place": place["id"], "reminder": reminder["id"], "label": label["id"], "token": token["id"],
                             "lat": lat, "lon": lon}
        self.send("POST", "/api/settings", {"units": "imperial"}, self.alice)

    def markers(self, who):
        word, (lat, lon) = ("Alpha", self.ALPHA) if who == "alice" else ("Bravo", self.BRAVO)
        return (word, word.lower(), str(lat), str(lon))

    def assert_none_of(self, who, text, where):
        for marker in self.markers(who):
            self.assertNotIn(marker, text, f"{who}'s {marker!r} leaked into {where}")

    def get_routes(self):
        source = (Path(__file__).resolve().parent.parent / "src/geologs/presentation/http.py").read_text()
        reads = source[source.index("def do_GET"):source.index("def do_POST")]
        return sorted(set(re.findall(r'url\.path == "(/api/[^"]+)"', reads)) - self.OUTSIDE)

    def test_every_read_route_shows_only_your_own(self):
        routes = self.get_routes()
        self.assertGreater(len(routes), 35)
        for reader, other in (("bob", "alice"), ("alice", "bob")):
            headers, theirs = (self.bob, self.ids["alice"]) if reader == "bob" else (self.alice, self.ids["bob"])
            query = {"date": "2026-10-01", "end_date": "2026-10-01", "timezone": "UTC", "start": "2026-10-01T00:00:00Z",
                     "end": "2026-10-02T00:00:00Z", "lat": theirs["lat"], "lon": theirs["lon"], "radius_m": 5000,
                     "place_id": theirs["place"], "id": theirs["place"], "q": "secret", "limit": 500}
            from urllib.parse import urlencode
            for route in routes:
                status, body, _ = self.send("GET", route + "?" + urlencode(query), headers=headers)
                self.assertIn(status, (200, 400, 403, 404), route)  # 403: an admin-only route
                self.assert_none_of(other, json.dumps(body, default=str), f"{reader}'s {route}")
        # Not vacuous: each does see their own.
        for reader, headers, word in (("bob", self.bob, "Bravo"), ("alice", self.alice, "Alpha")):
            self.assertIn(f"{word} Secret", json.dumps(self.send("GET", "/api/places", headers=headers)[1]))
            day = self.send("GET", "/api/day?date=2026-10-01&timezone=UTC", headers=headers)[1]
            self.assertEqual(day["points"], 40)
        self.assertEqual(self.send("GET", "/api/settings", headers=self.bob)[1]["units"], "metric")

    def test_every_read_tool_shows_only_your_own(self):
        mcp = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        writes = {"save_place", "delete_place", "set_place_label", "set_travel_mode", "remove_place_label",
                  "create_reminder", "cancel_reminder", "ingest_location", "geocode_address", "list_nearby_places",
                  "reverse_geocode"}
        values = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z", "date": "2026-10-01",
                  "end_date": "2026-10-01", "timestamp": "2026-10-01T12:10:00Z", "radius_m": 5000, "query": "secret",
                  "limit": 100, "days": 3650, "timezone": "UTC", "since_id": 0, "offset": 0}
        listed = self.send("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {**mcp, **self.bob})[1]
        tools = [tool for tool in listed["result"]["tools"] if tool["name"] not in writes]
        self.assertGreater(len(tools), 20)
        for reader, other in (("bob", "alice"), ("alice", "bob")):
            headers, theirs = (self.bob, self.ids["alice"]) if reader == "bob" else (self.alice, self.ids["bob"])
            for tool in tools:
                arguments = {name: values.get(name, {"lat": theirs["lat"], "lon": theirs["lon"], "place_id": theirs["place"],
                                                      "id": theirs["place"]}.get(name))
                             for name in tool["inputSchema"]["properties"]}
                arguments = {name: value for name, value in arguments.items() if value is not None}
                call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool["name"], "arguments": arguments}}
                body = self.send("POST", "/mcp", call, {**mcp, **headers})[1]
                self.assert_none_of(other, json.dumps(body, default=str), f"{reader}'s {tool['name']}")

    def test_writes_aimed_at_another_users_rows_change_nothing(self):
        alice = self.ids["alice"]
        for path, payload in (("/api/places/delete", {"id": alice["place"]}),
                              ("/api/reminders/cancel", {"id": alice["reminder"]}),
                              ("/api/place-labels/delete", {"id": alice["label"]}),
                              ("/api/ingest-tokens/delete", {"id": alice["token"]}),
                              ("/api/places", {"id": alice["place"], "name": "Taken", "lat": 1, "lon": 1})):
            self.send("POST", path, payload, self.bob)
        self.assertEqual([place["name"] for place in self.send("GET", "/api/places", headers=self.alice)[1]], ["Alpha Secret"])
        self.assertEqual([item["text"] for item in self.send("GET", "/api/reminders", headers=self.alice)[1]], ["alpha secret note"])
        self.assertEqual([label["name"] for label in self.send("GET", "/api/place-labels", headers=self.alice)[1]], ["Alpha Label"])
        self.assertEqual(len(self.send("GET", "/api/ingest-tokens", headers=self.alice)[1]["tokens"]), 1)

    def test_a_device_token_sends_as_the_user_who_made_it(self):
        token = self.send("POST", "/api/ingest-tokens", {"name": "bob's tablet"}, self.bob)[1]["token"]
        self.assertEqual(self.send("POST", f"/api/ingest/generic?token={token}",
                                   {"lat": 35.6, "lon": -90.9, "device": "tablet"})[0], 200)
        sources = lambda headers: {item["source"] for item in self.send("GET", "/api/sources", headers=headers)[1]["sources"]}
        self.assertIn("generic:tablet", sources(self.bob))
        self.assertNotIn("generic:tablet", sources(self.alice))


class WebAppTests(LiveServerCase):
    """The browser app: its files are served, and its modules fit together."""

    def test_every_file_the_page_loads_is_served_with_its_type(self):
        status, _, response = self.send("GET", "/")
        self.assertEqual((status, response.getheader("Content-Type")), (200, "text/html; charset=utf-8"))
        html = (WEB / "index.html").read_text()
        local = re.findall(r'(?:href|src)="(/(?:css|js)/[^"]+)"', html)
        self.assertIn("/js/main.js", local)
        self.assertGreater(len(local), 10)
        for path in local:
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            connection.close()
            kind = "text/css" if path.endswith(".css") else "text/javascript"
            self.assertEqual((path, response.status, response.getheader("Content-Type")), (path, 200, kind + "; charset=utf-8"))
            self.assertTrue(body)

    def test_module_imports_resolve_to_exported_names(self):
        modules = sorted((WEB / "js").rglob("*.js"))
        self.assertGreater(len(modules), 10)
        exported = {}
        for module in modules:
            text = module.read_text()
            names = set(re.findall(r"^export (?:async )?function (\w+)", text, re.M))
            for declaration in re.findall(r"^export (?:const|let) ([^=;]+)", text, re.M):
                names.update(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)", declaration))
            exported[module] = names
        for module in modules:
            for names, target in re.findall(r'^import \{([^}]+)\} from "([^"]+)";', module.read_text(), re.M):
                path = (module.parent / target).resolve()
                self.assertIn(path, exported, f"{module.name} imports missing {target}")
                for name in (item.strip() for item in names.split(",") if item.strip()):
                    self.assertIn(name, exported[path], f"{module.name} imports {name} that {target} does not export")

    def test_only_web_files_are_served(self):
        for path in ("/js/../http.py", "/css/%2e%2e/tokens.css", "/js/.hidden.js", "/js/main.py", "/css/", "/js/nope.js",
                     "/css//tokens.css"):
            self.assertEqual(self.send("GET", path)[0], 404, path)

    def test_unchanged_files_are_not_sent_again(self):
        status, _, response = self.send("GET", "/css/tokens.css")
        tag = response.getheader("ETag")
        self.assertEqual((status, response.getheader("Cache-Control")), (200, "no-cache"))
        self.assertEqual(self.send("GET", "/css/tokens.css", headers={"If-None-Match": tag})[0], 304)


PHONE_TIMELINE = {
    "semanticSegments": [
        {"startTime": "2024-05-01T08:00:00.000-07:00", "endTime": "2024-05-01T09:00:00.000-07:00",
         "visit": {"topCandidate": {"placeLocation": {"latLng": "45.5252°, -122.6322°"}}}},
        {"startTime": "2024-05-01T09:00:00.000-07:00", "endTime": "2024-05-01T09:20:00.000-07:00",
         "activity": {"start": {"latLng": "45.5252°, -122.6322°"}, "end": {"latLng": "45.5266°, -122.6819°"}}},
        {"startTime": "2024-05-01T09:00:00.000-07:00", "endTime": "2024-05-01T10:00:00.000-07:00",
         "timelinePath": [{"point": "45.5255°, -122.6400°", "time": "2024-05-01T09:05:00.000-07:00"},
                          {"point": "45.5260°, -122.6600°", "time": "2024-05-01T09:12:00.000-07:00"}]},
    ],
    "rawSignals": [{"position": {"LatLng": "45.5266°, -122.6819°", "accuracyMeters": 12, "timestamp": "2024-05-01T09:30:00.000-07:00",
                                 "speedMetersPerSecond": 0.0}}, {"wifiScan": {}}],
}
IOS_TIMELINE = [
    {"startTime": "2024-05-01T08:00:00.000-07:00", "endTime": "2024-05-01T08:40:00.000-07:00",
     "visit": {"topCandidate": {"placeLocation": "geo:45.525200,-122.632200"}}},
    {"startTime": "2024-05-01T09:00:00.000-07:00", "endTime": "2024-05-01T10:00:00.000-07:00",
     "timelinePath": [{"point": "geo:45.525500,-122.640000", "durationMinutesOffsetFromStartTime": "5"}]},
]
SEMANTIC_HISTORY = {"timelineObjects": [
    {"placeVisit": {"location": {"latitudeE7": 455252000, "longitudeE7": -1226322000},
                    "duration": {"startTimestamp": "2019-06-01T15:00:00Z", "endTimestamp": "2019-06-01T15:45:00Z"}}},
    {"activitySegment": {"startLocation": {"latitudeE7": 455252000, "longitudeE7": -1226322000},
                         "endLocation": {"latitudeE7": 455266000, "longitudeE7": -1226819000},
                         "duration": {"startTimestamp": "2019-06-01T15:45:00Z", "endTimestamp": "2019-06-01T16:00:00Z"},
                         "simplifiedRawPath": {"points": [{"latE7": 455260000, "lngE7": -1226600000, "timestamp": "2019-06-01T15:52:00Z"}]}}},
]}


def csv_rows(text):
    import csv
    return list(csv.DictReader(io.StringIO(text)))


class ImportExportTests(unittest.TestCase):
    """Taking your history out (CSV, GPX, GeoJSON) and bringing it in from Google's exports, told apart by content."""

    def setUp(self):
        self.url = scratch_database(self)
        self.db = open_database(self.url)
        self.addCleanup(self.db.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def file(self, content, name="upload"):
        path = Path(self.tmp.name) / name
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return str(path)

    def test_a_csv_export_imports_again_under_another_name(self):
        from geologs.services.imports import import_file
        from geologs.services.samples import export_csv, export_gpx
        for minute in range(3):
            ingest(self.db, {"source": "owntracks:alex/phone", "timestamp": f"2026-09-01T10:0{minute}:00Z",
                             "lat": 45.52 + minute / 1000, "lon": -122.63, "accuracy_m": 8})
        exported = export_csv(self.db)
        self.assertEqual(exported.splitlines()[0], "timestamp,lat,lon,accuracy_m,speed_mps,source")
        self.assertEqual(len(exported.splitlines()), 4)
        result = import_file(self.db, self.file(exported), "restored")
        self.assertEqual((result["format"], result["source"], result["inserted"]), ("csv", "restored", 3))
        self.assertEqual(result["overlaps"], {"owntracks:alex/phone": 3})
        gpx = export_gpx(self.db, "2026-09-01T10:01:00Z")
        self.assertEqual(gpx.count("<trkpt"), 4)  # two fixes from each tracker since 10:01
        self.assertIn("<name>owntracks:alex/phone</name>", gpx)
        self.assertIn('<trkpt lat="45.521" lon="-122.63"><time>2026-09-01T10:01:00Z</time></trkpt>', gpx)

    def test_a_geojson_export_imports_again_and_lines_with_times_do_too(self):
        from geologs.services.imports import import_file
        from geologs.services.samples import export_geojson
        for minute in range(3):
            ingest(self.db, {"source": "owntracks:alex/phone", "timestamp": f"2026-09-01T10:0{minute}:00Z",
                             "lat": 45.52 + minute / 1000, "lon": -122.63, "accuracy_m": 8})
        result = import_file(self.db, self.file(export_geojson(self.db)), "restored")
        self.assertEqual((result["format"], result["source"], result["inserted"], result["skipped"]), ("geojson", "restored", 3, 0))
        restored = [p for p in self.db.points.observations(limit=None) if p["source"] == "restored"]
        self.assertEqual({p["accuracy_m"] for p in restored}, {8})
        converted = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-122.6, 45.5], [-122.61, 45.51]]},
             "properties": {"coordTimes": ["2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"]}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}, "properties": {}}]}
        result = import_file(self.db, self.file(converted))
        self.assertEqual((result["source"], result["inserted"], result["skipped"]), ("geojson", 2, 1))

    def test_the_phone_timeline_export_imports_its_signals_paths_and_visits(self):
        from geologs.services.imports import import_file
        result = import_file(self.db, self.file(PHONE_TIMELINE))
        self.assertEqual((result["format"], result["source"]), ("timeline (phone)", "google_timeline"))
        # The visit's hour (8:00, 8:30, 9:00), the trip's ends (9:00 is the same fix), two path points, one raw fix.
        self.assertEqual(result["inserted"], 7)
        points = self.db.points.observations(limit=None)
        self.assertEqual({p["source"] for p in points}, {"google_timeline"})
        raw = next(p for p in points if p["timestamp"].startswith("2024-05-01T16:30"))
        self.assertEqual(raw["accuracy_m"], 12)
        self.assertEqual(import_file(self.db, self.file(PHONE_TIMELINE))["inserted"], 0)

    def test_the_ios_export_and_takeout_semantic_history_import_too(self):
        from geologs.services.imports import import_file
        ios = import_file(self.db, self.file(IOS_TIMELINE), "iphone")
        self.assertEqual((ios["format"], ios["inserted"]), ("timeline (ios)", 4))  # 8:00, 8:30, 8:40, and 9:05
        semantic = import_file(self.db, self.file(SEMANTIC_HISTORY), "history")
        self.assertEqual((semantic["format"], semantic["inserted"]), ("timeline (semantic)", 5))

    def test_a_full_backup_clones_the_archive_onto_another_server(self):
        from geologs.services import accounts
        from geologs.services.backup import export_backup
        from geologs.services.devices import ingest_token_for
        from geologs.services.imports import import_file
        # An hour at the market, then a drive away: one stay, and a trip.
        for minute in range(0, 60, 2):
            ingest(self.db, {"source": "phone", "timestamp": f"2026-09-01T10:{minute:02d}:00Z", "lat": 45.52, "lon": -122.63, "accuracy_m": 8})
        for step in range(1, 6):
            ingest(self.db, {"source": "phone", "timestamp": f"2026-09-01T11:0{step}:00Z",
                             "lat": 45.52 + step / 200, "lon": -122.63, "accuracy_m": 8, "speed_mps": 15})
        place = save_place(self.db, {"name": "Market", "lat": 45.52, "lon": -122.63, "radius_m": 120})
        create_reminder(self.db, {"text": "Buy salt", "place_id": place["id"]})
        save_settings(self.db, {"timezone": "America/Los_Angeles"})
        save_mode_override(self.db, {"source": "phone", "mode": "transit",
                                     "started_at": "2026-09-01T11:00:00Z", "ended_at": "2026-09-01T11:05:00Z"})
        save_stay_edit(self.db, {"source": "phone", "action": "remove",
                                 "started_at": "2026-09-01T10:20:00Z", "ended_at": "2026-09-01T10:30:00Z"})
        token = create_ingest_token(self.db, {"name": "Phone"})
        path = Path(self.tmp.name) / "backup.json.gz"
        with open(path, "wb") as out:
            export_backup(self.db, out)

        # The new server already has an account (and someone else) before the backup arrives.
        other = open_database(scratch_database(self))
        self.addCleanup(other.close)
        accounts.add_user(other, "someone@example.com", "correct horse battery")
        me = accounts.add_user(other, "me@example.com", "correct horse battery")
        other.as_user(me["id"])
        result = import_file(other, str(path), "ignored")
        self.assertEqual((result["format"], result["inserted"], result["duplicates"]), ("backup", 35, 0))
        restored = result["restored"]
        self.assertEqual((restored["places"], restored["reminders"], restored["mode_overrides"],
                          restored["stay_edits"], restored["ingest_tokens"]), (1, 1, 1, 1, 1))
        self.assertEqual({p["source"] for p in other.points.observations(limit=None)}, {"phone"})
        self.assertEqual(len(other.points.observations(limit=None)), 35)
        reminder = other.reminders.active()[0]
        self.assertEqual((reminder["text"], reminder["place_name"]), ("Buy salt", "Market"))
        self.assertEqual(other.settings.get()["timezone"], "America/Los_Angeles")
        self.assertEqual(len(other.corrections.stay_edits()), 1)
        # The phone's token works on the new server, for the same person.
        self.assertEqual(ingest_token_for(other, token["token"])["user_id"], me["id"])
        # Stays were found again from the restored fixes (the edit is applied as they are read).
        def found(db):
            return [(stay["kind"], stay["started_at"], stay["ended_at"]) for stay in db.stays.between(None, None, None, None)]
        self.assertTrue(found(self.db))
        self.assertEqual(found(other), found(self.db))
        # Restoring again adds nothing.
        again = import_file(other, str(path))
        self.assertEqual((again["inserted"], again["duplicates"]), (0, 35))
        self.assertEqual(sum(again["restored"].values()), 0)

    def backup_of_a_morning(self):
        from geologs.services.backup import export_backup
        for minute in range(0, 40, 2):
            ingest(self.db, {"source": "phone", "timestamp": f"2026-09-01T10:{minute:02d}:00Z", "lat": 45.52, "lon": -122.63, "accuracy_m": 8})
        path = Path(self.tmp.name) / "backup.json.gz"
        with open(path, "wb") as out:
            export_backup(self.db, out)
        other = open_database(scratch_database(self))
        self.addCleanup(other.close)
        return other, str(path)

    def test_part_of_the_history_imported_first_under_its_own_name_is_not_doubled_by_the_backup(self):
        from geologs.services.imports import import_file
        from geologs.services.samples import export_csv
        other, backup = self.backup_of_a_morning()
        part = "\n".join(export_csv(self.db).splitlines()[:11]) + "\n"  # the first ten fixes
        self.assertEqual(import_file(other, self.file(part), "phone")["inserted"], 10)
        result = import_file(other, backup)
        self.assertEqual((result["inserted"], result["duplicates"], result["overlaps"]), (10, 10, {}))
        self.assertEqual(len(other.points.observations(limit=None)), 20)

    def test_part_of_the_history_imported_under_another_name_is_pointed_out(self):
        from geologs.services.imports import import_file
        from geologs.services.samples import export_csv
        other, backup = self.backup_of_a_morning()
        import_file(other, self.file(export_csv(self.db)))  # filed under "csv"
        result = import_file(other, backup)
        self.assertEqual((result["inserted"], result["overlaps"]), (20, {"csv": 20}))

    def test_importing_again_finds_the_stays_an_import_cut_short_never_found(self):
        from geologs.services.imports import import_file
        csv = "timestamp,lat,lon,accuracy_m\n" + "".join(
            f"2026-09-01T10:{minute:02d}:00Z,45.52,-122.63,8\n" for minute in range(0, 40, 2))
        # The fixes came in, then the server stopped before stays were found.
        ingest(self.db, [{**row, "source": "old-gps"} for row in csv_rows(csv)], trigger_reminders=False, process_analysis=False)
        self.assertEqual(self.db.stays.between(None, None, "old-gps", None), [])
        result = import_file(self.db, self.file(csv), "old-gps")
        self.assertEqual((result["inserted"], result["duplicates"]), (0, 20))
        self.assertTrue(self.db.stays.between(None, None, "old-gps", None))

    def test_an_import_running_when_the_server_stopped_says_it_was_cut_short(self):
        from geologs.services.imports import CUT_SHORT, last_import, note_import
        self.assertEqual(last_import(self.db), {"state": "idle"})
        note_import(self.db, {"state": "running", "phase": "reading", "inserted": 4000})
        status = last_import(self.db)
        self.assertEqual((status["state"], status["error"], status["inserted"]), ("failed", CUT_SHORT, 4000))
        note_import(self.db, {"state": "done", "inserted": 9000})
        self.assertEqual(last_import(self.db)["state"], "done")
        # It describes this server, so a backup leaves it out.
        self.assertNotIn("last_import", [row[0] for row in self.db.backup.table_rows("app_settings")])

    def test_exports_are_timed_in_the_archives_timezone_and_import_as_the_same_instant(self):
        from geologs.services.imports import import_file
        from geologs.services.samples import export_csv, export_geojson
        save_settings(self.db, {"timezone": "America/Chicago"})
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-01T03:30:00Z", "lat": 41.88, "lon": -87.63})
        row = export_csv(self.db).splitlines()[1]
        self.assertTrue(row.startswith("2026-08-31T22:30:00-05:00,41.88,-87.63"), row)  # the evening before, as the app shows it
        feature = export_geojson(self.db)["features"][0]
        self.assertEqual(feature["properties"]["timestamp"], "2026-08-31T22:30:00-05:00")
        self.assertEqual(import_file(self.db, self.file(export_csv(self.db)), "phone")["duplicates"], 1)
        self.assertEqual(import_file(self.db, self.file(export_geojson(self.db)), "phone")["duplicates"], 1)

    def test_a_backup_carries_the_timezone_even_when_it_came_from_the_servers_default(self):
        from geologs.services.backup import export_backup
        from geologs.services.imports import import_file
        ingest(self.db, {"source": "phone", "timestamp": "2026-09-01T03:30:00Z", "lat": 41.88, "lon": -87.63})
        path = Path(self.tmp.name) / "backup.json.gz"
        with patch("geologs.config.default_timezone", return_value="America/Chicago"):
            self.assertEqual(self.db.settings.value("timezone"), None)  # never saved here
            with open(path, "wb") as out:
                export_backup(self.db, out)
        other = open_database(scratch_database(self))
        self.addCleanup(other.close)
        import_file(other, str(path))
        self.assertEqual(other.settings.get()["timezone"], "America/Chicago")
        self.assertEqual(other.points.observations(limit=None)[0]["timestamp"], "2026-09-01T03:30:00+00:00")

    def test_a_file_of_another_kind_is_refused_plainly(self):
        from geologs.services.imports import import_file
        with self.assertRaisesRegex(ValueError, "isn't a CSV"):
            import_file(self.db, self.file("name,city\nAlex,Portland\n"))


class ImportServerTests(LiveServerCase):
    def test_an_upload_imports_in_the_background(self):
        import time
        auth = {"Authorization": "Bearer main-token"}
        status, started, _ = self.send("POST", "/api/import?source=old-phone", json.dumps(PHONE_TIMELINE).encode(),
                                       {**auth, "Content-Type": "application/octet-stream"})
        self.assertEqual((status, started["state"]), (202, "running"))
        for _ in range(100):
            _, progress, _ = self.send("GET", "/api/import", headers=auth)
            if progress["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual((progress["state"], progress["source"], progress["inserted"]), ("done", "old-phone", 7))
        from geologs.presentation import http as http_module
        with http_module._imports_lock:
            http_module._imports.clear()  # as after a restart
        _, kept, _ = self.send("GET", "/api/import", headers=auth)
        self.assertEqual((kept["state"], kept["inserted"]), ("done", 7))
        status, body, response = self.send("GET", "/api/export.csv", headers=auth)
        self.assertEqual(status, 200)
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        self.assertEqual(len(body.decode().splitlines()), 8)
        import gzip
        status, body, response = self.send("GET", "/api/backup", headers=auth)
        self.assertEqual((status, response.getheader("Content-Type")), (200, "application/gzip"))
        lines = gzip.decompress(body).decode().splitlines()
        self.assertTrue(lines[0].startswith('{"geologs_backup": 1'))
        self.assertIn('{"table": "points", "columns": ["id", "source", "external_id", "timestamp", "lat", "lon", '
                      '"accuracy_m", "speed_mps", "speed_origin", "mode"]}', lines)
        self.assertEqual(self.send("GET", "/api/backup")[0], 401)

    def test_an_upload_needs_a_file_and_a_sign_in(self):
        self.assertEqual(self.send("POST", "/api/import", b"", {"Authorization": "Bearer main-token"})[0], 400)
        self.assertEqual(self.send("POST", "/api/import", b"timestamp,lat,lon\n")[0], 401)


class TrackerAlertTests(unittest.TestCase):
    """A watched tracker that stops sending is told about once, and once more when it is back."""

    def setUp(self):
        self.url = scratch_database(self)
        self.db = open_database(self.url)
        self.addCleanup(self.db.close)

    def fix_at(self, at, source="owntracks:alex/phone"):
        ingest(self.db, {"source": source, "timestamp": at.isoformat(), "lat": 45.52, "lon": -122.63})

    def test_a_quiet_tracker_is_told_once_and_again_when_back(self):
        from geologs.services.tracker_alerts import check_quiet_trackers, set_quiet_alert, tracker_notices
        now = datetime.now(timezone.utc)
        self.fix_at(now - timedelta(hours=3))
        set_quiet_alert(self.db, {"source": "owntracks:alex/phone", "hours": 2})
        quiet = check_quiet_trackers(self.db, now)
        self.assertEqual([(n["kind"], n["label"], n["hours"]) for n in quiet], [("quiet", "alex/phone (owntracks)", 2)])
        self.assertEqual(check_quiet_trackers(self.db, now + timedelta(minutes=1)), [])
        self.fix_at(now + timedelta(minutes=2))
        back = check_quiet_trackers(self.db, now + timedelta(minutes=3))
        self.assertEqual([n["kind"] for n in back], ["back"])
        self.assertEqual([n["kind"] for n in tracker_notices(self.db)], ["quiet", "back"])
        self.assertEqual(tracker_notices(self.db, quiet[0]["id"]), back)

    def test_an_unwatched_or_recent_tracker_says_nothing(self):
        from geologs.services.tracker_alerts import check_quiet_trackers, set_quiet_alert
        now = datetime.now(timezone.utc)
        self.fix_at(now - timedelta(hours=1))
        self.fix_at(now - timedelta(days=3), source="generic:old-tablet")
        set_quiet_alert(self.db, {"source": "owntracks:alex/phone", "hours": 2})
        self.assertEqual(check_quiet_trackers(self.db, now), [])
        set_quiet_alert(self.db, {"source": "owntracks:alex/phone", "hours": 0})
        self.assertEqual(check_quiet_trackers(self.db, now + timedelta(hours=5)), [])

    def test_only_offered_hours_and_known_trackers_can_be_watched(self):
        from geologs.services.tracker_alerts import set_quiet_alert
        self.fix_at(datetime.now(timezone.utc))
        with self.assertRaises(ValueError):
            set_quiet_alert(self.db, {"source": "owntracks:alex/phone", "hours": 5})
        with self.assertRaises(ValueError):
            set_quiet_alert(self.db, {"source": "owntracks:someone-else", "hours": 6})

    def test_the_owner_hears_by_email_and_webhook(self):
        from geologs.services.tracker_alerts import check_quiet_trackers, set_quiet_alert
        now = datetime.now(timezone.utc)
        self.db.users.set_email(1, "alex@example.com")
        self.db.users.mark_verified(1, now.isoformat())
        self.db.commit()
        self.fix_at(now - timedelta(hours=7))
        set_quiet_alert(self.db, {"source": "owntracks:alex/phone", "hours": 6})
        env = {"GEOLOGS_PUBLIC_URL": "https://geologs.example.com/", "GEOLOGS_SMTP_HOST": "smtp.example.com",
               "GEOLOGS_SMTP_FROM": "GeoLogs <geologs@example.com>", "GEOLOGS_WEBHOOK_URL": "https://hooks.example/geologs"}
        with patch.dict(os.environ, env), patch("geologs.integrations.webhooks.send", return_value="sent") as send:
            check_quiet_trackers(self.db, now)
        self.assertEqual(send.call_args.args[1]["type"], "tracker_quiet")
        self.assertEqual(send.call_args.args[1]["source"], "owntracks:alex/phone")
        with psycopg.connect(self.url) as raw:
            to, subject, body = raw.execute("SELECT to_address,subject,body FROM outbox").fetchone()
        self.assertEqual((to, subject), ("alex@example.com", "alex/phone (owntracks) has gone quiet"))
        self.assertIn("over 6 hours", body)


if __name__ == "__main__":
    unittest.main()
