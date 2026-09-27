import json
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from geotracker.core import Store, geocode, mode_for, reverse_cache_radius, reverse_geocode, route_segments_from_points, smooth_route_segment
from geotracker.analysis import day_summary, insights, place_patterns, predict_next, stays, transitions, visited_places
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
        self.assertEqual(self.store.reminders()[0]["active"], 0)

    def test_repeat_requires_reentry_and_cooldown(self):
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

    def test_stale_and_cross_source_segments_do_not_inflate_distance(self):
        self.store.ingest({"source": "one", "timestamp": "2026-09-26T10:00:00Z", "lat": 41, "lon": -87})
        self.store.ingest({"source": "two", "timestamp": "2026-09-26T10:01:00Z", "lat": 42, "lon": -88})
        self.assertEqual(self.store.stats()["distance_km"], 0)
        self.assertEqual(self.store.trips(), [])
        self.assertEqual(self.store.stats(source="one")["points"], 1)
        self.assertEqual(self.store.stats(source="missing")["points"], 0)
        self.assertEqual(len(self.store.near(41, -87, 100, source="one")), 1)
        self.assertEqual(self.store.near(41, -87, 100, source="two"), [])

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

    def test_day_stay_uses_visited_place_reverse_geocode_label(self):
        self.store.ingest([
            {"source": "phone", "timestamp": "2026-09-25T10:00:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
            {"source": "phone", "timestamp": "2026-09-25T10:06:00Z", "lat": 41, "lon": -87.9, "accuracy_m": 5},
        ])
        with patch("geotracker.analysis.address_for_coordinate", return_value="12801 South University Avenue") as reverse:
            day = day_summary(self.store, "2026-09-25", "UTC")
        self.assertEqual(day["visited_places"][0]["name"], "12801 South University Avenue")
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
        response = {"results": [{"formatted": "Nearby market", "lat": 41.01, "lon": -87.9}]}
        with patch.dict(os.environ, {"GEOAPIFY_API_KEY": "test", "GEOTRACKER_PHOTON_URL": ""}), patch("geotracker.core.provider_request", return_value=response) as request:
            self.assertEqual(geocode(self.store, "market")[0]["name"], "Nearby market")
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
            self.assertEqual(request.call_count, 1)

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
        self.assertEqual(insights(self.store)["unlabeled_places"][0]["visits"], 3)
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
