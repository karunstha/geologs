import json
import hashlib
import hmac
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from geotracker.core import Store, current_location, deliver_webhook, geocode, mode_for, reverse_cache_radius, reverse_geocode, route_segments_from_points, smooth_route_segment
from geotracker.analysis import daily_overview, day_summary, insights, place_patterns, predict_next, stays, transitions, visited_places
from geotracker.imports import import_takeout
from geotracker.mcp import dispatch
from geotracker.cli import load_env_file, poll_home_assistant
from geotracker.http import Handler, active_token


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
        self.store.save_place({"name": "Campus", "lat": 41, "lon": -87.9, "radius_m": 150})
        fixes = [
            (0, 41.01, 0), (5, 41.005, 15), (10, 41, 15),
            (15, 41, 0), (22, 41.00001, 0), (30, 41.00002, 0),
            (35, 41.001, 1.4), (40, 41.002, 1.4),
        ]
        self.store.ingest([{"source": "phone", "timestamp": (datetime(2026, 9, 25, 14, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
                           for minute, lat, speed in fixes])
        self.store.ingest({"source": "other", "timestamp": "2026-09-25T14:20:00Z", "lat": 42, "lon": -88})
        day = day_summary(self.store, "2026-09-25", "America/Chicago", "phone")
        self.assertEqual(day["points"], 8)
        self.assertEqual(day["stats"]["points"], 8)
        self.assertEqual(day["activity_counts"]["driving"], 1)
        self.assertEqual(day["activity_counts"]["walking"], 1)
        self.assertEqual(len(day["visited_places"]), 1)
        self.assertEqual(day["visited_places"][0]["name"], "Campus")
        self.assertEqual(day["stays"][0]["display_name"], "Campus")
        self.assertEqual(day["visited_places"][0]["visits"], 1)
        self.assertEqual(len(day["trips"]), 2)
        self.assertEqual(len(day["route_segments"]), 2)

    def test_day_summary_is_persisted_until_its_inputs_change(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9},
            {"source": "phone", "timestamp": "2026-09-25T10:01:00Z", "lat": 41.001, "lon": -87.9},
        ])
        first = day_summary(self.store, "2026-09-25", "UTC", "phone")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)
        with patch("geotracker.analysis.stays", side_effect=AssertionError("summary was recalculated")):
            self.assertEqual(day_summary(self.store, "2026-09-25", "UTC", "phone"), first)

        self.store.ingest({"source": "phone", "timestamp": "2026-09-25T10:02:00Z", "lat": 41.002, "lon": -87.9})
        with patch("geotracker.analysis.stays", wraps=stays) as calculate_stays:
            updated = day_summary(self.store, "2026-09-25", "UTC", "phone")
        self.assertTrue(calculate_stays.called)
        self.assertEqual(updated["points"], 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 1)

        self.store.save_place({"name": "Campus", "lat": 41, "lon": -87.9})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM analysis_cache").fetchone()["n"], 0)

    def test_day_stay_uses_visited_place_reverse_geocode_label(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:06:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        found = {"name": "University of Arkansas at Little Rock", "address": "2801 South University Avenue"}
        with patch("geotracker.analysis.place_for_coordinate", return_value=found) as reverse:
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
            for minute, lat, speed in [(0, 41, 0), (5, 41.01, 10), (10, 41.02, 10),
                                       (15, 41.021, 1.5), (20, 41.022, 1.5)]
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
            for minute, lat, speed in [(0, 41.0, 0), (1, 41.001, 4), (2, 41.002, 4),
                                       (3, 41.002, 0), (4, 41.002, 0), (5, 41.003, 14),
                                       (6, 41.004, 16)]
        ])
        day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual(len(day["trips"]), 1)
        self.assertEqual(day["trips"][0]["mode"], "driving")
        self.assertEqual(day["activity_counts"]["running_cycling"], 0)

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
        self.assertEqual(len(day["trips"]), 1)
        self.assertEqual(day["trips"][0]["mode"], "driving")
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
               "GEOTRACKER_URL": "", "GEOTRACKER_DATABASE_URL": "", "GEOTRACKER_DB": str(Path(self.tmp.name) / "poll.db")}
        with patch.dict(os.environ, env), patch("geotracker.cli.urllib.request.urlopen") as urlopen, \
                patch("geotracker.cli.datetime") as clock:
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

    def test_long_stop_separates_driving_and_walking(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        self.store.ingest([
            {"source": "phone", "timestamp": (base + timedelta(minutes=minute)).isoformat(),
             "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 10}
            for minute, lat, speed in [(0, 41.0, 0), (1, 41.001, 15), (2, 41.002, 15),
                                       (3, 41.002, 0), (4, 41.002, 0), (5, 41.002, 0),
                                       (6, 41.002, 0), (7, 41.002, 0), (8, 41.0025, 1.5),
                                       (9, 41.003, 1.5)]
        ])
        trips = list(reversed(day_summary(self.store, "2026-09-25", "UTC")["trips"]))
        self.assertEqual([trip["mode"] for trip in trips], ["driving", "walking"])

    def test_held_home_assistant_stop_splits_separate_trips(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41.000, "start", 0), (1, 41.010, "drive-a", 15), (2, 41.020, "stop", 15),
                 (3, 41.020, "held", 0), (8, 41.020, "held", 0),
                 (9, 41.030, "drive-b", 15), (10, 41.040, "finish", 15)]
        self.store.ingest([{"source": "home_assistant:phone", "external_id": external_id,
                            "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                            "lat": lat, "lon": -87.9, "speed_mps": speed, "accuracy_m": 5}
                           for minute, lat, external_id, speed in fixes])
        trips = list(reversed(day_summary(self.store, "2026-09-25", "UTC")["trips"]))
        self.assertEqual(len(trips), 2)
        self.assertEqual([trip["mode"] for trip in trips], ["driving", "driving"])

    def test_home_is_hidden_from_visited_list_but_kept_in_daily_overview(self):
        home = self.store.save_place({"name": "Home", "lat": 41, "lon": -87.9, "radius_m": 150})
        campus = self.store.save_place({"name": "Campus", "lat": 41.02, "lon": -87.9, "radius_m": 150})
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        fixes = [(0, 41), (6, 41), (7, 41.005), (8, 41.01), (10, 41.02), (16, 41.02)]
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
        self.assertEqual({place["name"] for place in summary["places"]}, {"Home", "Campus"})
        self.assertTrue(next(place for place in summary["places"] if place["name"] == "Home")["home"])
        self.assertEqual(summary["totals"]["reminders_triggered"], 1)
        self.assertEqual(len(self.store.daily_overviews()), 1)
        self.assertEqual(current_location(self.store)["place_name"], "Campus")

    def test_csv_import_and_mcp(self):
        csv = "timestamp,lat,lon,accuracy_m,speed_mps\n2026-09-26T10:00:00Z,41,-87.9,,\n"
        self.assertEqual(self.store.import_csv(csv)["inserted"], 1)
        init = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "geotracker")
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
        path.write_text("GEOTRACKER_TOKEN=file-token\n# comment\nGEOAPIFY_API_KEY='quoted-key'\n")
        with patch.dict(os.environ, {"GEOTRACKER_ENV_FILE": str(path)}, clear=True):
            load_env_file()
            self.assertEqual(os.environ["GEOTRACKER_TOKEN"], "file-token")
            self.assertEqual(os.environ["GEOAPIFY_API_KEY"], "quoted-key")
            os.environ["GEOTRACKER_TOKEN"] = "process-token"
            load_env_file()
            self.assertEqual(os.environ["GEOTRACKER_TOKEN"], "process-token")

    def test_mounted_token_file_tracks_changes(self):
        path = Path(self.tmp.name) / ".env"
        path.write_text("GEOTRACKER_TOKEN=first\n")
        with patch.dict(os.environ, {"GEOTRACKER_TOKEN_FILE": str(path), "GEOTRACKER_TOKEN": "old-env"}):
            self.assertEqual(active_token(), "first")
            path.write_text("GEOTRACKER_TOKEN=second\n")
            self.assertEqual(active_token(), "second")

    def test_optional_self_hosted_providers_and_cache(self):
        feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-87.9, 41]}, "properties": {"name": "Market", "city": "Chicago", "type": "house"}}
        with patch.dict(os.environ, {"GEOTRACKER_PHOTON_URL": "http://127.0.0.1:2322"}), patch("geotracker.core.json_get", return_value={"features": [feature]}) as request:
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
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}), patch("geotracker.core.provider_request", return_value=response) as request:
            self.assertEqual(geocode(self.store, "market")[0]["name"], "Neighborhood Market")
            self.assertEqual(geocode(self.store, "market")[0]["address"], "Neighborhood Market, 1200 Main Street")
            self.assertEqual(request.call_args.args[1]["bias"], "proximity:-87.9,41.0")
            geocode(self.store, "market")
            self.assertEqual(request.call_count, 1)
            geocode(self.store, "market", 42, -88)
            self.assertEqual(request.call_count, 2)

    def test_saved_places_reverse_geocode_once_per_cached_region(self):
        response = {"results": [{"formatted": "Market Street", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}), patch("geotracker.core.provider_request", return_value=response) as request:
            first = self.store.save_place({"name": "Shop", "lat": 41, "lon": -87.9})
            second = self.store.save_place({"name": "Office", "lat": 41.00003, "lon": -87.9})
            self.assertEqual(first["address"], "Market Street")
            self.assertEqual(second["address"], "Market Street")
            self.assertEqual(request.call_count, 2)

    def test_geoapify_place_details_wins_and_nearby_lookup_uses_cache(self):
        response = {"features": [
            {"geometry": {"type": "Polygon", "coordinates": []},
             "properties": {"feature_type": "details", "name": "War Memorial Park",
                            "formatted": "War Memorial Park, Little Rock",
                            "categories": ["leisure.park"], "place_id": "park"}},
        ]}
        env = {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geotracker.core.provider_request", return_value=response) as request:
            result = reverse_geocode(self.store, 34.749, -92.332)
            cached = reverse_geocode(self.store, 34.74903, -92.332)
        self.assertEqual(result["place_name"], "War Memorial Park")
        self.assertEqual(result["address"], "War Memorial Park, Little Rock")
        self.assertEqual(result["provider_place_id"], "park")
        self.assertEqual(result["source"], "geoapify_place_details")
        self.assertEqual(result["cache"], "provider")
        self.assertEqual(cached["cache"], "region")
        self.assertTrue(cached["approximate"])
        self.assertEqual(request.call_count, 1)
        path, params = request.call_args.args
        self.assertEqual(path, "/v2/place-details")
        self.assertEqual(params, {"lat": 34.749, "lon": -92.332, "features": "details"})

    def test_unnamed_geoapify_place_details_falls_back_to_reverse_address(self):
        details = {"features": [
            {"geometry": {"type": "Polygon", "coordinates": []},
             "properties": {"feature_type": "details", "formatted": "Unnamed nearby object"}},
        ]}
        reverse = {"results": [{"formatted": "5511 West Markham Street, Little Rock, AR",
                                  "result_type": "building"}]}
        responses = lambda path, params: details if path == "/v2/place-details" else reverse
        env = {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}
        with patch.dict(os.environ, env), patch("geotracker.core.provider_request", side_effect=responses) as request:
            result = reverse_geocode(self.store, 34.749, -92.332)
        self.assertEqual(result["name"], "5511 West Markham Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/v2/place-details", "/v1/geocode/reverse"])

    def test_geoapify_reverse_prefers_place_name_and_keeps_address(self):
        response = {"results": [{"name": "War Memorial Park",
                                  "formatted": "War Memorial Park, 5511 West Markham Street, Little Rock, AR",
                                  "address_line1": "War Memorial Park", "result_type": "amenity",
                                  "category": "leisure.park", "distance": 8}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}), patch("geotracker.core.provider_request", return_value=response) as request:
            result = reverse_geocode(self.store, 34.749, -92.332)
            nearby = reverse_geocode(self.store, 34.74903, -92.332)
        self.assertEqual(result["name"], "War Memorial Park")
        self.assertEqual(result["place_name"], "War Memorial Park")
        self.assertEqual(result["address"], "War Memorial Park, 5511 West Markham Street, Little Rock, AR")
        self.assertEqual(nearby["name"], "War Memorial Park")
        self.assertEqual(nearby["cache"], "region")
        self.assertEqual(result["source"], "geoapify_reverse")
        self.assertEqual(request.call_count, 2)

    def test_geoapify_reverse_uses_address_when_place_is_unnamed(self):
        response = {"results": [{"formatted": "1200 Main Street, Little Rock, AR", "result_type": "building"}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}), patch("geotracker.core.provider_request", return_value=response):
            result = reverse_geocode(self.store, 34.74, -92.27)
        self.assertEqual(result["name"], "1200 Main Street, Little Rock, AR")
        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["source"], "geoapify_reverse")

    def test_webhook_uses_hermes_compatible_hmac_v2_signature(self):
        event = {"id": 99, "text": "Pick up salt", "place": "Walmart"}
        env = {"GEOTRACKER_WEBHOOK_URL": "http://host.docker.internal:8644/webhooks/geotracker-reminders",
               "GEOTRACKER_WEBHOOK_SECRET": "shared-secret", "GEOTRACKER_WEBHOOK_TOKEN": ""}
        with patch.dict(os.environ, env), patch("geotracker.core.urllib.request.urlopen", return_value=io.BytesIO()) as urlopen:
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
            for label, lat, offset in (("A", 41.0, 0), ("B", 41.01, 12)):
                for minute in (0, 3, 6):
                    batch.append({"source": "phone", "external_id": f"{cycle}-{label}-{minute}",
                                  "timestamp": (base + timedelta(hours=cycle, minutes=offset + minute)).isoformat(),
                                  "lat": lat, "lon": -87.9})
        for minute in (0, 3, 6):
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
        self.assertEqual(self.store.place_visits(place["id"])["total_observed_minutes"], 24)
        ranked = visited_places(self.store)
        self.assertEqual((ranked[0]["display_name"], ranked[0]["saved"], ranked[0]["visits"]), ("University", True, 4))
        self.assertGreater(ranked[0]["observed_minutes"], ranked[1]["observed_minutes"])
        insight = insights(self.store)
        self.assertEqual(insight["unlabeled_places"][0]["visits"], 3)
        self.assertNotIn("Unlabeled", insight["unlabeled_places"][0]["display_name"])
        mcp = dispatch(self.store, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": "get_frequent_places", "arguments": {"limit": 10}}})
        self.assertEqual(mcp["result"]["structuredContent"]["result"][0]["name"], "University")

    def test_late_import_rebuilds_affected_source_stays(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def put(id_, minute):
            self.store.ingest({"source": "phone", "external_id": str(id_),
                               "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                               "lat": 41, "lon": -87.9})
        put(1, 0)
        put(3, 6)
        self.assertEqual(len(stays(self.store)), 1)
        put(2, 3)
        self.assertEqual(stays(self.store)[0]["point_count"], 3)

    def test_repeated_home_assistant_samples_create_separate_stays_across_gap(self):
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        def poll(minute):
            return self.store.ingest({"source": "home_assistant:phone", "external_id": "unchanged-state",
                                      "timestamp": (base + timedelta(minutes=minute)).isoformat(),
                                      "lat": 41, "lon": -87.9, "accuracy_m": 4})
        poll(0)
        self.assertEqual(poll(6)["recorded"], 1)
        self.assertEqual(len(self.store.points()), 1)
        self.assertEqual(len(stays(self.store)), 1)
        self.assertEqual(stays(self.store)[0]["point_count"], 2)
        poll(50)
        poll(56)
        rows = stays(self.store)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["point_count"] for row in rows], [2, 2])

    def test_existing_raw_samples_bootstrap_stays_when_opened(self):
        path = Path(self.tmp.name) / "bootstrap.db"
        archive = Store(path)
        base = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
        for minute in (0, 6):
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


if __name__ == "__main__":
    unittest.main()
