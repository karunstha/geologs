"""Flights: told from the stays at either end, not from fixes nobody recorded in the air."""
import unittest

from geologs.domain.geo import great_circle, metres
from geologs.domain.motion import is_flight
from geologs.domain.paths import learnable, path_length, with_flight_way
from geologs.domain.place_routes import same_kind, track_from_trip
from geologs.domain.places import is_airport_place
from geologs.domain.trips import trips_between_stays

CHICAGO = (41.98, -87.90)
NEW_YORK = (40.64, -73.78)
MILWAUKEE = (42.95, -87.90)


def stay(stay_id, place, started_at, ended_at, source="phone"):
    return {"id": stay_id, "source": source, "lat": place[0], "lon": place[1],
            "started_at": started_at, "ended_at": ended_at}


def fix(place, timestamp, point_id, source="phone"):
    return {"id": point_id, "source": source, "lat": place[0], "lon": place[1], "timestamp": timestamp,
            "accuracy_m": 10, "speed_mps": None, "speed_origin": None, "external_id": None}


class FlightTests(unittest.TestCase):
    def test_far_and_faster_than_the_ground_is_a_flight(self):
        hour = 3600
        self.assertTrue(is_flight(1_160_000, 2.5 * hour, None))  # Chicago to New York, door to door
        self.assertFalse(is_flight(1_160_000, 14 * hour, None))  # the same way by road
        self.assertFalse(is_flight(90_000, .25 * hour, None))   # too short to be a flight, whatever the clock says

    def test_an_airport_at_either_end_lowers_the_bar(self):
        # 300 km in 1 h 45 m with the drives to and from the airports: 171 km/h.
        asked = []
        self.assertFalse(is_flight(300_000, 6300, lambda: asked.append(1) or False))
        self.assertTrue(is_flight(300_000, 6300, lambda: True))
        self.assertFalse(is_flight(300_000, 6300, None))
        # A drive is too slow to need the check at all.
        is_flight(300_000, 4 * 3600, lambda: asked.append(2) or True)
        self.assertEqual(asked, [1])

    def test_airports_are_known_by_name_or_category(self):
        self.assertTrue(is_airport_place({"name": "O'Hare International Airport"}))
        self.assertTrue(is_airport_place({"name": "Terminal 2", "category": "aeroway.terminal"}))
        self.assertTrue(is_airport_place({"place_name": "Flughafen Wien", "category": ""}))
        self.assertTrue(is_airport_place({"name": "CDG", "category": "airport.international"}))
        self.assertFalse(is_airport_place({"name": "Airporter Hotel Shuttle Stop"}))
        self.assertFalse(is_airport_place({"name": "Starbucks", "category": "catering.cafe"}))
        self.assertFalse(is_airport_place(None))

    def test_trips_between_far_stays_are_flights(self):
        stays = [stay(1, CHICAGO, "2026-09-27T10:00:00+00:00", "2026-09-27T12:00:00+00:00"),
                 stay(2, NEW_YORK, "2026-09-27T14:30:00+00:00", "2026-09-27T16:00:00+00:00")]
        points = [fix(CHICAGO, "2026-09-27T11:59:00+00:00", 1), fix(NEW_YORK, "2026-09-27T14:31:00+00:00", 2)]
        (trip,) = trips_between_stays(points, stays)
        self.assertEqual(trip["mode"], "flying")
        self.assertTrue(trip["recorded"])
        self.assertAlmostEqual(trip["distance_km"], metres(*CHICAGO, *NEW_YORK) / 1000, places=1)
        self.assertNotIn("fixes", trip)  # nothing for a road route to match
        self.assertFalse(trip["coverage_complete"])

    def test_a_short_airport_hop_needs_the_airport(self):
        stays = [stay(1, CHICAGO, "2026-09-27T10:00:00+00:00", "2026-09-27T12:00:00+00:00"),
                 stay(2, MILWAUKEE, "2026-09-27T12:40:00+00:00", "2026-09-27T16:00:00+00:00")]
        # 108 km in 40 minutes: 162 km/h, fast for a car but no proof of a plane.
        (trip,) = trips_between_stays([], stays)
        self.assertIsNone(trip["mode"])
        (trip,) = trips_between_stays([], stays, near_airport=lambda row: row["id"] == 1)
        self.assertEqual(trip["mode"], "flying")

    def test_a_flight_is_drawn_over_the_globe_and_never_learned(self):
        trip = {"mode": "flying", "from_lat": CHICAGO[0], "from_lon": CHICAGO[1], "to_lat": NEW_YORK[0],
                "to_lon": NEW_YORK[1], "distance_km": 0, "loop": False, "recorded_points": 400, "max_fix_gap_m": 0,
                "coverage_complete": True,
                "route": [{"kind": "recorded", "coordinates": [[CHICAGO[1], CHICAGO[0]], [NEW_YORK[1], NEW_YORK[0]]]}]}
        drawn = with_flight_way(trip)
        (leg,) = drawn["route"]
        self.assertTrue(leg["flight"])
        self.assertEqual(leg["coordinates"][0], [CHICAGO[1], CHICAGO[0]])
        # A great circle from Chicago to New York bows north of the straight line on the map.
        middle = leg["coordinates"][len(leg["coordinates"]) // 2]
        self.assertGreater(middle[1], (CHICAGO[0] + NEW_YORK[0]) / 2)
        self.assertAlmostEqual(drawn["distance_km"], metres(*CHICAGO, *NEW_YORK) / 1000, delta=1)
        self.assertFalse(learnable(trip))
        self.assertIsNone(track_from_trip(trip))
        self.assertFalse(same_kind("flying", "driving"))
        self.assertFalse(same_kind("flying", None))
        self.assertTrue(same_kind("flying", "flying"))
        self.assertTrue(same_kind("driving", None))

    def test_an_arc_across_the_antimeridian_goes_the_short_way(self):
        tokyo, honolulu = [139.78, 35.55], [-157.92, 21.32]
        way = great_circle(tokyo, honolulu)
        self.assertTrue(all(abs(b[0] - a[0]) < 10 for a, b in zip(way, way[1:])))
        self.assertAlmostEqual(path_length(way), metres(tokyo[1], tokyo[0], honolulu[1], honolulu[0]), delta=1000)
        self.assertEqual(way[0], tokyo)
        self.assertAlmostEqual((way[-1][0] - honolulu[0]) % 360, 0, places=6)


if __name__ == "__main__":
    unittest.main()
