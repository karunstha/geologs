import json
import hashlib
import hmac
import io
import math
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from geologs.core import Store, classify_motion, database_path, place_details, current_location, deliver_webhook, geocode, mode_for, name_matches, nearby_places, places_provider, street_key, places_provider_identity, reverse_cache_radius, reverse_geocode, route_segments_from_points, smooth_route_segment
from geologs.analysis import daily_brief, daily_briefs, daily_overview, day_summary, insights, place_overview, stored_daily_record, place_patterns, predict_next, resolve_place_patterns, save_scheduled_summaries, search_visits, stays, transitions, visited_places


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
    from geologs.core import metres
    return metres(place[0], place[1], row["lat"], row["lon"])


def lake_run_day(hold=1):
    """Home, drive 10 km, run an hour of laps around a 500 m radius lake, drive back, home."""
    shore = shifted(LAKE, -500, 0)
    run = laps(LAKE, 500, 60, 9)
    return minute_fixes([still(HOME, 31), line(HOME, shore, 12), run, line(run[-1], HOME, 12), still(HOME, 30)],
                        source="home_assistant:phone", hold=hold)
from geologs.imports import import_takeout
from geologs.mcp import dispatch
from geologs.cli import load_env_file, main, poll_home_assistant
from geologs.http import Handler, active_token


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_arrival_once_and_idempotent_source_poll(self):
        place = self.store.save_place({"name": "Walmart", "lat": 41, "lon": -87.9, "radius_m": 120})
        self.store.create_reminder({"text": "Pick up salt", "place_id": place["id"]})
        now = datetime.now(timezone.utc)
        outside = {"source": "ha", "external_id": "a", "timestamp": (now-timedelta(minutes=10)).isoformat(), "lat": 41.01, "lon": -87.9}
        inside = {"source": "ha", "external_id": "b", "timestamp": (now-timedelta(minutes=5)).isoformat(), "lat": 41, "lon": -87.9}
        self.assertEqual(self.store.ingest(outside)["events"], [])
        result = self.store.ingest(inside)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(self.store.ingest(inside)["duplicates"], 1)
        self.assertEqual(len(self.store.observations()), 2)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.reminders(), [])
        self.assertEqual(self.store.reminder_history()[0]["status"], "triggered")

    def test_reminders_are_one_shot_even_if_old_repeat_fields_are_sent(self):
        place = self.store.save_place({"name": "Campus", "lat": 41, "lon": -87.9})
        self.store.create_reminder({"text": "Call friend", "place_id": place["id"], "repeat": True, "cooldown_minutes": 60})
        base = datetime.now(timezone.utc)-timedelta(minutes=25)
        def put(id_, minute, lat):
            return self.store.ingest({"source": "phone", "external_id": str(id_), "timestamp": (base+timedelta(minutes=minute)).isoformat(), "lat": lat, "lon": -87.9})
        self.assertEqual(len(put(1, 0, 41.01)["events"]), 0)
        self.assertEqual(len(put(2, 5, 41)["events"]), 1)
        self.assertEqual(len(put(3, 10, 41)["events"]), 0)
        put(4, 15, 41.01)
        self.assertEqual(len(put(5, 20, 41)["events"]), 0)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.reminders(), [])

    def test_cancelled_reminder_moves_to_history_without_reenable(self):
        place = self.store.save_place({"name": "Market", "lat": 41, "lon": -87.9})
        reminder = self.store.create_reminder({"text": "Buy salt", "place_id": place["id"]})
        self.assertEqual(len(self.store.reminders()), 1)
        self.store.cancel_reminder(reminder["id"])
        self.assertEqual(self.store.reminders(), [])
        history = self.store.reminder_history()
        self.assertEqual(history[0]["status"], "cancelled")
        self.assertIsNotNone(history[0]["cancelled_at"])
        with self.assertRaisesRegex(ValueError, "active reminder"):
            self.store.cancel_reminder(reminder["id"])

    def test_unsaved_coordinate_reminder_stays_out_of_saved_places_and_triggers(self):
        found = {"name": "Lake Example", "address": "Lake Example, Chicago",
                 "category": "natural.water", "provider_place_id": "lake-1"}
        with patch("geologs.core.place_for_coordinate", return_value=found):
            reminder = self.store.create_reminder({"text": "Start my run", "place_name": "Lake Example",
                                                    "lat": 41, "lon": -87.9, "radius_m": 120})
        self.assertEqual(self.store.places(), [])
        self.assertEqual(reminder["place_name"], "Lake Example")
        self.assertEqual(reminder["category"], "natural.water")
        now = datetime.now(timezone.utc)
        self.store.ingest({"source": "phone", "timestamp": (now - timedelta(minutes=5)).isoformat(),
                           "lat": 41.01, "lon": -87.9})
        event = self.store.ingest({"source": "phone", "timestamp": now.isoformat(),
                                   "lat": 41, "lon": -87.9})["events"][0]
        self.assertEqual(event["place"], "Lake Example")
        history = self.store.reminder_history()[0]
        self.assertNotIn("acknowledged_at", history)
        self.assertNotIn("acknowledged_at", {row[1] for row in self.store.db.execute("PRAGMA table_info(events)")})

    def test_stale_and_cross_source_segments_do_not_inflate_distance(self):
        self.store.ingest({"source": "one", "timestamp": "2026-09-26T10:00:00Z", "lat": 41, "lon": -87})
        self.store.ingest({"source": "two", "timestamp": "2026-09-26T10:01:00Z", "lat": 42, "lon": -88})
        self.assertEqual(self.store.stats()["distance_km"], 0)
        self.assertEqual(self.store.trips(), [])
        self.assertEqual(self.store.stats(source="one")["points"], 1)
        self.assertEqual(self.store.stats(source="missing")["points"], 0)
        self.assertEqual(len(self.store.near(41, -87, 100, source="one")), 1)
        self.assertEqual(self.store.near(41, -87, 100, source="two"), [])

    def test_interleaved_sources_keep_independent_route_segments(self):
        points = [
            {"id": 1, "source": "one", "timestamp": "2026-09-26T10:00:00+00:00", "lat": 41, "lon": -87, "accuracy_m": 5},
            {"id": 2, "source": "two", "timestamp": "2026-09-26T10:01:00+00:00", "lat": 42, "lon": -88, "accuracy_m": 5},
            {"id": 3, "source": "one", "timestamp": "2026-09-26T10:02:00+00:00", "lat": 41.001, "lon": -87, "accuracy_m": 5},
            {"id": 4, "source": "two", "timestamp": "2026-09-26T10:03:00+00:00", "lat": 42.001, "lon": -88, "accuracy_m": 5},
        ]
        self.assertEqual(len(route_segments_from_points(points)), 2)

    def test_day_summary_counts_trips_and_unique_observed_places(self):
        self.store.save_place({"name": "Home", "lat": 41.01, "lon": -87.9, "radius_m": 150})
        self.store.save_place({"name": "Campus", "lat": 41, "lon": -87.9, "radius_m": 150})
        self.store.save_place({"name": "Park", "lat": 41.002, "lon": -87.9, "radius_m": 100})
        fixes = [
            (0, 41.01, 0), (15, 41.01, 0),
            (16, 41.007, 15), (17, 41.004, 15), (18, 41.001, 15),
            (20, 41, 0), (35, 41, 0),
            (36, 41.0005, 1.4), (37, 41.001, 1.4),
            (40, 41.002, 0), (55, 41.002, 0),
        ]
        self.store.ingest([{"source": "phone", "timestamp": (datetime(2026, 9, 25, 14, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
                           for minute, lat, speed in fixes])
        self.store.ingest({"source": "other", "timestamp": "2026-09-25T14:20:00Z", "lat": 42, "lon": -88})
        day = day_summary(self.store, "2026-09-25", "America/Chicago", "phone")
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
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T10:01:00Z", "lat": 41.001, "lon": -87.9},
        ])
        first = day_summary(self.store, "2026-09-25", "UTC", "phone")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)
        with patch("geologs.analysis.stays", side_effect=AssertionError("summary was recalculated")):
            self.assertEqual(day_summary(self.store, "2026-09-25", "UTC", "phone"), first)

        self.store.ingest({"source": "phone", "timestamp": "2026-09-25T10:02:00Z", "lat": 41.002, "lon": -87.9})
        with patch("geologs.analysis.stays", wraps=stays) as calculate_stays:
            updated = day_summary(self.store, "2026-09-25", "UTC", "phone")
        self.assertTrue(calculate_stays.called)
        self.assertEqual(updated["points"], 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)

        self.store.save_place({"name": "Campus", "lat": 41, "lon": -87.9})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 0)

    def test_day_stay_uses_visited_place_reverse_geocode_label(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:16:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        found = {"name": "University of Arkansas at Little Rock", "address": "2801 South University Avenue"}
        with patch("geologs.analysis.place_for_coordinate", return_value=found) as reverse:
            day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual(day["visited_places"][0]["name"], "University of Arkansas at Little Rock")
        self.assertEqual(day["visited_places"][0]["address"], "2801 South University Avenue")
        self.assertEqual(day["stays"][0]["display_name"], day["visited_places"][0]["name"])
        reverse.assert_called_once()

    def test_recorded_route_does_not_connect_gaps(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T11:00:00Z", "lat": 41.01, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T11:05:00Z", "lat": 41.011, "lon": -87.9},
        ])
        segments = route_segments_from_points(list(reversed(self.store.points(limit=None))))
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
            self.store.ingest({"source": "phone", "timestamp": f"2026-09-25T10:0{minute}:00Z",
                               "lat": 41 + minute / 1000, "lon": -87.9})
        first = self.store.observations(limit=2, offset=0)
        second = self.store.observations(limit=2, offset=2)
        last = self.store.observations(limit=2, offset=4)
        self.assertEqual([row["id"] for row in first + second + last], [5, 4, 3, 2, 1])

    def test_speed_settings_reported_offset_and_calculated_mode(self):
        self.store.save_settings({"speed_strategy": "reported", "reported_speed_offset_kmh": -3.6})
        self.store.ingest({"source": "reported", "timestamp": "2026-09-25T10:00:00Z",
                           "lat": 41, "lon": -87.9, "speed_mps": 2})
        reported = self.store.observations(source="reported", limit=1)[0]
        self.assertAlmostEqual(reported["speed_mps"], 1)
        self.assertEqual(reported["speed_origin"], "reported_adjusted")
        self.store.save_settings({"speed_strategy": "calculated", "reported_speed_offset_kmh": -3.6})
        self.store.ingest([
            {"source": "calculated", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "speed_mps": 40},
            {"source": "calculated", "timestamp": "2026-09-25T10:01:00Z", "lat": 41.001, "lon": -87.9, "speed_mps": 40},
        ])
        calculated = self.store.observations(source="calculated", limit=1)[0]
        self.assertEqual(calculated["speed_origin"], "estimated")
        self.assertLess(calculated["speed_mps"], 3)

    def test_timezone_setting_is_validated(self):
        value = self.store.save_settings({"timezone": "America/Chicago"})
        self.assertEqual(value["timezone"], "America/Chicago")
        self.assertEqual(self.store.settings()["timezone"], "America/Chicago")
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            self.store.save_settings({"timezone": "not/a-zone"})

    def test_query_paths_have_storage_indexes(self):
        indexes = {row["name"] for row in self.store._rows("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"point_observations_source_time", "observed_stays_v2_source_time",
                         "reminders_active_created", "events_triggered", "daily_summaries_date"}.issubset(indexes))

    def test_trip_classification_uses_connected_trip_context(self):
        self.store.ingest([
            {"source": "phone", "timestamp": f"2026-09-25T10:{minute:02d}:00Z", "lat": lat, "lon": -87.9, "speed_mps": speed}
            for minute, lat, speed in [(0, 41, 0), (15, 41, 0), (16, 41.01, 10), (17, 41.02, 10),
                                       (18, 41.021, 1.5), (19, 41.022, 1.5), (20, 41.023, 0), (35, 41.023, 0)]
        ])
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual(day["activity_counts"]["driving"], 1)
        self.assertEqual(day["activity_counts"]["walking"], 0)
        self.assertEqual(len(day["trips"]), 1)

    def test_red_light_does_not_split_driving_trip(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        self.store.ingest([
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
            for minute, lat, speed in [(0, 41.0, 0), (15, 41.0, 0),
                                       (16, 41.001, 14), (17, 41.002, 14),
                                       (18, 41.002, 0), (19, 41.002, 0),
                                       (20, 41.003, 14), (21, 41.004, 16),
                                       (22, 41.005, 0), (37, 41.005, 0)]
        ])
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual(len(day["trips"]), 1)
        self.assertEqual(day["trips"][0]["mode"], "driving")
        self.assertEqual((day["activity_counts"]["running"], day["activity_counts"]["cycling"]), (0, 0))

    def test_stay_requires_fifteen_minutes(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        self.store.ingest([
            {"source": "short", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 41, "lon": -87.9, "accuracy_m": 5}
            for minute in (0, 14)
        ] + [
            {"source": "confirmed", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 42, "lon": -88.9, "accuracy_m": 5}
            for minute in (0, 15)
        ])
        self.assertEqual(stays(self.store, source="short"), [])
        self.assertEqual(len(stays(self.store, source="confirmed")), 1)

    def test_trip_route_keeps_coverage_gaps_as_separate_segments(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41.000, 0), (15, 41.000, 0),
                 (16, 41.001, 15), (17, 41.002, 15),
                 (60, 41.003, 15), (61, 41.004, 15),
                 (62, 41.005, 0), (77, 41.005, 0)]
        self.store.ingest([
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 5}
            for minute, lat, speed in fixes
        ])
        trip = day_summary(self.store, "2026-09-25", "UTC")["trips"][0]
        self.assertEqual(len(trip["route_segments"]), 2)
        self.assertFalse(trip["coverage_complete"])
        self.assertEqual(trip["coordinates"], [])

    def test_held_home_assistant_state_does_not_strand_next_fix(self):
        self.store.ingest([
            {"source": "home_assistant:phone", "external_id": "held", "timestamp": "2026-09-25T10:00:00Z",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "home_assistant:phone", "external_id": "held", "timestamp": "2026-09-25T10:04:00Z",
             "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "home_assistant:phone", "external_id": "fresh", "timestamp": "2026-09-25T10:05:00Z",
             "lat": 41.045, "lon": -87.9, "accuracy_m": 5},
        ])
        day = day_summary(self.store, "2026-09-25", "UTC")
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
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 90},
            {"source": "phone", "timestamp": "2026-09-25T10:00:30Z", "lat": 41.00002, "lon": -87.9, "accuracy_m": 8},
            {"source": "phone", "timestamp": "2026-09-25T10:04:00Z", "lat": 41.00003, "lon": -87.9, "accuracy_m": 12},
        ])
        self.assertEqual(len(self.store.observations(limit=None)), 3)
        timeline = list(reversed(self.store.timeline(limit=None)))
        self.assertEqual([point["role"] for point in timeline], ["arrival", "latest"])
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual((day["timeline_points"], day["points"]), (2, 3))

    def test_home_assistant_polls_record_repeated_coordinates(self):
        state = {"last_updated": "2026-09-25T08:00:00Z",
                 "attributes": {"latitude": 41, "longitude": -87.9, "gps_accuracy": 8}}
        times = [datetime(2026, 9, 25, 10, 0, 0, 100000, tzinfo=timezone.utc),
                 datetime(2026, 9, 25, 10, 0, 0, 200000, tzinfo=timezone.utc)]
        env = {"HA_URL": "http://home-assistant.test:8123", "HA_TOKEN": "test", "HA_ENTITY": "device_tracker.phone",
               "GEOLOGS_URL": "", "GEOLOGS_DATABASE_URL": "", "GEOLOGS_DB": str(Path(self.tmp.name) / "poll.db")}
        with patch.dict(os.environ, env), patch("geologs.cli.urllib.request.urlopen") as urlopen, \
                patch("geologs.cli.datetime") as clock:
            urlopen.side_effect = lambda *_args, **_kwargs: io.BytesIO(json.dumps(state).encode())
            clock.now.side_effect = times
            first, second = poll_home_assistant(), poll_home_assistant()
        self.assertEqual((first["inserted"], first["recorded"]), (1, 1))
        self.assertEqual((second["duplicates"], second["recorded"]), (1, 1))
        archive = Store(Path(self.tmp.name) / "poll.db")
        try:
            day = day_summary(archive, "2026-09-25", "UTC")
            self.assertEqual((day["timeline_points"], day["points"]), (2, 2))
        finally:
            archive.close()

    def test_repeated_poll_does_not_inherit_previous_movement_speed(self):
        self.store.ingest({"source": "ha", "external_id": "first", "timestamp": "2026-09-25T10:00:00Z",
                           "lat": 41, "lon": -87.9, "accuracy_m": 4})
        moving = {"source": "ha", "external_id": "second", "timestamp": "2026-09-25T10:01:00Z",
                  "lat": 41.0014, "lon": -87.9, "accuracy_m": 4}
        self.store.ingest(moving)
        self.store.ingest({**moving, "timestamp": "2026-09-25T10:01:01Z"})
        newest, previous = self.store.observations(limit=2)
        self.assertEqual(previous["mode"], "running_cycling")
        self.assertEqual(newest["speed_mps"], 0)
        self.assertEqual(newest["speed_origin"], "estimated")
        self.assertEqual(newest["mode"], "idle")

    def test_compact_timeline_keeps_stop_edges_and_movement(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        at_home = [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                    "lat": 41, "lon": -87.9, "accuracy_m": 5} for minute in range(100)]
        self.store.ingest(at_home)
        timeline = list(reversed(self.store.timeline(limit=None)))
        self.assertEqual(len(self.store.observations(limit=None)), 100)
        self.assertEqual([point["role"] for point in timeline], ["arrival", "latest"])
        self.assertEqual([point["timestamp"] for point in timeline], [at_home[0]["timestamp"], at_home[-1]["timestamp"]])
        movement = [{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                     "lat": 41 + (minute - 99) * .001, "lon": -87.9, "accuracy_m": 5}
                    for minute in range(100, 104)]
        self.store.ingest(movement)
        timeline = list(reversed(self.store.timeline(limit=None)))
        self.assertEqual(len(timeline), 6)
        self.assertEqual(timeline[1]["role"], "departure")
        self.assertEqual([point["role"] for point in timeline[-4:]], ["movement"] * 4)

    def test_stays_use_best_accuracy_observation_for_a_timestamp(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 90},
            {"source": "phone", "timestamp": "2026-09-25T10:06:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        timeline = list(reversed(self.store.timeline(limit=None)))
        self.assertEqual(len(self.store.observations(limit=None)), 3)
        self.assertEqual([point["accuracy_m"] for point in timeline], [5, 5])

    def test_existing_observations_get_speed_origin_without_inheriting_speed(self):
        path = Path(self.tmp.name) / "v2.db"
        archive = Store(path)
        archive.ingest({"source": "ha", "external_id": "first", "timestamp": "2026-09-25T10:00:00Z",
                        "lat": 41, "lon": -87.9, "accuracy_m": 4})
        moving = {"source": "ha", "external_id": "second", "timestamp": "2026-09-25T10:01:00Z",
                  "lat": 41.0014, "lon": -87.9, "accuracy_m": 4}
        archive.ingest(moving)
        archive.ingest({**moving, "timestamp": "2026-09-25T10:01:01Z"})
        archive.db.execute("UPDATE point_observations SET speed_mps=NULL")
        archive.db.execute("ALTER TABLE point_observations DROP COLUMN speed_origin")
        archive.db.execute("PRAGMA user_version=2")
        archive.db.commit()
        archive.close()
        migrated = Store(path)
        try:
            newest, previous = migrated.observations(limit=2)
            self.assertEqual(previous["mode"], "running_cycling")
            self.assertEqual(newest["speed_mps"], 0)
            self.assertEqual(newest["mode"], "idle")
        finally:
            migrated.close()

    def test_duplicate_delivery_is_stored_and_better_accuracy_is_selected(self):
        first = {"source": "phone", "external_id": "ha-1", "timestamp": "2026-09-25T10:00:00Z",
                 "lat": 41, "lon": -87.9, "accuracy_m": 80}
        self.store.ingest(first)
        self.assertEqual(self.store.ingest({**first, "accuracy_m": 5})["duplicates"], 1)
        self.assertEqual(len(self.store.points()), 1)
        self.assertEqual(len(self.store.observations()), 2)
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual((day["unique_points"], day["points"]), (1, 2))
        self.assertEqual(self.store.latest()["accuracy_m"], 5)

    def test_retry_of_backfilled_legacy_fix_does_not_add_an_observation(self):
        fix = {"source": "phone", "external_id": "ha-1", "timestamp": "2026-09-25T10:00:00Z",
               "lat": 41, "lon": -87.9, "accuracy_m": 80}
        self.store.ingest(fix)
        point_id = self.store.points()[0]["id"]
        self.store.db.execute("UPDATE point_observations SET legacy_point_id=?,fingerprint=NULL WHERE point_id=?",
                              (point_id, point_id))
        self.store.db.commit()
        self.assertEqual(self.store.ingest(fix)["duplicates"], 1)
        self.assertEqual(len(self.store.observations()), 1)
        self.store.ingest({**fix, "accuracy_m": 5})
        self.assertEqual(len(self.store.observations()), 2)

    def test_short_stop_after_a_drive_is_a_quick_stop(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        self.store.ingest([
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
            for minute, lat, speed in [(0, 41.0, 0), (15, 41.0, 0),
                                       (16, 41.001, 15), (17, 41.002, 15),
                                       (18, 41.002, 0), (19, 41.002, 0), (20, 41.002, 0),
                                       (21, 41.002, 0), (22, 41.002, 0),
                                       (23, 41.0025, 1.5), (24, 41.003, 1.5),
                                       (25, 41.004, 0), (40, 41.004, 0)]
        ])
        trips = list(reversed(day_summary(self.store, "2026-09-25", "UTC")["trips"]))
        # Five minutes still after a drive is a quick stop (a fuel stop) that splits the trip.
        self.assertEqual([trip["mode"] for trip in trips], ["driving", "walking"])
        self.assertEqual([stay["kind"] for stay in reversed(stays(self.store, limit=None))], ["stationary", "stop", "stationary"])
        self.store.save_settings({"quick_stop_minutes": 0})
        trips = list(reversed(day_summary(self.store, "2026-09-25", "UTC")["trips"]))
        self.assertEqual([trip["mode"] for trip in trips], ["driving"])

    def test_held_home_assistant_stop_under_15_minutes_does_not_split_trip(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41.000, "home", 0), (15, 41.000, "home", 0),
                 (16, 41.010, "drive-a", 15), (17, 41.020, "stop", 15),
                 (18, 41.020, "held", 0), (23, 41.020, "held", 0),
                 (24, 41.030, "drive-b", 15), (25, 41.040, "finish", 0),
                 (40, 41.040, "finish", 0)]
        self.store.ingest([{"source": "home_assistant:phone", "external_id": external_id,
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 5}
                           for minute, lat, external_id, speed in fixes])
        trips = list(reversed(day_summary(self.store, "2026-09-25", "UTC")["trips"]))
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["mode"], "driving")
        self.assertTrue(trips[0]["coverage_complete"])

    def test_home_is_hidden_from_visited_list_but_kept_in_daily_overview(self):
        home = self.store.save_place({"name": "Home", "lat": 41, "lon": -87.9, "radius_m": 150})
        campus = self.store.save_place({"name": "Campus", "lat": 41.02, "lon": -87.9, "radius_m": 150})
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41), (16, 41), (17, 41.005), (18, 41.01), (20, 41.02), (36, 41.02)]
        self.store.ingest([{"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "accuracy_m": 5}
                           for minute, lat in fixes])
        reminder = self.store.create_reminder({"text": "Call friend", "place_id": campus["id"]})
        point_id = self.store.points(limit=1)[0]["id"]
        triggered_at = (base + timedelta(minutes=16)).isoformat()
        self.store.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?)",
                              (reminder["id"], point_id, triggered_at))
        self.store.db.execute("UPDATE reminders SET active=0,last_triggered_at=? WHERE id=?", (triggered_at, reminder["id"]))
        self.store.db.commit()
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual([place["name"] for place in day["visited_places"]], ["Campus"])
        self.assertEqual({place["name"] for place in day["places_including_home"]}, {"Home", "Campus"})
        summary = daily_overview(self.store, "2026-09-25", "UTC")
        self.assertEqual(summary["schema_version"], 7)
        self.assertEqual({place["name"] for place in summary["places"]}, {"Home", "Campus"})
        self.assertEqual((summary["totals"]["trips"], summary["trips"][0]["from"], summary["trips"][0]["to"]),
                         (1, "Home", "Campus"))
        self.assertEqual(summary["totals"]["distance_km"], summary["trips"][0]["distance_km"])
        self.assertTrue(next(place for place in summary["places"] if place["name"] == "Home")["home"])
        self.assertEqual(summary["totals"]["reminders_triggered"], 1)
        self.assertEqual(len(self.store.daily_overviews()), 1)
        self.assertEqual(current_location(self.store)["place_name"], "Campus")

    def test_csv_import_and_mcp(self):
        csv = "timestamp,lat,lon,accuracy_m,speed_mps\n2026-09-26T10:00:00Z,41,-87.9,,\n"
        self.assertEqual(self.store.import_csv(csv)["inserted"], 1)
        init = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "geologs")
        tools = dispatch(self.store, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("get_current_location", tool_names)
        self.assertIn("get_compact_timeline", tool_names)
        self.assertIn("get_visited_places", tool_names)
        self.assertIn("get_daily_overview", tool_names)
        self.assertIn("list_reminder_history", tool_names)
        self.assertNotIn("set_reminder_active", tool_names)
        self.assertNotIn("plan_route", tool_names)
        result = dispatch(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_current_location", "arguments": {}}})
        self.assertEqual(result["result"]["structuredContent"]["result"]["lat"], 41)

    def test_mcp_streamable_http_headers_and_notification(self):
        handler = Handler.__new__(Handler)
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json; charset=utf-8"
        handler.headers["Accept"] = "application/json, text/event-stream"
        handler.headers["MCP-Protocol-Version"] = "2025-11-25"
        handler.mcp_error = Mock()
        self.assertTrue(handler.mcp_headers_valid())
        self.assertIsNone(dispatch(self.store, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        handler.headers.replace_header("Accept", "application/json")
        self.assertFalse(handler.mcp_headers_valid())
        handler.mcp_error.assert_called_with(406, -32600, "Accept must include application/json and text/event-stream")
        handler.headers.replace_header("Accept", "application/json, text/event-stream")
        handler.headers.replace_header("MCP-Protocol-Version", "2024-11-05")
        self.assertFalse(handler.mcp_headers_valid())
        handler.mcp_error.assert_called_with(400, -32600, "Unsupported MCP protocol version")

    def test_historical_points_do_not_fire_live_reminders(self):
        place = self.store.save_place({"name": "Market", "lat": 41, "lon": -87.9})
        self.store.create_reminder({"text": "Buy salt", "place_id": place["id"]})
        result = self.store.import_csv("timestamp,lat,lon\n2020-01-01T10:00:00Z,41,-87.9\n")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.store.events(), [])

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

    def test_settings_named_for_geotracker_still_work_with_a_reminder(self):
        # A cron poll with an un-renamed .env must still post to the server, not a local database.
        path = Path(self.tmp.name) / ".env"
        path.write_text("GEOTRACKER_URL=http://127.0.0.1:8766\nGEOTRACKER_TOKEN=old-token\nGEOLOGS_TIMEZONE=UTC\n")
        errors = io.StringIO()
        with patch.dict(os.environ, {"GEOTRACKER_ENV_FILE": str(path), "GEOLOGS_PORT": "9000"}, clear=True), \
                patch("sys.argv", ["geologs", "poll-ha"]), patch("sys.stderr", errors):
            self.assertEqual(main(), 1)  # No Home Assistant settings; it stops before polling.
            self.assertEqual((os.environ["GEOLOGS_URL"], os.environ["GEOLOGS_TOKEN"]), ("http://127.0.0.1:8766", "old-token"))
            self.assertEqual(os.environ["GEOLOGS_PORT"], "9000")
        self.assertIn("rename GEOTRACKER_ENV_FILE, GEOTRACKER_TOKEN, GEOTRACKER_URL to GEOLOGS_*", errors.getvalue())

    def test_default_database_stays_where_an_archive_from_before_the_rename_is(self):
        home = Path(self.tmp.name)
        legacy, current = home / ".local/share/geotracker/geotracker.db", home / ".local/share/geologs/geologs.db"
        with patch.dict(os.environ, {}, clear=True), patch("geologs.core.Path.home", return_value=home):
            self.assertEqual(database_path(), str(current))
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            self.assertEqual(database_path(), str(legacy))
            current.parent.mkdir(parents=True)
            current.touch()
            self.assertEqual(database_path(), str(current))
            os.environ["GEOLOGS_DB"] = str(home / "chosen.db")
            self.assertEqual(database_path(), str(home / "chosen.db"))

    def test_mounted_token_file_tracks_changes(self):
        path = Path(self.tmp.name) / ".env"
        path.write_text("GEOLOGS_TOKEN=first\n")
        with patch.dict(os.environ, {"GEOLOGS_TOKEN_FILE": str(path), "GEOLOGS_TOKEN": "old-env"}):
            self.assertEqual(active_token(), "first")
            path.write_text("GEOLOGS_TOKEN=second\n")
            self.assertEqual(active_token(), "second")

    def test_optional_self_hosted_providers_and_cache(self):
        feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-87.9, 41]}, "properties": {"name": "Market", "city": "Chicago", "type": "house"}}
        with patch.dict(os.environ, {"GEOLOGS_PHOTON_URL": "http://127.0.0.1:2322"}), patch("geologs.core.json_get", return_value={"features": [feature]}) as request:
            self.assertEqual(geocode(self.store, "market")[0]["lat"], 41)
            self.assertEqual(geocode(self.store, "market")[0]["name"], "Market, Chicago")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(reverse_geocode(self.store, 41, -87.9)["cache"], "provider")
            exact = reverse_geocode(self.store, 41, -87.9)
            self.assertEqual(exact["cache"], "region")
            self.assertFalse(exact["approximate"])
            reused = reverse_geocode(self.store, 41.00005, -87.9)
            self.assertEqual(reused["cache"], "region")
            self.assertTrue(reused["approximate"])
            self.assertEqual(reverse_geocode(self.store, 41.001, -87.9)["cache"], "provider")
            self.assertEqual(request.call_count, 3)

    def test_geocode_bias_uses_latest_fix_and_cache_is_area_specific(self):
        self.store.ingest({"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9})
        response = {"results": [{"name": "Neighborhood Market", "formatted": "Neighborhood Market, 1200 Main Street", "lat": 41.01, "lon": -87.9}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.core.provider_request", return_value=response) as request:
            self.assertEqual(geocode(self.store, "market")[0]["name"], "Neighborhood Market")
            self.assertEqual(geocode(self.store, "market")[0]["address"], "Neighborhood Market, 1200 Main Street")
            # Nearby first (a 100 km circle), then anywhere; both favour the latest fix.
            nearby, anywhere = (call.args[1] for call in request.call_args_list)
            self.assertEqual((nearby["filter"], nearby["bias"]), ("circle:-87.9,41.0,100000", "proximity:-87.9,41.0"))
            self.assertEqual((anywhere.get("filter"), anywhere["bias"]), (None, "proximity:-87.9,41.0"))
            geocode(self.store, "market")
            self.assertEqual(request.call_count, 2)
            geocode(self.store, "market", 42, -88)
            self.assertEqual(request.call_count, 4)

    def test_saved_places_reverse_geocode_once_per_cached_region(self):
        response = {"results": [{"formatted": "Market Street", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.core.provider_request", return_value=response) as request:
            first = self.store.save_place({"name": "Shop", "lat": 41, "lon": -87.9})
            second = self.store.save_place({"name": "Office", "lat": 41.00003, "lon": -87.9})
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
        with patch.dict(os.environ, env), patch("geologs.core.provider_request", side_effect=responses) as request:
            result = reverse_geocode(self.store, 34.852116, -92.412809)
            cached = reverse_geocode(self.store, 34.85214, -92.412809)
            wider = reverse_geocode(self.store, 34.852116, -92.412809, radius_m=515)
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
                patch("geologs.core.provider_request", side_effect=responses):
            self.assertEqual(reverse_geocode(self.store, 41, -87.9)["name"], "Planet Fitness")

    def test_no_named_place_nearby_falls_back_to_the_address(self):
        reverse = {"results": [{"formatted": "5511 West Markham Street, Little Rock, AR", "result_type": "building"}]}
        responses = lambda path, params: {"features": []} if path == "/v2/places" else reverse
        env = {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geologs.core.provider_request", side_effect=responses) as request:
            result = reverse_geocode(self.store, 34.749, -92.332)
        self.assertEqual(result["name"], "5511 West Markham Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/v2/places", "/v2/places", "/v1/geocode/reverse"])

    def test_photon_searches_its_radius_and_skips_streets(self):
        street = {"geometry": {"coordinates": [-87.9, 41]}, "properties": {"name": "Main Street", "osm_key": "highway"}}
        cafe = {"geometry": {"coordinates": [-87.9, 41.0003]}, "properties": {"name": "Corner Cafe", "osm_key": "amenity", "osm_value": "cafe"}}
        with patch.dict(os.environ, {"GEOLOGS_PHOTON_URL": "http://127.0.0.1:2322"}), \
                patch("geologs.core.json_get", return_value={"features": [street, cafe]}) as request:
            result = reverse_geocode(self.store, 41, -87.9, radius_m=250)
        self.assertEqual(result["name"], "Corner Cafe")
        self.assertIn("radius=0.3", request.call_args.args[0])

    def test_places_provider_follows_settings_when_configured(self):
        only_geoapify = {"GEOAPIFY_API_KEY": "key", "MAPBOX_ACCESS_TOKEN": "", "GEOLOGS_PHOTON_URL": ""}
        both = {**only_geoapify, "MAPBOX_ACCESS_TOKEN": "token"}
        with patch.dict(os.environ, only_geoapify):
            self.assertEqual(places_provider(self.store), "geoapify")
            with self.assertRaisesRegex(ValueError, "Set MAPBOX_ACCESS_TOKEN to use Mapbox"):
                self.store.save_settings({"places_provider": "mapbox"})
            with self.assertRaisesRegex(ValueError, "places_provider must be one of"):
                self.store.save_settings({"places_provider": "google"})
        with patch.dict(os.environ, both):
            self.assertEqual(self.store.save_settings({"places_provider": "mapbox"})["places_provider"], "mapbox")
            self.assertEqual((places_provider(self.store), places_provider_identity(self.store)), ("mapbox", "mapbox"))
        # A chosen provider whose token was removed falls back to one that works.
        with patch.dict(os.environ, only_geoapify):
            self.assertEqual(places_provider(self.store), "geoapify")
            self.assertEqual(self.store.save_settings({"timezone": "UTC"})["places_provider"], "mapbox")
        with patch.dict(os.environ, {**only_geoapify, "GEOAPIFY_API_KEY": ""}):
            self.assertIsNone(places_provider(self.store))
            with self.assertRaisesRegex(ValueError, "MAPBOX_ACCESS_TOKEN"):
                reverse_geocode(self.store, 41, -87.9)

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
        with patch.dict(os.environ, env), patch("geologs.core.json_get", side_effect=respond):
            park = reverse_geocode(self.store, *origin)
            far = reverse_geocode(self.store, *shifted(origin, 0, 2000))
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
        with patch.dict(os.environ, env), patch("geologs.core.MAPBOX_CATEGORY_REQUEST_INTERVAL_SECONDS", 0), \
                patch("geologs.core.json_get", side_effect=respond) as request:
            places = nearby_places(self.store, *origin, radius_m=1000)
            cached = nearby_places(self.store, *shifted(origin, 5, 0), radius_m=1000)
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
        with patch.dict(os.environ, env), patch("geologs.core.json_get", side_effect=respond) as request:
            self.store.save_settings({"places_provider": "mapbox"})
            listed = geocode(self.store, "costco", 34.85, -92.41, resolve=0)
            geocode(self.store, "costco", 34.85, -92.41, resolve=0)
            # The browser lists suggestions, then fetches coordinates for the one picked.
            self.assertEqual(request.call_count, 1)
            self.assertIn("/search/searchbox/v1/suggest?q=costco", request.call_args.args[0])
            self.assertIn("proximity=-92.41%2C34.85", request.call_args.args[0])
            self.assertEqual((listed[0]["name"], listed[0]["lat"], listed[0]["distance_m"]), ("Costco Wholesale", None, 9800))
            picked = place_details(self.store, listed[0]["provider_place_id"], listed[0]["session"])
            self.assertIn(f"/retrieve/costco?session_token={listed[0]['session']}", request.call_args.args[0])
            self.assertEqual((picked["lat"], picked["lon"]), (34.8, -92.3))
            # Callers that need coordinates (a reminder by place name, MCP) get them resolved.
            resolved = geocode(self.store, "costco", 34.85, -92.41, resolve=1)
            self.assertEqual((resolved[0]["lat"], request.call_count), (34.8, 2))
        # Switching back to Geoapify asks Geoapify instead of reusing Mapbox's cached answer.
        with patch.dict(os.environ, env), patch("geologs.core.provider_request", return_value={"results": []}) as geoapify:
            self.store.save_settings({"places_provider": "geoapify"})
            self.assertEqual(geocode(self.store, "costco", 34.85, -92.41), [])
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
        with patch.dict(os.environ, env), patch("geologs.core.json_get", return_value=suggested):
            self.store.save_settings({"places_provider": "mapbox"})
            found = geocode(self.store, "Tea Capital", *here, resolve=0)
        self.assertEqual([(place["name"], place["distance_m"]) for place in found],
                         [("Tea Capital", 18000), ("Capital Tea", 1234000), ("Tea capital", 14892000)])
        # Geoapify: a nearby search runs first, so a tea shop across town beats an exact name in India.
        def geoapify(path, params):
            if "filter" in params:
                return {"results": [{"name": "Zen Tea", "formatted": "Zen Tea, 5625 Kavanaugh Boulevard, Little Rock", "lat": 34.79, "lon": -92.33}]}
            return {"results": [{"name": "Tea Capital", "formatted": "Tea Capital, Ernakulam, India", "lat": 9.96, "lon": 76.29},
                                {"name": "Zen Tea", "formatted": "Zen Tea, 5625 Kavanaugh Boulevard, Little Rock", "lat": 34.79, "lon": -92.33}]}
        with patch.dict(os.environ, {**env, "GEOAPIFY_API_KEY": "key"}), patch("geologs.core.provider_request", side_effect=geoapify):
            self.store.save_settings({"places_provider": "geoapify"})
            found = geocode(self.store, "Tea Capital", *here)
        self.assertEqual([place["name"] for place in found], ["Zen Tea", "Tea Capital"])
        self.assertLess(found[0]["distance_m"], 15000)
        self.assertGreater(found[1]["distance_m"], 14_000_000)

    def test_geoapify_reverse_prefers_place_name_and_keeps_address(self):
        response = {"results": [{"name": "War Memorial Park",
                                  "formatted": "War Memorial Park, 5511 West Markham Street, Little Rock, AR",
                                  "address_line1": "War Memorial Park", "result_type": "amenity",
                                  "category": "leisure.park", "distance": 8}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.core.provider_request", return_value=response) as request:
            result = reverse_geocode(self.store, 34.749, -92.332)
            nearby = reverse_geocode(self.store, 34.74903, -92.332)
        self.assertEqual(result["name"], "War Memorial Park")
        self.assertEqual(result["place_name"], "War Memorial Park")
        self.assertEqual(result["address"], "War Memorial Park, 5511 West Markham Street, Little Rock, AR")
        self.assertEqual(nearby["name"], "War Memorial Park")
        self.assertEqual(nearby["cache"], "region")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual(request.call_count, 3)

    def test_geoapify_reverse_uses_address_when_place_is_unnamed(self):
        response = {"results": [{"formatted": "1200 Main Street, Little Rock, AR", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOLOGS_PHOTON_URL": ""}), patch("geologs.core.provider_request", return_value=response):
            result = reverse_geocode(self.store, 34.74, -92.27)
        self.assertEqual(result["name"], "1200 Main Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")

    def test_webhook_uses_hermes_compatible_hmac_v2_signature(self):
        event = {"id": 99, "text": "Pick up salt", "place": "Walmart"}
        env = {"GEOLOGS_WEBHOOK_URL": "http://host.docker.internal:8644/webhooks/geologs-reminders",
               "GEOLOGS_WEBHOOK_SECRET": "shared-secret", "GEOLOGS_WEBHOOK_TOKEN": ""}
        with patch.dict(os.environ, env), patch("geologs.core.urllib.request.urlopen", return_value=io.BytesIO()) as urlopen:
            deliver_webhook(self.store, [event])
        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        timestamp = headers["x-webhook-timestamp"]
        expected = hmac.new(b"shared-secret", timestamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-webhook-signature-v2"], expected)

    def test_reverse_cache_expires_and_coarse_results_have_no_assumed_area(self):
        self.assertEqual(reverse_cache_radius("city"), 0)
        self.assertEqual(reverse_cache_radius("building"), 15)
        self.assertEqual(reverse_cache_radius("street"), 30)
        self.store.save_reverse_region("provider", 41, -87.9, 30, {"name": "First"})
        self.assertEqual(self.store.reverse_region("provider", 41.0001, -87.9)["name"], "First")
        self.assertIsNone(self.store.reverse_region("other", 41, -87.9))
        self.store.db.execute("UPDATE reverse_regions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.store.db.commit()
        self.assertIsNone(self.store.reverse_region("provider", 41, -87.9))

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
        self.store.ingest(batch)
        self.assertEqual(len(stays(self.store)), 7)
        self.assertEqual(len(place_patterns(self.store)[0]), 2)
        self.assertEqual(sum(t["count"] for t in transitions(self.store)), 6)
        self.assertEqual(predict_next(self.store)["frequency"], 1)
        self.assertEqual(day_summary(self.store, "2026-09-25", "UTC")["points"], 21)
        place = self.store.save_place({"name": "University", "lat": 41, "lon": -87.9})
        # Fixes come every 8 minutes, so each visit lasts until shortly before the next fix elsewhere.
        self.assertEqual(self.store.place_visits(place["id"])["total_observed_minutes"], 84.4)
        ranked = visited_places(self.store)
        # A saved place is a zone: with nothing named right there, the place inside is known by the zone's name.
        university = next(item for item in ranked if item["zone"] == "University")
        self.assertEqual((university["display_name"], university["saved"], university["visits"]), ("University", False, 4))
        self.assertGreaterEqual(ranked[0]["observed_minutes"], ranked[1]["observed_minutes"])
        insight = insights(self.store)
        self.assertEqual(insight["unlabeled_places"][0]["visits"], 3)
        self.assertNotIn("Unlabeled", insight["unlabeled_places"][0]["display_name"])
        mcp = dispatch(self.store, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": "get_frequent_places", "arguments": {"limit": 10}}})
        self.assertIn("University", [item["name"] for item in mcp["result"]["structuredContent"]["result"]])

    def test_resolved_stays_at_same_provider_place_are_merged(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [
            (0, 41.0000), (16, 41.0000),
            (17, 41.0005), (20, 41.0010), (36, 41.0010),
            (37, 41.0015), (40, 41.0020), (56, 41.0020),
        ]
        self.store.ingest([{"source": "phone", "external_id": str(minute),
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "accuracy_m": 5}
                           for minute, lat in fixes])
        resolved = {"name": "Lake Example", "address": "Lake Example, Chicago",
                    "category": "natural.water", "categories": ["natural.water"],
                    "provider_place_id": "geoapify-lake-1"}
        with patch("geologs.analysis.place_for_coordinate", return_value=resolved):
            places = visited_places(self.store)
            day = day_summary(self.store, "2026-09-25", "UTC", "phone")
            pattern = insights(self.store)["frequent_places"]
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
        with patch("geologs.analysis.place_for_coordinate", side_effect=labels):
            merged, mapping = resolve_place_patterns(self.store, (clusters, {10: 0, 11: 1}))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["visits"], 2)
        self.assertEqual(mapping[10], mapping[11])

    def test_saved_place_keeps_provider_metadata(self):
        place = self.store.save_place({"name": "Walmart", "lat": 41, "lon": -87.9,
                                       "address": "100 Main Street", "category": "commercial.supermarket",
                                       "provider_place_id": "geoapify-store-1"})
        self.assertEqual(place["category"], "commercial.supermarket")
        self.assertEqual(place["provider_place_id"], "geoapify-store-1")
        self.assertEqual(self.store.places()[0]["category"], "commercial.supermarket")

    def test_late_import_rebuilds_affected_source_stays(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def put(id_, minute):
            self.store.ingest({"source": "phone", "external_id": str(id_),
                               "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                               "lat": 41, "lon": -87.9})
        put(1, 0)
        put(3, 16)
        self.assertEqual(len(stays(self.store)), 1)
        put(2, 8)
        self.assertEqual(stays(self.store)[0]["point_count"], 3)

    def test_repeated_home_assistant_samples_at_one_spot_are_one_stay_across_a_gap(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def poll(minute):
            return self.store.ingest({"source": "home_assistant:phone", "external_id": "unchanged-state",
                                      "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                                      "lat": 41, "lon": -87.9, "accuracy_m": 4})
        poll(0)
        self.assertEqual(poll(16)["recorded"], 1)
        self.assertEqual(len(self.store.points()), 1)
        self.assertEqual(len(stays(self.store)), 1)
        self.assertEqual(stays(self.store)[0]["point_count"], 2)
        poll(50)
        poll(66)
        # Still at the same spot after a quiet half hour: one stay, not two.
        rows = stays(self.store)
        self.assertEqual([(row["point_count"], row["observed_minutes"]) for row in rows], [(4, 66)])

    def test_existing_raw_samples_bootstrap_stays_when_opened(self):
        path = Path(self.tmp.name) / "bootstrap.db"
        archive = Store(path)
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        for minute in (0, 16):
            archive.ingest({"source": "home_assistant:phone", "external_id": "same-state",
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": 41, "lon": -87.9, "accuracy_m": 4}, process_analysis=False)
        self.assertEqual(len(stays(archive)), 0)
        archive.close()
        reopened = Store(path)
        try:
            self.assertEqual(len(stays(reopened)), 1)
            self.assertEqual(stays(reopened)[0]["point_count"], 2)
        finally:
            reopened.close()

    def test_stay_threshold_migration_removes_old_short_stays(self):
        path = Path(self.tmp.name) / "old-stays.db"
        archive = Store(path)
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        archive.ingest([
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": 41, "lon": -87.9, "accuracy_m": 5}
            for minute in (0, 6)
        ])
        observations = list(reversed(archive.observations(limit=None)))
        archive.db.execute("""INSERT INTO observed_stays_v2
            (source,start_observation_id,end_observation_id,started_at,ended_at,lat,lon,point_count,ongoing)
            VALUES (?,?,?,?,?,?,?,?,1)""",
            ("phone", observations[0]["id"], observations[1]["id"], observations[0]["timestamp"],
             observations[1]["timestamp"], 41, -87.9, 2))
        archive.db.execute("PRAGMA user_version=7")
        archive.db.commit()
        archive.close()
        migrated = Store(path)
        try:
            self.assertEqual(stays(migrated), [])
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 16)
        finally:
            migrated.close()

    def test_takeout_import_is_idempotent_and_does_not_trigger_reminders(self):
        place = self.store.save_place({"name": "Market", "lat": 41, "lon": -87.9})
        self.store.create_reminder({"text": "Buy salt", "place_id": place["id"]})
        content = json.dumps({"locations": [
            {"timestampMs": "1600000000000", "latitudeE7": 410000000, "longitudeE7": -879000000, "accuracy": 12},
            {"timestamp": "2020-09-13T12:30:00Z", "latitudeE7": 410000001, "longitudeE7": -879000000},
            {"timestamp": "2020-09-13T12:31:00Z"}]})
        first = import_takeout(self.store, io.StringIO(content))
        second = import_takeout(self.store, io.StringIO(content))
        self.assertEqual((first["inserted"], first["skipped"]), (2, 1))
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(self.store.events(), [])

    def test_lake_run_between_drives_is_a_running_area_stay(self):
        self.store.save_place({"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 100})
        # Home Assistant holds each position for two polls; timing a move from
        # the last poll instead of the first would make the run look like a drive.
        for fix in lake_run_day(hold=2):
            self.store.ingest(fix)
        rows = list(reversed(stays(self.store, limit=None)))
        self.assertEqual([row["kind"] for row in rows], ["stationary", "area", "stationary"])
        lake = rows[1]
        self.assertEqual(lake["activity"], "running")
        self.assertGreaterEqual(lake["observed_minutes"], 55)
        self.assertLess(metres_from(lake, LAKE), 100)
        self.assertAlmostEqual(lake["radius_m"], 500, delta=60)
        self.assertAlmostEqual(lake["distance_m"] / 1000, 9, delta=1)
        live = [(row["kind"], row["started_at"], row["ended_at"]) for row in rows]
        process_stays_rebuild(self.store)
        self.assertEqual([(row["kind"], row["started_at"], row["ended_at"]) for row in reversed(stays(self.store, limit=None))], live)

        found = {"name": "Lake Example", "address": "Lake Example, Chicago", "category": "natural.water",
                 "categories": ["natural.water"], "provider_place_id": "lake-1"}
        with patch("geologs.analysis.place_for_coordinate", return_value=found):
            day = day_summary(self.store, "2026-09-27", "UTC")
            overview = daily_overview(self.store, "2026-09-27", "UTC")
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
        self.store.ingest(minute_fixes([still(HOME, 30), walk, still(HOME, 30)]))
        self.assertEqual([row["kind"] for row in stays(self.store, limit=None)], ["stationary", "stationary"])
        trips = day_summary(self.store, "2026-09-27", "UTC")["trips"]
        self.assertEqual([(trip["mode"], trip["loop"]) for trip in trips], [("walking", True)])
        self.assertGreater(trips[0]["distance_km"], 1)

    def test_errands_driven_with_short_stops_are_not_an_area_stay(self):
        self.store.save_settings({"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
        a, b, c = shifted(HOME, 3000, 0), shifted(HOME, 3000, 1500), shifted(HOME, 1500, 1500)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, a, 6), still(a, 8), line(a, b, 4), still(b, 6),
                                        line(b, c, 4), still(c, 9), line(c, HOME, 5), still(HOME, 30)]))
        self.assertEqual([row["kind"] for row in stays(self.store, limit=None)], ["stationary", "stationary"])
        trips = day_summary(self.store, "2026-09-27", "UTC")["trips"]
        self.assertEqual([trip["mode"] for trip in trips], ["driving"])

    def test_indoor_jitter_at_a_large_store_is_one_visit(self):
        store = shifted(HOME, 6000, 2000)
        jitter = [shifted(store, 70 * math.sin(index * 2.3), 50 * math.cos(index * 1.7)) + (35,) for index in range(40)]
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, store, 10), jitter, line(store, HOME, 10), still(HOME, 30)]))
        rows = list(reversed(stays(self.store, limit=None)))
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(rows[1]["observed_minutes"], 35)
        self.assertLess(metres_from(rows[1], store), 60)

    def test_inaccurate_and_stray_fixes_do_not_split_a_stay(self):
        fixes = still(HOME, 20) + [(HOME[0] + .003, HOME[1], 300)] + still(HOME, 10) + [shifted(HOME, 80, 0) + (20,)] + still(HOME, 10)
        self.store.ingest(minute_fixes([fixes]))
        rows = stays(self.store, limit=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_minutes"], 41)

    def test_departure_confirmed_after_stray_fixes_keeps_stay_end(self):
        away = line(HOME, shifted(HOME, 3000, 0), 6)
        for fix in minute_fixes([still(HOME, 20), away]):
            self.store.ingest(fix)
        row = stays(self.store, limit=None)[0]
        self.assertEqual((row["ongoing"], row["observed_minutes"]), (0, 19))
        timeline = list(reversed(self.store.timeline(limit=None)))
        self.assertEqual([point["role"] for point in timeline[:2]], ["arrival", "departure"])

    def test_stay_split_by_gps_noise_is_reported_as_one_visit(self):
        store = shifted(HOME, 4000, 0)
        strays = [shifted(store, 80, 0), shifted(store, 85, 10), shifted(store, 80, 20)]
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, store, 6), still(store, 16), strays,
                                        still(store, 17), line(store, HOME, 6), still(HOME, 30)]))
        self.assertEqual(len(stays(self.store, limit=None)), 4)
        label = {"name": "Costco Wholesale", "address": "", "category": "commercial.supermarket",
                 "categories": ["commercial.supermarket"], "provider_place_id": "costco"}
        home = {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
        def resolve(_store, lat, lon, _radius=None):
            return label if lon > HOME[1] + .01 else home
        with patch("geologs.analysis.place_for_coordinate", side_effect=resolve):
            found = search_visits(self.store, "costco", timezone_name="UTC")
            day = day_summary(self.store, "2026-09-27", "UTC")
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
            self.store.ingest(fix)
        signature = lambda: [(row["kind"], row["activity"], row["started_at"], row["ended_at"], row["went_elsewhere"])
                             for row in stays(self.store, limit=None)]
        live = signature()
        self.assertEqual([(kind, activity) for kind, activity, *_ in reversed(live)], [("area", "walking"), ("area", "running")])
        process_stays_rebuild(self.store)
        self.assertEqual(signature(), live)

    def campus_day(self, middle, extra=None):
        """Home, drive to campus, `middle`, drive home; campus coordinates geocode to University."""
        library = CAMPUS
        fixes = minute_fixes([still(HOME, 30), line(HOME, library, 12), *middle, line(middle[-1][-1][:2], HOME, 12), still(HOME, 30)])
        self.store.ingest(fixes)
        if extra:
            self.store.ingest(extra)
        with patch("geologs.analysis.place_for_coordinate", side_effect=campus_label):
            return (day_summary(self.store, "2026-09-27", "UTC"), search_visits(self.store, "university", timezone_name="UTC"),
                    daily_overview(self.store, "2026-09-27", "UTC"))

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
        self.store.save_settings({"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
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
        self.store.ingest(before + after)
        with patch("geologs.analysis.place_for_coordinate", side_effect=campus_label):
            day = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual(day["coverage_gaps"], [])
        campus = [stay for stay in day["stays"] if metres_between(CAMPUS, stay) < 100]
        self.assertEqual([(stay["started_at"], stay["ended_at"]) for stay in campus],
                         [("2026-09-27T13:41:00+00:00", "2026-09-27T17:59:00+00:00")])

    def test_coverage_gap_between_different_places_is_still_reported(self):
        # Silent overnight and the next fix 300 km away: no way to know when you left.
        far = shifted(HOME, 300000, 0)
        self.store.ingest(minute_fixes([still(HOME, 30)]) + minute_fixes([still(far, 30)], start=datetime(2026, 9, 27, 20, tzinfo=timezone.utc)))
        day = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual(len(day["coverage_gaps"]), 1)
        home = next(stay for stay in day["stays"] if metres_between(HOME, stay) < 100)
        self.assertEqual(home["ended_at"], "2026-09-27T13:29:00+00:00")

    def test_went_elsewhere_flags_match_rebuild_when_ingested_live(self):
        taco = shifted(CAMPUS, 2500, 0)
        for fix in minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), still(CAMPUS, 60), line(CAMPUS, LAB, 7), still(LAB, 40),
                                 line(LAB, taco, 5), still(taco, 10), line(taco, LAB, 5), still(LAB, 40)]):
            self.store.ingest(fix)
        live = [(row["started_at"], row["went_elsewhere"]) for row in stays(self.store, limit=None)]
        # Home -> campus and campus -> lab go straight; lab -> taco place -> lab went elsewhere.
        # The taco place is a quick stop; flags still compare stays, as a rebuild does.
        self.assertEqual([(row["kind"], row["went_elsewhere"]) for row in reversed(stays(self.store, limit=None))],
                         [("stationary", None), ("stationary", 0), ("stationary", 0), ("stop", None), ("stationary", 1)])
        process_stays_rebuild(self.store)
        self.assertEqual([(row["started_at"], row["went_elsewhere"]) for row in stays(self.store, limit=None)], live)

    def test_place_is_named_at_the_sample_weighted_average_of_its_stays(self):
        def row(id_, east, count, radius=None):
            lat, lon = shifted(CAMPUS, east, 0)
            return {"id": id_, "source": "phone", "lat": lat, "lon": lon, "place_id": None, "radius_m": radius,
                    "activity": None, "started_at": f"2026-09-2{id_}T10:00:00+00:00", "ended_at": f"2026-09-2{id_}T11:00:00+00:00",
                    "observed_minutes": 60, "point_count": count}
        clusters, mapping = place_patterns(self.store, rows=[row(1, 0, 10), row(2, 60, 30)])
        self.assertEqual(len(clusters), 1)
        self.assertLess(metres_from(clusters[0], shifted(CAMPUS, 45, 0)), 1)
        self.assertEqual(clusters[0]["extent_m"], 45)
        self.assertNotIn("_members", clusters[0])
        with patch("geologs.analysis.place_for_coordinate", return_value={"name": "University"}) as lookup:
            resolve_place_patterns(self.store, (clusters, mapping))
        lat, lon, radius = lookup.call_args.args[1:]
        self.assertEqual(round(lat, 6), round(clusters[0]["lat"], 6))
        self.assertEqual(radius, 45)

    def test_round_trip_names_where_it_stopped(self):
        self.store.save_settings({"quick_stop_minutes": 0})  # stops this long would otherwise be quick stops
        store = shifted(HOME, 4000, 0)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, store, 8), still(store, 8), line(store, HOME, 8), still(HOME, 30)]))
        with patch("geologs.analysis.place_for_coordinate", side_effect=campus_label):
            day = day_summary(self.store, "2026-09-27", "UTC")
        trip = day["trips"][0]
        self.assertTrue(trip["loop"])
        self.assertEqual((trip["turnaround"]["name"], trip["turnaround"]["stop_minutes"]), ("Taco Place", 8))
        self.assertAlmostEqual(trip["turnaround"]["distance_m"], 4000, delta=20)
        with patch("geologs.analysis.place_for_coordinate", side_effect=campus_label):
            brief = daily_brief(daily_overview(self.store, "2026-09-27", "UTC"))
        self.assertRegex(brief["timeline"][1], r"^13:[23]\d-13:5\d drove \d\.\d km round trip from .+ via Taco Place$")

    def test_short_fast_looking_move_within_a_place_is_walking(self):
        day, _found, _overview = self.campus_day([still(CAMPUS, 90), line(CAMPUS, LAB, 1), still(LAB, 80)])
        self.assertEqual([move["mode"] for move in day["stays"][1]["internal_moves"]], ["walking"])

    def test_one_daily_summary_per_date_in_the_configured_timezone(self):
        self.store.save_settings({"timezone": "UTC"})
        self.store.ingest(minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        daily_overview(self.store, "2026-09-27")
        # An agent asking in another zone or for one source gets an answer, but no second record.
        other_zone = daily_overview(self.store, "2026-09-27", "America/Chicago")
        one_source = daily_overview(self.store, "2026-09-27", source="phone")
        self.assertEqual((other_zone["timezone"], one_source["source"]), ("America/Chicago", "phone"))
        self.assertEqual([(row["date"], row["timezone"], row["source"]) for row in self.store.daily_overviews()],
                         [("2026-09-27", "UTC", None)])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM daily_summaries").fetchone()["n"], 1)
        # Changing the timezone drops summaries stored for the old one.
        self.store.save_settings({"timezone": "America/Chicago"})
        self.assertEqual(self.store.daily_overviews(), [])
        daily_overview(self.store, "2026-09-27")
        self.assertEqual([row["timezone"] for row in self.store.daily_overviews()], ["America/Chicago"])

    def test_upgrade_removes_duplicate_daily_summaries_and_keeps_reminders(self):
        path = Path(self.tmp.name) / "before-name-reminders.db"
        archive = Store(path)
        archive.save_settings({"timezone": "UTC"})
        place = archive.save_place({"name": "Market", "lat": 41, "lon": -87.9})
        reminder = archive.create_reminder({"text": "Buy salt", "place_id": place["id"]})
        archive.ingest({"source": "phone", "timestamp": "2026-09-27T10:00:00Z", "lat": 41, "lon": -87.9})
        point_id = archive.points()[0]["id"]
        archive.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?)",
                           (reminder["id"], point_id, "2026-09-27T10:00:00+00:00"))
        for zone, source in (("UTC", ""), ("America/Chicago", ""), ("UTC", "phone")):
            archive.db.execute("""INSERT INTO daily_summaries(summary_date,timezone,source,watermark,points,distance_km,
                trip_count,place_count,reminder_count,summary,created_at,updated_at) VALUES (?,?,?,?,5,0,0,0,0,'{}',?,?)""",
                ("2026-09-27", zone, source, "w", "2026-09-28T00:00:00+00:00", "2026-09-28T00:00:00+00:00"))
        # Recreate the pre-v13 reminders table, where place_id could not be empty.
        archive.db.commit()
        archive.db.execute("PRAGMA foreign_keys=OFF")
        archive.db.execute("""CREATE TABLE reminders_legacy (id INTEGER PRIMARY KEY, text TEXT NOT NULL,
            place_id INTEGER NOT NULL REFERENCES places(id), active INTEGER NOT NULL DEFAULT 1,
            repeat INTEGER NOT NULL DEFAULT 0, cooldown_minutes INTEGER NOT NULL DEFAULT 1440,
            created_at TEXT NOT NULL, last_triggered_at TEXT, cancelled_at TEXT, destination_name TEXT,
            destination_address TEXT, destination_category TEXT, destination_provider_place_id TEXT,
            FOREIGN KEY(place_id) REFERENCES places(id))""")
        archive.db.execute("""INSERT INTO reminders_legacy SELECT id,text,place_id,active,repeat,cooldown_minutes,created_at,
            last_triggered_at,cancelled_at,destination_name,destination_address,destination_category,
            destination_provider_place_id FROM reminders""")
        archive.db.execute("DROP TABLE reminders")
        archive.db.execute("ALTER TABLE reminders_legacy RENAME TO reminders")
        archive.db.execute("PRAGMA user_version=12")
        archive.db.commit()
        archive.close()
        migrated = Store(path)
        try:
            self.assertEqual([(row["timezone"], row["source"]) for row in migrated.daily_overviews()], [("UTC", None)])
            self.assertEqual(migrated.db.execute("SELECT COUNT(*) AS n FROM daily_summaries").fetchone()["n"], 1)
            self.assertEqual([item["text"] for item in migrated.reminders()], ["Buy salt"])
            self.assertEqual(len(migrated.events()), 1)
            self.assertEqual(migrated.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            named = migrated.create_reminder({"text": "Get beans", "match_name": "Starbucks"})
            self.assertIsNone(named["place_id"])
        finally:
            migrated.close()

    def test_name_only_reminder_fires_after_a_short_stop_near_a_matching_place(self):
        reminder = self.store.create_reminder({"text": "Get coffee beans", "match_name": "starbuks"})
        self.assertEqual((reminder["place_name"], reminder["match_name"], reminder["place_id"]), ("starbuks", "starbuks", None))
        cafe = shifted(HOME, 5000, 0)
        nearby = [{"name": "Chipotle Mexican Grill", "distance_m": 12}, {"name": "Starbucks Coffee", "distance_m": 40},
                  {"name": "Starbucks Reserve", "distance_m": 400}]
        start = datetime.now(timezone.utc) - timedelta(minutes=20)
        fixes = minute_fixes([line(HOME, cafe, 10), still(cafe, 8)], start=start)
        events = []
        with patch("geologs.analysis.nearby_places", return_value=nearby) as lookup:
            for fix in fixes[:12]:  # two minutes still: not yet
                events += self.store.ingest(fix)["events"]
            self.assertEqual(events, [])
            for fix in fixes[12:]:
                events += self.store.ingest(fix)["events"]
        self.assertEqual([(event["text"], event["place"]) for event in events], [("Get coffee beans", "Starbucks Coffee")])
        self.assertEqual(lookup.call_count, 1)  # looked up once per stop, not on every poll
        self.assertEqual(self.store.reminders(), [])
        history = self.store.reminder_history()[0]
        self.assertEqual((history["status"], history["place_name"]), ("triggered", "starbuks"))
        self.assertEqual(self.store.events()[0]["place_name"], "starbuks")

    def test_name_only_reminder_ignores_other_places_and_old_data(self):
        self.store.create_reminder({"text": "Get coffee beans", "match_name": "Starbucks"})
        with patch("geologs.analysis.nearby_places", return_value=[{"name": "Dunkin'", "distance_m": 5}]):
            start = datetime.now(timezone.utc) - timedelta(minutes=10)
            events = self.store.ingest(minute_fixes([still(HOME, 6)], start=start))["events"]
        self.assertEqual(events, [])
        with patch("geologs.analysis.nearby_places", return_value=[{"name": "Starbucks", "distance_m": 5}]) as lookup:
            old = self.store.ingest(minute_fixes([still(HOME, 6)], source="old", start=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual((old["events"], lookup.call_count), ([], 0))
        self.assertEqual(len(self.store.reminders()), 1)

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
                patch("geologs.core.provider_request", side_effect=responses) as request:
            places = nearby_places(self.store, 41, -87.9, 250)
            again = nearby_places(self.store, 41.00005, -87.9, 250)
        self.assertEqual([place["name"] for place in places], ["Chipotle", "Hampton Inn"])
        self.assertEqual(places[0]["distance_m"], 11)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([place["name"] for place in again], ["Chipotle", "Hampton Inn"])
        self.assertEqual(request.call_args_list[0].args[1]["filter"], "circle:-87.9,41.0,300")

    def test_corrected_name_replaces_the_providers_guess_everywhere(self):
        cafe = shifted(HOME, 5000, 0)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, cafe, 8), still(cafe, 25), line(cafe, HOME, 8), still(HOME, 30)]))
        wrong = {"name": "Chipotle Mexican Grill", "address": "1 Strip Mall", "category": "catering.restaurant",
                 "categories": ["catering.restaurant"], "provider_place_id": "chipotle"}
        home = {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
        resolve = lambda _store, lat, lon, _radius=None: wrong if lon > HOME[1] + .01 else home
        with patch("geologs.analysis.place_for_coordinate", side_effect=resolve):
            before = day_summary(self.store, "2026-09-27", "UTC")
            self.assertEqual(before["stays"][1]["display_name"], "Chipotle Mexican Grill")
            visit = before["stays"][1]
            label = self.store.save_place_label({"name": "Starbucks", "lat": visit["lat"], "lon": visit["lon"],
                                                 "category": "catering.cafe", "provider_place_id": "starbucks"})
            after = day_summary(self.store, "2026-09-27", "UTC")
            overview = daily_overview(self.store, "2026-09-27", "UTC")
            found = search_visits(self.store, "starbucks", timezone_name="UTC")
            missed = search_visits(self.store, "chipotle", timezone_name="UTC")
        self.assertEqual(label["radius_m"], 50)
        self.assertEqual(after["stays"][1]["display_name"], "Starbucks")
        corrected = next(place for place in after["visited_places"] if place["name"] == "Starbucks")
        self.assertEqual(corrected["label_id"], label["id"])
        self.assertNotIn("Chipotle Mexican Grill", [place["name"] for place in after["visited_places"]])
        self.assertIn("Starbucks", [visit["place"] for visit in overview["visits"]])
        self.assertEqual((found["total_visits"], missed["total_visits"]), (1, 0))
        # No saved place was created.
        self.assertEqual(self.store.places(), [])
        # Renaming the same visit again replaces the correction; removing it restores the automatic name.
        again = self.store.save_place_label({"name": "Starbucks Reserve", "lat": visit["lat"] + .0001, "lon": visit["lon"]})
        self.assertEqual((again["id"], len(self.store.place_labels())), (label["id"], 1))
        self.store.delete_place_label(label["id"])
        with patch("geologs.analysis.place_for_coordinate", side_effect=resolve):
            self.assertEqual(day_summary(self.store, "2026-09-27", "UTC")["stays"][1]["display_name"], "Chipotle Mexican Grill")

    def test_visit_only_correction_leaves_other_visits_to_the_same_spot_alone(self):
        cafe = shifted(HOME, 5000, 0)
        for day in (26, 27):
            self.store.ingest(minute_fixes([still(cafe, 30)], start=datetime(2026, 9, day, 15, tzinfo=timezone.utc)))
        wrong = {"name": "Chipotle Mexican Grill", "address": "", "category": "catering.restaurant",
                 "categories": ["catering.restaurant"], "provider_place_id": "chipotle"}
        names = lambda: {stay["started_at"][:10]: stay["place_name"] for stay in stays(self.store, limit=None)}
        first = next(stay for stay in stays(self.store, limit=None) if stay["started_at"].startswith("2026-09-26"))
        only_first = self.store.save_place_label({"name": "Starbucks", "lat": first["lat"], "lon": first["lon"], "source": first["source"],
                                                  "started_at": first["started_at"], "ended_at": first["ended_at"]})
        self.assertEqual((only_first["source"], only_first["started_at"]), ("phone", first["started_at"]))
        self.assertEqual(names(), {"2026-09-26": "Starbucks", "2026-09-27": None})
        with patch("geologs.analysis.place_for_coordinate", return_value=wrong):
            self.assertEqual(day_summary(self.store, "2026-09-27", "UTC")["stays"][0]["display_name"], "Chipotle Mexican Grill")
            self.assertEqual([visit["date"] for visit in search_visits(self.store, "starbucks", timezone_name="UTC")["visits"]], ["2026-09-26"])
        # An every-visit correction names both days; the visit-only one still wins for its own visit.
        everywhere = self.store.save_place_label({"name": "Coffee Corner", "lat": first["lat"], "lon": first["lon"]})
        self.assertEqual(names(), {"2026-09-26": "Starbucks", "2026-09-27": "Coffee Corner"})
        # Correcting the same visit again replaces it; another visit gets its own.
        again = self.store.save_place_label({"name": "Starbucks Reserve", "lat": first["lat"], "lon": first["lon"],
                                             "source": "phone", "started_at": first["started_at"], "ended_at": first["ended_at"]})
        self.assertEqual(again["id"], only_first["id"])
        self.assertEqual(len(self.store.place_labels()), 2)
        self.assertNotEqual(everywhere["id"], only_first["id"])
        # Every-visit corrections only name the spot itself when there is no visit to match.
        from geologs.core import place_for_coordinate
        self.assertEqual(place_for_coordinate(self.store, first["lat"], first["lon"])["name"], "Coffee Corner")
        with self.assertRaisesRegex(ValueError, "together"):
            self.store.save_place_label({"name": "X", "lat": 41, "lon": -87.9, "started_at": first["started_at"]})

    def test_rename_without_an_address_looks_up_the_spot_s_own_address(self):
        from geologs.core import save_place_label
        house = {"name": "742 Evergreen Terrace", "place_name": "", "address": "742 Evergreen Terrace"}
        cafe_nearby = {"name": "Corner Cafe", "place_name": "Corner Cafe", "address": "10 Main St",
                       "place_lat": 41.0010, "place_lon": -87.9}
        with patch("geologs.core.reverse_geocode", return_value=house):
            friend = save_place_label(self.store, {"name": "Alex", "lat": 41, "lon": -87.9})
        with patch("geologs.core.reverse_geocode", return_value=cafe_nearby):
            other = save_place_label(self.store, {"name": "Sam", "lat": 41, "lon": -87.8})
        with patch("geologs.core.reverse_geocode", side_effect=AssertionError("looked up a given address")):
            typed = save_place_label(self.store, {"name": "Kim", "lat": 41, "lon": -87.7, "address": "1 Typed Rd"})
        with patch("geologs.core.reverse_geocode", side_effect=ValueError("no places provider")):
            offline = save_place_label(self.store, {"name": "Lee", "lat": 41, "lon": -87.6})
        self.assertEqual(friend["address"], "742 Evergreen Terrace")
        # A named place 110 m away is a neighbour, not this spot: its address is not borrowed.
        self.assertEqual(other["address"], "")
        self.assertEqual(typed["address"], "1 Typed Rd")
        self.assertEqual(offline["address"], "")

    def test_current_location_keeps_a_visit_only_correction_while_you_are_there(self):
        cafe = shifted(HOME, 5000, 0)
        start = datetime.now(timezone.utc) - timedelta(minutes=40)
        fixes = minute_fixes([still(cafe, 40)], start=start)
        self.store.ingest(fixes[:30])
        visit = stays(self.store, limit=None)[0]
        self.store.save_place_label({"name": "Starbucks", "lat": visit["lat"], "lon": visit["lon"], "source": "phone",
                                     "started_at": visit["started_at"], "ended_at": visit["ended_at"]})
        self.store.ingest(fixes[30:])  # still there, past the corrected window
        with patch("geologs.core.place_for_coordinate", return_value={"name": "Chipotle", "address": "", "category": "",
                                                                       "provider_place_id": ""}):
            self.assertEqual(current_location(self.store)["place_name"], "Starbucks")

    def test_correction_inside_a_large_saved_place_wins_only_where_it_points(self):
        self.store.save_place({"name": "Campus", "lat": CAMPUS[0], "lon": CAMPUS[1], "radius_m": 600})
        self.store.save_place_label({"name": "Ottenheimer Library", "lat": CAMPUS[0], "lon": CAMPUS[1]})
        day, _found, _overview = self.campus_day([still(CAMPUS, 60), line(CAMPUS, shifted(LAB, 0, 3000), 8),
                                                   line(shifted(LAB, 0, 3000), LAB, 8), still(LAB, 40)])
        # The saved Campus is a zone: the lab is named by the provider, and keeps the zone.
        self.assertEqual([(stay["display_name"], stay["zone"]) for stay in day["stays"]],
                         [("Home Street", None), ("Ottenheimer Library", "Campus"), ("University", "Campus"), ("Home Street", None)])
        latest = {"source": "phone", "timestamp": datetime.now(timezone.utc).isoformat(), "lat": CAMPUS[0], "lon": CAMPUS[1]}
        self.store.ingest(latest)
        self.assertEqual(current_location(self.store)["place_name"], "Ottenheimer Library")

    def buildings_lookup(self, buildings):
        """A provider that knows named buildings at their own coordinates, and only addresses elsewhere."""
        from geologs.core import metres
        def lookup(_store, lat, lon, _radius=None):
            name, spot = min(buildings.items(), key=lambda item: metres(lat, lon, *item[1]))
            return {"name": name, "named": True, "address": f"{name} address", "category": "", "categories": [],
                    "provider_place_id": name, "place_lat": spot[0], "place_lon": spot[1]}
        return lookup

    def test_saved_place_is_a_zone_holding_the_buildings_visited_inside_it(self):
        university = self.store.save_place({"name": "University", "lat": CAMPUS[0], "lon": CAMPUS[1], "radius_m": 600})
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), still(CAMPUS, 90), line(CAMPUS, LAB, 7),
                                        still(LAB, 80), line(LAB, HOME, 12), still(HOME, 30)]))
        lookup = self.buildings_lookup({"Engineering Hall": shifted(CAMPUS, 10, 5), "Library": shifted(LAB, -8, 0),
                                        "Home Street": HOME})
        with patch("geologs.analysis.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.store, "2026-09-27", "UTC")
            overview = daily_overview(self.store, "2026-09-27", "UTC")
            places = place_overview(self.store)
            found = search_visits(self.store, "university", timezone_name="UTC")
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
        self.assertEqual(self.store.place_visits(university["id"])["total_observed_minutes"], 177)

    def test_a_named_place_down_the_street_does_not_name_a_spot_inside_a_zone(self):
        house = shifted(HOME, 4000, 0)
        self.store.save_place({"name": "Mom's house", "lat": house[0], "lon": house[1], "radius_m": 150})
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, house, 10), still(house, 60), line(house, HOME, 10), still(HOME, 30)]))
        # The provider's nearest named place is a shop 120 m away: fine for a stranger's
        # street, wrong inside a zone you saved, so the zone names it.
        lookup = self.buildings_lookup({"Corner Shop": shifted(house, 120, 0), "Home Street": HOME})
        with patch("geologs.analysis.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual([stay["display_name"] for stay in day["stays"]], ["Home Street", "Mom's house", "Home Street"])

    def test_home_names_everything_inside_it(self):
        self.store.save_place({"name": "Home", "lat": HOME[0], "lon": HOME[1]})
        self.store.ingest(minute_fixes([still(HOME, 60)]))
        lookup = self.buildings_lookup({"Neighbour's Bakery": shifted(HOME, 15, 0)})
        with patch("geologs.analysis.place_for_coordinate", side_effect=lookup):
            day = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual([(stay["display_name"], stay["zone"]) for stay in day["stays"]], [("Home", "Home")])
        latest = {"source": "phone", "timestamp": datetime.now(timezone.utc).isoformat(), "lat": HOME[0], "lon": HOME[1]}
        self.store.ingest(latest)
        current = current_location(self.store)
        self.assertEqual((current["place_name"], current["zone"]), ("Home", "Home"))

    def test_deleting_a_saved_place_keeps_reminders_that_use_it(self):
        unused = self.store.save_place({"name": "Gym", "lat": 41.1, "lon": -87.9})
        market = self.store.save_place({"name": "Market", "lat": 41, "lon": -87.9})
        self.store.create_reminder({"text": "Buy salt", "place_id": market["id"]})
        self.store.delete_place(unused["id"])
        self.store.delete_place(market["id"])
        self.assertEqual(self.store.places(), [])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM places WHERE id=?", (unused["id"],)).fetchone())
        # The reminder still knows where to fire and what to call it.
        self.assertEqual([(item["text"], item["place_name"]) for item in self.store.reminders()], [("Buy salt", "Market")])
        now = datetime.now(timezone.utc)
        self.store.ingest({"source": "phone", "timestamp": (now - timedelta(minutes=5)).isoformat(), "lat": 41.01, "lon": -87.9})
        event = self.store.ingest({"source": "phone", "timestamp": now.isoformat(), "lat": 41, "lon": -87.9})["events"][0]
        self.assertEqual(event["place"], "Market")
        # The name is free to use again.
        self.assertEqual(self.store.save_place({"name": "Market", "lat": 41.2, "lon": -87.9})["name"], "Market")
        with self.assertRaisesRegex(ValueError, "saved place not found"):
            self.store.delete_place(unused["id"])

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
            self.store.ingest(minute_fixes([still(spot, hours * 60)], start=datetime(2026, 9, day, 1, tzinfo=timezone.utc)))
        with patch("geologs.analysis.place_for_coordinate", side_effect=resolve):
            places = {place["name"].split(",")[0]: place["visits"] for place in visited_places(self.store)}
        # The drifted stay joins the place where most time was spent; the rest stay apart.
        self.assertEqual(places, {"9004 Longacre Drive": 3, "9600 Longacre Drive": 1, "1200 Pine Street": 1, "Corner Cafe": 1})
        self.assertEqual(street_key("9004 Longacre Drive, Little Rock, Arkansas 72205, United States"), "longacre drive|little rock")
        self.assertIsNone(street_key("Lake Willastein Park, Maumelle, AR"))
        self.assertIsNone(street_key("9004 Longacre Drive"))

    def test_saved_home_counts_its_visits_including_gps_drift_down_the_street(self):
        house, drifted = shifted(HOME, 4000, 0), shifted(HOME, 4170, 0)             # 170 m: outside Home's circle
        neighbour, elsewhere = shifted(HOME, 3850, 0), shifted(HOME, 4000, 400)
        street = ", Little Rock, Arkansas 72205, United States"
        home = self.store.save_place({"name": "Home", "lat": house[0], "lon": house[1], "radius_m": 150, "address": "9004 Longacre Drive" + street})
        friend = self.store.save_place({"name": "Sam's", "lat": neighbour[0], "lon": neighbour[1], "radius_m": 50, "address": "8950 Longacre Drive" + street})
        def resolve(_store, lat, lon, _radius=None):
            text = ("1300 Pine Street" if lat > house[0] + .002 else "9109 Longacre Drive") + street
            return {"name": text, "named": False, "address": text, "category": "", "categories": [], "provider_place_id": ""}
        for day, spot in ((20, house), (21, drifted), (22, house), (23, neighbour), (24, elsewhere)):
            self.store.ingest(minute_fixes([still(spot, 120)], start=datetime(2026, 9, day, 1, tzinfo=timezone.utc)))
        with patch("geologs.analysis.place_for_coordinate", side_effect=resolve):
            overview = place_overview(self.store)
        self.assertEqual(overview["saved"][str(home["id"])]["visits"], 3)
        self.assertEqual(overview["saved"][str(friend["id"])]["visits"], 1)
        self.assertEqual(sorted(place["name"].split(",")[0] for place in overview["visited"]), ["1300 Pine Street", "Sam's"])

    def test_a_day_with_nothing_recorded_is_summarized_but_not_stored(self):
        self.store.save_settings({"timezone": "UTC"})
        self.store.ingest(minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        empty = daily_overview(self.store, "2026-09-28")
        self.assertEqual((empty["totals"]["raw_fixes"], daily_brief(empty)["summary"]), (0, "No location recorded."))
        with self.assertRaisesRegex(ValueError, "no summary is stored for 2026-09-28"):
            stored_daily_record(self.store, "2026-09-28")
        daily_overview(self.store, "2026-09-27")
        self.assertEqual([row["date"] for row in self.store.daily_overviews()], ["2026-09-27"])

    def test_stored_daily_record_shows_what_is_stored_and_when_it_is_out_of_date(self):
        self.store.save_settings({"timezone": "UTC"})
        self.store.ingest(minute_fixes([still(HOME, 30)], start=datetime(2026, 9, 27, 13, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(ValueError, "no summary is stored for 2026-09-27"):
            stored_daily_record(self.store, "2026-09-27")
        built = daily_overview(self.store, "2026-09-27")
        record = stored_daily_record(self.store, "2026-09-27")
        self.assertEqual((record["date"], record["timezone"], record["stale"]), ("2026-09-27", "UTC", False))
        self.assertEqual(record["summary"], built)
        self.store.ingest({"source": "phone", "timestamp": "2026-09-27T18:00:00Z", "lat": HOME[0], "lon": HOME[1]})
        self.assertTrue(stored_daily_record(self.store, "2026-09-27")["stale"])
        daily_overview(self.store, "2026-09-27")
        self.assertFalse(stored_daily_record(self.store, "2026-09-27")["stale"])

    def test_early_home_return_is_linked_to_both_daily_summaries(self):
        late = shifted(HOME, 3000, 0)
        self.store.save_place({"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 150})
        self.store.save_place({"name": "Late Cafe", "lat": late[0], "lon": late[1], "radius_m": 150})
        start = datetime(2026, 9, 27, 21, 30, tzinfo=timezone.utc)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, late, 10), still(late, 190),
                                        line(late, HOME, 10), still(HOME, 45)], start=start))
        evening = daily_overview(self.store, "2026-09-27", "UTC")
        morning = daily_overview(self.store, "2026-09-28", "UTC")
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
        self.store.save_place({"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 150})
        self.store.save_place({"name": "Late Cafe", "lat": late[0], "lon": late[1], "radius_m": 150})
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, late, 10), still(late, 130)],
                                       start=datetime(2026, 9, 27, 21, tzinfo=timezone.utc)))
        self.assertIsNone(daily_overview(self.store, "2026-09-27", "UTC")["home_return"])
        self.store.ingest(minute_fixes([line(late, HOME, 10), still(HOME, 30)],
                                       start=datetime(2026, 9, 28, 0, 10, tzinfo=timezone.utc)))
        self.assertTrue(stored_daily_record(self.store, "2026-09-27")["stale"])
        refreshed = daily_overview(self.store, "2026-09-27", "UTC")
        self.assertEqual(refreshed["home_return"]["timing"], "next_day")
        self.assertFalse(stored_daily_record(self.store, "2026-09-27")["stale"])

    def test_daily_brief_states_only_what_happened_in_plain_text(self):
        self.store.save_settings({"timezone": "America/Chicago"})
        self.store.save_place({"name": "Home", "lat": HOME[0], "lon": HOME[1]})
        lake = self.store.save_place({"name": "Lake Example", "lat": LAKE[0], "lon": LAKE[1], "radius_m": 600})
        self.store.create_reminder({"text": "Stretch first", "place_id": lake["id"]})
        self.store.create_reminder({"text": "Buy sunscreen", "match_name": "Walgreens"})
        self.store.db.execute("UPDATE reminders SET created_at='2026-09-27T12:15:00+00:00'")
        for fix in lake_run_day():
            self.store.ingest(fix)
        # Reminders fire only on live fixes; record the lake arrival as if it had.
        point = self.store.db.execute("SELECT id,timestamp FROM points ORDER BY ABS(lat-?)+ABS(lon-?) LIMIT 1", LAKE).fetchone()
        self.store.db.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (1,?,?)", (point["id"], point["timestamp"]))
        self.store.db.commit()
        overview = daily_overview(self.store, "2026-09-27")
        brief = stored_daily_record(self.store, "2026-09-27")["brief"]
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

        briefs = daily_briefs(self.store, "2026-09-27", "2026-09-28")
        self.assertEqual(briefs, [brief, {"date": "Monday, 2026-09-28 (America/Chicago)", "summary": "No location recorded."}])
        called = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "get_daily_brief", "arguments": {"date": "2026-09-27"}}})
        self.assertEqual(called["result"]["structuredContent"]["result"], [brief])
        with self.assertRaisesRegex(ValueError, "end_date must be on or up to 30 days after date"):
            daily_briefs(self.store, "2026-09-27", "2026-09-26")
        with self.assertRaisesRegex(ValueError, "end_date must be on or up to 30 days after date"):
            daily_briefs(self.store, "2026-09-01", "2026-10-02")

    def test_travel_mode_corrections_override_the_speed_guess(self):
        office = shifted(HOME, 2000, 0)
        # Crawling 2 km in 25 minutes reads as walking at these speeds.
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, office, 25), still(office, 30)]))
        day = day_summary(self.store, "2026-09-27", "UTC")
        trip = day["trips"][0]
        self.assertEqual(trip["mode"], "walking")
        # Saved a couple of minutes off, as if the trip had been recalculated since.
        self.store.save_mode_override({"source": trip["source"], "target": "trip", "mode": "driving",
                                       "started_at": (datetime.fromisoformat(trip["start"]) + timedelta(minutes=2)).isoformat(),
                                       "ended_at": (datetime.fromisoformat(trip["end"]) + timedelta(minutes=1)).isoformat()})
        corrected = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual((corrected["trips"][0]["mode"], corrected["trips"][0]["detected_mode"]), ("driving", "walking"))
        self.assertEqual((corrected["activity_totals"]["driving"]["count"], corrected["activity_totals"]["walking"]["count"]), (1, 0))
        self.assertTrue(daily_overview(self.store, "2026-09-27", "UTC")["trips"][0]["mode_corrected"])
        self.assertEqual([item["mode"] for item in self.store.trips("2026-09-27T00:00:00Z", "2026-09-28T00:00:00Z")], ["driving"])
        # Setting it again replaces the correction; "auto" restores the guess.
        self.store.save_mode_override({"source": trip["source"], "mode": "transit", "started_at": trip["start"], "ended_at": trip["end"]})
        self.assertEqual(len(self.store.mode_overrides()), 1)
        self.assertEqual(day_summary(self.store, "2026-09-27", "UTC")["activity_totals"]["transit"]["count"], 1)
        self.store.save_mode_override({"source": trip["source"], "mode": "auto", "started_at": trip["start"], "ended_at": trip["end"]})
        self.assertEqual((self.store.mode_overrides(), day_summary(self.store, "2026-09-27", "UTC")["trips"][0]["mode"]), ([], "walking"))
        with self.assertRaisesRegex(ValueError, "mode must be one of"):
            self.store.save_mode_override({"source": "phone", "mode": "skateboard", "started_at": trip["start"], "ended_at": trip["end"]})
        with self.assertRaisesRegex(ValueError, "target must be"):
            self.store.save_mode_override({"source": "phone", "target": "place", "mode": "driving", "started_at": trip["start"], "ended_at": trip["end"]})

    def test_corrected_ride_between_drives_is_not_turned_back_into_driving(self):
        lot_a, lot_b = shifted(CAMPUS, -200, -300), shifted(CAMPUS, 900, 500)
        middle = [line(CAMPUS, lot_a, 1), still(lot_a, 90), line(lot_a, lot_b, 4), still(lot_b, 80)]
        fixes = minute_fixes([still(HOME, 30), line(HOME, CAMPUS, 12), *middle, line(lot_b, HOME, 12), still(HOME, 30)])
        self.store.ingest(fixes)
        with patch("geologs.analysis.place_for_coordinate", return_value={"name": "Street"}):
            move = [trip for trip in anchored_trips_for_day(self.store) if 1 < trip["distance_km"] < 2][0]
            self.store.save_mode_override({"source": move["source"], "mode": "cycling", "started_at": move["start"], "ended_at": move["end"]})
            day = day_summary(self.store, "2026-09-27", "UTC")
        modes = [trip["mode"] for trip in day["trips"]] + [item["mode"] for stay in day["stays"] for item in stay["internal_moves"]]
        self.assertIn("cycling", modes)

    def test_area_stay_activity_can_be_corrected(self):
        self.store.ingest(lake_run_day())
        lake = next(row for row in stays(self.store, limit=None) if row["kind"] == "area")
        self.store.save_mode_override({"source": lake["source"], "target": "stay", "mode": "cycling",
                                       "started_at": lake["started_at"], "ended_at": lake["ended_at"]})
        corrected = next(row for row in stays(self.store, limit=None) if row["kind"] == "area")
        self.assertEqual((corrected["activity"], corrected["detected_activity"], corrected["activity_corrected"]), ("cycling", "running", True))
        totals = day_summary(self.store, "2026-09-27", "UTC")["activity_totals"]
        self.assertEqual((totals["cycling"]["count"], totals["running"]["count"]), (1, 0))

    def test_parked_out_and_back_run_is_one_visit(self):
        trail = shifted(HOME, 15000, 0)
        turn = shifted(trail, 0, 3000)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, trail, 15), line(trail, turn, 20),
                                        line(turn, trail, 20), line(trail, HOME, 15), still(HOME, 30)]))
        area = [row for row in stays(self.store, limit=None) if row["kind"] == "area"]
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
        brisk = {**self.store.settings(), "walking_max_kmh": 10}
        self.assertEqual(classify_motion([(150, minute, None)] * 10, brisk)["mode"], "walking")
        self.assertEqual(mode_for(2.5, None, brisk), "walking")
        # A slow drive through traffic still counts once stretches reach the driving speed.
        slow_traffic = [(300, minute, None)] * 8 + [(700, minute, None)] * 2
        self.assertEqual(classify_motion(slow_traffic)["mode"], "driving")
        self.assertEqual(classify_motion(slow_traffic, {**brisk, "driving_min_kmh": 50})["mode"], "cycling")

    def test_changing_activity_speeds_recalculates_stored_stays(self):
        self.store.ingest(lake_run_day())
        area = lambda: [stay for stay in stays(self.store, limit=None) if stay["kind"] == "area"]
        self.assertEqual(area()[0]["activity"], "running")
        before = daily_overview(self.store, "2026-09-27", "UTC")
        self.store.save_settings({"walking_max_kmh": 10})
        self.assertEqual(area()[0]["activity"], "walking")
        record = stored_daily_record(self.store, "2026-09-27")
        self.assertTrue(record["stale"])
        self.assertNotEqual(daily_overview(self.store, "2026-09-27", "UTC")["activity_totals"], before["activity_totals"])

    def test_minimum_stay_length_from_settings(self):
        from geologs.core import metres
        self.store.save_settings({"quick_stop_minutes": 0})
        cafe = shifted(HOME, 3000, 0)
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, cafe, 6), still(cafe, 9),
                                        line(cafe, HOME, 6), still(HOME, 30)]))
        near_cafe = lambda: [stay for stay in stays(self.store, limit=None) if metres(*cafe, stay["lat"], stay["lon"]) < 100]
        self.assertEqual(near_cafe(), [])
        self.store.save_settings({"stay_min_minutes": 5})
        self.assertEqual(len(near_cafe()), 1)
        self.assertIn("at least 5 minutes", search_visits(self.store, "anything", timezone_name="UTC")["note"])

    def test_analysis_settings_are_validated(self):
        for bad in ({"stay_min_minutes": 2}, {"stay_min_minutes": 7.5}, {"walking_max_kmh": 20},
                    {"cycling_max_kmh": 40}, {"driving_min_kmh": 0}, {"summary_schedule": "weekly"},
                    {"summary_interval_hours": 24}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.save_settings(bad)
        saved = self.store.save_settings({"summary_schedule": "interval", "summary_interval_hours": 2})
        self.assertEqual((saved["summary_schedule"], saved["summary_interval_hours"], saved["stay_min_minutes"]),
                         ("interval", 2, 15))

    def test_summary_schedule_saves_today_only_on_an_interval(self):
        start = datetime(2026, 9, 27, 13, tzinfo=timezone.utc)
        self.store.ingest(minute_fixes([still(HOME, 60)], start=start))
        saved_today = lambda: any(item["date"] == "2026-09-27" for item in self.store.daily_overviews())
        save_scheduled_summaries(self.store, now=start + timedelta(hours=2))
        self.assertFalse(saved_today())
        self.store.save_settings({"summary_schedule": "interval", "summary_interval_hours": 3})
        save_scheduled_summaries(self.store, now=start + timedelta(hours=2))
        self.assertTrue(saved_today())
        # Not due again until the interval has passed.
        with patch("geologs.analysis.daily_overview") as rebuild:
            save_scheduled_summaries(self.store, now=start + timedelta(hours=3))
        self.assertNotIn("2026-09-27", [call.args[1] for call in rebuild.call_args_list])

    def test_deleting_samples_previews_then_removes_and_recalculates(self):
        from geologs.analysis import delete_samples
        cafe = shifted(HOME, 5000, 0)
        place = self.store.save_place({"name": "Cafe", "lat": cafe[0], "lon": cafe[1], "radius_m": 120})
        self.store.create_reminder({"text": "Buy beans", "place_id": place["id"]})
        fixes = minute_fixes([still(HOME, 30), line(HOME, cafe, 8), still(cafe, 25), line(cafe, HOME, 8), still(HOME, 30)])
        with patch("geologs.core.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 27, 13, 40, tzinfo=timezone.utc)
            clock.fromisoformat = datetime.fromisoformat
            clock.fromtimestamp = datetime.fromtimestamp
            self.store.ingest(fixes)
        daily_overview(self.store, "2026-09-27", "UTC")
        at_cafe = [row for row in self.store.observations(limit=None)
                   if metres_between(cafe, row) < 60]
        self.assertGreaterEqual(len(at_cafe), 20)
        ids = [row["id"] for row in at_cafe]
        preview = delete_samples(self.store, ids)
        self.assertFalse(preview["deleted"])
        self.assertEqual((preview["samples"], preview["sources"], preview["dates"]), (len(ids), ["phone"], ["2026-09-27"]))
        self.assertEqual([item["reminder"] for item in preview["reminder_triggers"]], ["Buy beans"])
        self.assertEqual(self.store.observation_count(), len(fixes))  # a preview changes nothing
        result = delete_samples(self.store, ids, confirm=True)
        self.assertTrue(result["deleted"])
        self.assertEqual(self.store.observation_count(), len(fixes) - len(ids))
        self.assertEqual(self.store._rows("SELECT * FROM events"), [])
        # The cafe visit is gone from stays and from the stored summary.
        self.assertFalse([stay for stay in stays(self.store, limit=None) if metres_between(cafe, stay) < 100])
        record = stored_daily_record(self.store, "2026-09-27")
        self.assertFalse(record["stale"])
        self.assertEqual(record["summary"]["totals"]["raw_fixes"], len(fixes) - len(ids))
        with self.assertRaises(ValueError):
            delete_samples(self.store, ids, confirm=True)

    def test_deleting_every_sample_of_a_day_drops_its_summary(self):
        from geologs.analysis import delete_samples
        self.store.ingest(minute_fixes([still(HOME, 40)]))
        daily_overview(self.store, "2026-09-27", "UTC")
        ids = [row["id"] for row in self.store.observations(limit=None)]
        delete_samples(self.store, ids, confirm=True)
        self.assertEqual((self.store.daily_overviews(), self.store.latest()), ([], None))
        # The source can send again afterwards.
        self.assertEqual(self.store.ingest(minute_fixes([still(HOME, 2)]))["inserted"], 2)
        for bad in ([], ["x"], list(range(1, 502))):
            with self.assertRaises(ValueError):
                delete_samples(self.store, bad)

    def test_quick_stop_needs_stillness_and_travel_on_both_sides(self):
        fuel = shifted(HOME, 4000, 0)
        work = shifted(HOME, 9000, 0)
        jitter = [shifted(fuel, 4 * math.sin(i), 4 * math.cos(i)) for i in range(5)]
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, fuel, 5), jitter, line(fuel, work, 6), still(work, 30)]))
        kinds = lambda: [stay["kind"] for stay in reversed(stays(self.store, limit=None))]
        self.assertEqual(kinds(), ["stationary", "stop", "stationary"])
        trips = day_summary(self.store, "2026-09-27", "UTC")["trips"]
        self.assertEqual([trip["mode"] for trip in reversed(trips)], ["driving", "driving"])

    def test_sparse_fuel_stop_from_owntracks_is_kept_only_near_a_place(self):
        # Real OwnTracks fixes: the phone went quiet at the pump between 19:29:29 and 19:32:55.
        source = "owntracks:karun/iphone"
        # Only these six: the sharp fixes 200 m out as you pull away end the stop at once.
        rows = [("19:27:13", 34.75946, -92.45359, 2), ("19:28:11", 34.75837, -92.45184, 2), ("19:29:29", 34.75796, -92.45161, 16),
                ("19:32:55", 34.75773, -92.45214, 5), ("19:34:44", 34.75828, -92.44997, 3), ("19:34:58", 34.75784, -92.44775, 2)]
        self.store.ingest([{"source": source, "external_id": at, "timestamp": f"2026-10-10T{at}+00:00",
                            "lat": lat, "lon": lon, "accuracy_m": accuracy} for at, lat, lon, accuracy in rows])
        shell = {"place_name": "Shell", "place_lat": 34.7579, "place_lon": -92.4519}
        with patch("geologs.analysis.named_place_near", return_value=shell) as lookup:
            kept = stays(self.store, limit=None)
        self.assertEqual([(row["kind"], row["started_at"], row["observed_minutes"]) for row in kept],
                         [("stop", "2026-10-10T19:29:29+00:00", 3.4)])
        self.assertFalse(lookup.call_args.args[4])  # a history sweep reads only the lookup cache
        # With no place there it was probably the road or a red light, so the day offers it to keep.
        with patch("geologs.analysis.named_place_near", return_value=None):
            self.assertEqual(stays(self.store, limit=None), [])
            day = day_summary(self.store, "2026-10-10", "UTC")
        self.assertEqual([(stop["started_at"], stop["minutes"]) for stop in day["possible_stops"]],
                         [("2026-10-10T19:29:29+00:00", 3.4)])
        kept = self.store.save_stay_edit({"action": "add", **{key: day["possible_stops"][0][key] for key in ("source", "started_at", "ended_at")}})
        with patch("geologs.analysis.named_place_near", return_value=None):
            day = day_summary(self.store, "2026-10-10", "UTC")
        self.assertEqual(([stay["kind"] for stay in day["stays"]], day["possible_stops"]), (["manual"], []))
        self.store.delete_stay_edit(kept["id"])
        # A saved place there is evidence too, with no lookup at all.
        self.store.save_place({"name": "Fuel", "lat": 34.7579, "lon": -92.4519, "radius_m": 80})
        with patch("geologs.analysis.named_place_near", side_effect=AssertionError("looked up")):
            self.assertEqual(len(stays(self.store, limit=None)), 1)

    def test_leaving_after_a_quiet_stretch_ends_the_stay_near_the_departure(self):
        # Home at 13:00, the phone quiet until it reports 2.7 km away at 15:00.
        away = shifted(HOME, 2000, 0)  # line() starts one 700 m step past this
        self.store.ingest(minute_fixes([still(HOME, 3)]) + minute_fixes(
            [line(away, shifted(HOME, 9000, 0), 10), still(shifted(HOME, 9000, 0), 30)],
            start=datetime(2026, 9, 27, 15, tzinfo=timezone.utc)))
        home = list(reversed(stays(self.store, limit=None)))[0]
        self.assertEqual(home["started_at"], "2026-09-27T13:00:00+00:00")
        # 2.7 km at 30 km/h is 5 min 24 s before the fix.
        self.assertEqual(home["ended_at"], "2026-09-27T14:54:36+00:00")
        day = day_summary(self.store, "2026-09-27", "UTC")
        self.assertEqual(day["coverage_gaps"], [])

    def test_creeping_traffic_and_short_waits_are_not_quick_stops(self):
        queue = shifted(HOME, 4000, 0)
        crawl = line(queue, shifted(queue, 0, 60), 6)  # 60 m in six minutes: stop-and-go traffic
        light = still(shifted(HOME, 6000, 0), 2)  # a red light
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, queue, 5), crawl,
                                        line(crawl[-1], light[0], 3), light, line(light[0], shifted(HOME, 9000, 0), 4),
                                        still(shifted(HOME, 9000, 0), 30)]))
        self.assertEqual([stay["kind"] for stay in stays(self.store, limit=None)], ["stationary", "stationary"])

    def test_still_fixes_after_a_coverage_gap_are_not_a_quick_stop(self):
        fuel = shifted(HOME, 4000, 0)
        before = minute_fixes([still(HOME, 30), line(HOME, fuel, 5)])
        # Nothing for 20 minutes, then five still minutes and on to work.
        after = minute_fixes([still(fuel, 5), line(fuel, shifted(HOME, 9000, 0), 6), still(shifted(HOME, 9000, 0), 30)],
                             start=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc))
        self.store.ingest(before + after)
        self.assertNotIn("stop", [stay["kind"] for stay in stays(self.store, limit=None)])

    def test_a_pause_on_a_lake_loop_is_not_a_quick_stop(self):
        shore = shifted(LAKE, -500, 0)
        # 30 minutes at 9 km/h covers 4.5 km, 9 radians of a 500 m circle.
        first, second = laps(LAKE, 500, 30, 9), laps(LAKE, 500, 30, 9, start_angle=math.pi + 9)
        pause = [shifted(first[-1], 3 * math.sin(i), 3 * math.cos(i)) for i in range(5)]
        self.store.ingest(minute_fixes([still(HOME, 31), line(HOME, shore, 12), first, pause, second,
                                        line(second[-1], HOME, 12), still(HOME, 30)]))
        self.assertEqual([stay["kind"] for stay in reversed(stays(self.store, limit=None))],
                         ["stationary", "area", "stationary"])

    def test_adding_and_removing_stops_by_hand(self):
        fuel, work = shifted(HOME, 4000, 0), shifted(HOME, 9000, 0)
        self.store.save_settings({"quick_stop_minutes": 0})
        # Two minutes at the pump: too short for any detector.
        self.store.ingest(minute_fixes([still(HOME, 30), line(HOME, fuel, 5), still(fuel, 2), line(fuel, work, 6), still(work, 30)]))
        trips = lambda: day_summary(self.store, "2026-09-27", "UTC")["trips"]
        self.assertEqual(len(trips()), 1)
        overview = daily_overview(self.store, "2026-09-27", "UTC")
        added = self.store.save_stay_edit({"action": "add", "source": "phone",
                                           "started_at": "2026-09-27T13:35:00+00:00", "ended_at": "2026-09-27T13:36:00+00:00"})
        self.assertLess(metres_between(fuel, added), 5)
        rows = list(reversed(stays(self.store, limit=None)))
        self.assertEqual([(row["kind"], row["id"]) for row in rows][1], ("manual", -added["id"]))
        self.assertEqual(len(trips()), 2)
        self.assertTrue(stored_daily_record(self.store, "2026-09-27")["stale"])
        self.assertNotEqual(daily_overview(self.store, "2026-09-27", "UTC")["totals"], overview["totals"])
        # Removing the work stay; undoing both edits restores what was detected.
        work_stay = rows[2]
        removed = self.store.save_stay_edit({"action": "remove", "source": "phone",
                                             "started_at": work_stay["started_at"], "ended_at": work_stay["ended_at"]})
        self.assertEqual([row["kind"] for row in reversed(stays(self.store, limit=None))], ["stationary", "manual"])
        self.assertEqual(len(self.store.stay_edits(source="phone")), 2)
        for edit in (added, removed):
            self.store.delete_stay_edit(edit["id"])
        self.assertEqual(len(stays(self.store, limit=None)), 2)
        with self.assertRaises(ValueError):
            self.store.delete_stay_edit(added["id"])
        with self.assertRaises(ValueError):  # no samples in that window
            self.store.save_stay_edit({"action": "add", "source": "phone",
                                       "started_at": "2026-09-28T13:00:00+00:00", "ended_at": "2026-09-28T13:05:00+00:00"})
        with self.assertRaises(ValueError):
            self.store.save_settings({"quick_stop_minutes": 15})

    def test_search_visits_finds_named_places_by_word(self):
        costco, club = shifted(HOME, 5000, 0), shifted(HOME, -5000, 0)
        for day, place in ((25, costco), (26, club), (27, costco)):
            self.store.ingest(minute_fixes([still(place, 40)], start=datetime(2026, 9, day, 18, tzinfo=timezone.utc)))
        labels = {costco: {"name": "Costco Wholesale", "address": "1 Warehouse Way", "category": "commercial.supermarket",
                           "categories": ["commercial.supermarket"], "provider_place_id": "costco"},
                  club: {"name": "Sam's Club", "address": "9 Depot Road", "category": "commercial.supermarket",
                         "categories": ["commercial.supermarket"], "provider_place_id": "sams"}}
        def label(_store, lat, lon, _radius=None):
            return next(value for place, value in labels.items() if abs(place[0] - lat) + abs(place[1] - lon) < 1e-3)
        with patch("geologs.analysis.place_for_coordinate", side_effect=label):
            result = search_visits(self.store, "costco", timezone_name="America/Chicago")
            self.assertEqual([visit["date"] for visit in result["visits"]], ["2026-09-27", "2026-09-25"])
            self.assertEqual(result["visits"][0]["arrival"], "2026-09-27T13:00-05:00")
            self.assertEqual(result["visits"][0]["weekday"], "Sunday")
            self.assertEqual(result["places"][0]["name"], "Costco Wholesale")
            self.assertEqual(search_visits(self.store, "sams club")["total_visits"], 1)
            self.assertEqual(search_visits(self.store, "supermarket")["total_visits"], 3)
            self.assertEqual(search_visits(self.store, "warehouse costco")["total_visits"], 2)
            self.assertEqual(search_visits(self.store, "walmart")["visits"], [])
            response = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "search_visits", "arguments": {"query": "Costco"}}})
        self.assertEqual(response["result"]["structuredContent"]["result"]["total_visits"], 2)
        with self.assertRaisesRegex(ValueError, "query is required"):
            search_visits(self.store, "  ")

    def test_area_stay_migration_rebuilds_an_existing_archive(self):
        path = Path(self.tmp.name) / "before-area-stays.db"
        archive = Store(path)
        archive.ingest(lake_run_day())
        archive.db.execute("DELETE FROM observed_stays_v2 WHERE kind='area'")
        archive.db.execute("PRAGMA user_version=10")
        archive.db.commit()
        archive.close()
        migrated = Store(path)
        try:
            self.assertEqual([row["kind"] for row in reversed(stays(migrated, limit=None))], ["stationary", "area", "stationary"])
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 16)
        finally:
            migrated.close()


CAMPUS = shifted(HOME, 6000, 3000)
LAB = shifted(CAMPUS, 400, 0)


def campus_label(_store, lat, lon, _radius=None):
    from geologs.core import metres
    if metres(lat, lon, *CAMPUS) < 1500:
        return {"name": "University", "address": "Campus", "category": "education.university",
                "categories": ["education.university"], "provider_place_id": "university"}
    if metres(lat, lon, *HOME) < 200:
        return {"name": "Home Street", "address": "", "category": "", "categories": [], "provider_place_id": "home"}
    return {"name": "Taco Place", "address": "", "category": "catering", "categories": ["catering"], "provider_place_id": "taco"}


def anchored_trips_for_day(store):
    from geologs.analysis import anchored_trips
    return anchored_trips(store, "2026-09-27T00:00:00+00:00", "2026-09-27T23:59:59+00:00")


def metres_from(row, place):
    from geologs.core import metres
    return metres(row["lat"], row["lon"], place[0], place[1])


def process_stays_rebuild(store):
    from geologs.analysis import process_stays
    process_stays(store, rebuild=True)
    store.db.commit()


class LiveServerCase(unittest.TestCase):
    """A real server on a free port, with a scratch database and known tokens."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ingest.db")
        self.env = patch.dict(os.environ, {"GEOLOGS_DB": self.db, "GEOLOGS_TOKEN": "main-token",
                                           "GEOLOGS_INGEST_TOKEN": "phone-token"})
        self.env.start()
        for name in ("GEOLOGS_DATABASE_URL", "GEOLOGS_TOKEN_FILE", "GEOLOGS_WEBHOOK_URL"):
            os.environ.pop(name, None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
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
        store = Store(self.db)
        try:
            return store.points(limit=None)
        finally:
            store.close()



class IngestTests(LiveServerCase):
    """Tracking apps posting to a running server, each in its own format."""

    def test_owntracks_http_mode_with_basic_auth_and_queued_fixes(self):
        basic = {"Authorization": "Basic " + __import__("base64").b64encode(b"karun:phone-token").decode(),
                 "X-Limit-U": "karun", "X-Limit-D": "pixel"}
        an_hour_ago = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        store = Store(self.db)
        place = store.save_place({"name": "Market", "lat": 41.0, "lon": -87.9})
        store.create_reminder({"text": "Buy salt", "place_id": place["id"]})
        store.close()
        # A fix queued while the phone was offline arrives an hour late, with its own time.
        fix = {"_type": "location", "lat": 41.0, "lon": -87.9, "tst": an_hour_ago, "acc": 12, "vel": 36, "tid": "px",
               "created_at": an_hour_ago + 3600}
        status, reply, _ = self.send("POST", "/api/ingest/owntracks", fix, basic)
        self.assertEqual((status, reply), (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", fix, basic)[:2], (200, []))  # a retry
        [point] = self.points()
        self.assertEqual((point["source"], point["external_id"], point["accuracy_m"]), ("owntracks:karun/pixel", str(an_hour_ago), 12))
        self.assertEqual(datetime.fromisoformat(point["timestamp"]).timestamp(), an_hour_ago)
        store = Store(self.db)
        self.assertEqual(store.events(), [])  # too late to announce an arrival
        self.assertAlmostEqual(store.observations()[0]["speed_mps"], 10, places=3)  # 36 km/h
        store.close()
        # Other message types, and an empty body, are acknowledged and ignored.
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", {"_type": "transition", "event": "enter"}, basic)[:2], (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", b"", basic)[:2], (200, []))
        self.assertEqual(self.send("POST", "/api/ingest/owntracks", {"_type": "encrypted", "data": "x"}, basic)[0], 400)
        # Without X-Limit headers, the topic names the device.
        self.send("POST", "/api/ingest/owntracks", {**fix, "tst": an_hour_ago + 60, "topic": "owntracks/karun/ipad"}, {"Authorization": basic["Authorization"]})
        self.assertIn("owntracks:karun/ipad", {point["source"] for point in self.points()})
        wrong = {"Authorization": "Basic " + __import__("base64").b64encode(b"karun:nope").decode()}
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
        store = Store(self.db)
        speeds = {row["lat"]: row["speed_mps"] for row in store.observations(limit=None)}
        store.close()
        self.assertEqual({point["source"] for point in self.points()}, {"osmand:pixel"})
        self.assertAlmostEqual(speeds[41.0], 5.14444, places=4)  # 10 knots

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
                  {"Authorization": "Bearer phone-token", "X-Limit-U": "karun", "X-Limit-D": "pixel"})
        # A second phone with the wrong token is still told apart by the name it sends.
        self.send("POST", "/api/ingest/owntracks", {"_type": "location", "lat": 41, "lon": -87.9, "tst": 1790514060},
                  {"Authorization": "Bearer wrong", "X-Limit-U": "karun", "X-Limit-D": "ipad"})
        self.send("POST", "/api/ingest/overland?token=wrong", {"locations": []})
        self.send("POST", "/api/ingest/generic?token=phone-token", [{"lat": 41.001, "lon": -87.9, "device": "tasker"}, {"lat": "x"}])
        status, value, _ = self.send("GET", "/api/sources", headers=main)
        self.assertEqual((status, value["ingest_token"]), (200, True))
        devices = {(item["kind"], item["device"]): item for item in value["devices"]}
        pixel, ipad = devices[("owntracks", "owntracks:karun/pixel")], devices[("owntracks", "owntracks:karun/ipad")]
        self.assertEqual((pixel["last_status"], pixel["received"], pixel["last_message"]), (200, 1, None))
        self.assertEqual((ipad["last_status"], ipad["last_message"], ipad["last_success_at"]), (401, "Wrong or missing token", None))
        self.assertEqual((devices[("overland", "")]["last_status"]), 401)
        self.assertEqual(devices[("generic", "generic:tasker")]["last_message"], "1 malformed fix skipped")
        sources = {item["source"]: item for item in value["sources"]}
        self.assertEqual(set(sources), {"owntracks:karun/pixel", "generic:tasker"})
        self.assertEqual((sources["owntracks:karun/pixel"]["fixes"], sources["owntracks:karun/pixel"]["last_fix"]),
                         (1, "2026-09-27T13:00:00+00:00"))
        self.assertEqual(sources["generic:tasker"]["fixes_24h"], 1)
        # The sender token cannot read any of this.
        self.assertEqual(self.send("GET", "/api/sources", headers={"Authorization": "Bearer phone-token"})[0], 401)

    def test_unreachable_database_is_reported_not_a_dropped_connection(self):
        with patch("geologs.http.Store", side_effect=RuntimeError("password authentication failed for user \"geologs\"")):
            status, value, _ = self.send("GET", "/api/status", headers={"Authorization": "Bearer main-token"})
        self.assertEqual((status, value["error"]), (503, 'Database unavailable: password authentication failed for user "geologs"'))


WEB = Path(__file__).resolve().parent.parent / "src" / "geologs" / "web"


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


if __name__ == "__main__":
    unittest.main()
