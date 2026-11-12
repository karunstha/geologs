"""Live location sharing: a viewer sees the sharer's position now, at the precision shared, and nothing else."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from geologs.services import accounts, open_database
from geologs.services.ingest import ingest
from geologs.services.saved_places import save_label
from geologs.services.sharing import leave_share, refresh_live_position, share_location, sharing_status, stop_sharing
from scratch_db import scratch_database
from test_geologs import HOME, minute_fixes, still

PASSWORD = "correct horse battery"


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.db = open_database(scratch_database(self))
        self.addCleanup(lambda: self.db.close())
        self.alice = accounts.add_user(self.db, "alice@example.com", PASSWORD, name="Alice")["id"]
        self.bob = accounts.add_user(self.db, "bob@example.com", PASSWORD, name="Bob")["id"]
        self.carol = accounts.add_user(self.db, "carol@example.com", PASSWORD, name="Carol")["id"]
        self.db.as_user(self.alice)
        # Alice has been home for the last 40 minutes, and calls it Home.
        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=40)
        ingest(self.db, minute_fixes([still(HOME, 40)], start=start))
        save_label(self.db, {"name": "Home", "lat": HOME[0], "lon": HOME[1], "radius_m": 100})
        town = patch("geologs.services.sharing.ambient.place_of", return_value={"city": "Elmhurst"})
        town.start()
        self.addCleanup(town.stop)

    def as_user(self, user_id):
        self.db.as_user(user_id)
        return sharing_status(self.db)

    def test_a_viewer_sees_where_you_are_and_nothing_of_your_archive(self):
        given = share_location(self.db, {"email": "BOB@example.com", "precision": "exact", "duration": "always"})["given"]
        self.assertEqual([(share["viewer"]["name"], share["precision"], share["expires_at"]) for share in given],
                         [("Bob", "exact", None)])
        (received,) = self.as_user(self.bob)["received"]
        self.assertEqual(received["sharer"]["name"], "Alice")
        position = received["position"]
        self.assertEqual((position["lat"], position["lon"], position["place_name"], position["moving"]),
                         (HOME[0], HOME[1], "Home", False))
        # Bob reads Alice's shared position and nothing else of hers.
        self.assertEqual(self.db.points.observations(limit=None), [])
        self.assertEqual(self.db.stays.between(None, None, None, None), [])
        self.assertEqual(self.db.sharing.given(), [])
        # Carol, shared with by no one, sees nothing, not even at the database.
        self.assertEqual(self.as_user(self.carol)["received"], [])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM live_positions").fetchone()["n"], 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM location_shares").fetchone()["n"], 0)

    def test_an_approximate_share_shows_only_the_town(self):
        share_location(self.db, {"email": "bob@example.com", "precision": "exact"})
        share_location(self.db, {"email": "carol@example.com", "precision": "approximate"})
        (received,) = self.as_user(self.carol)["received"]
        position = received["position"]
        self.assertEqual((position["lat"], position["lon"], position["place_name"]),
                         (round(HOME[0], 2), round(HOME[1], 2), "Elmhurst"))
        # The exact position is kept for Bob, but the database never gives it to Carol.
        precisions = [row["precision"] for row in self.db.conn.execute("SELECT precision FROM live_positions").fetchall()]
        self.assertEqual(precisions, ["approximate"])

    def test_a_share_ends_on_time_when_stopped_or_left(self):
        an_hour_and_more_ago = datetime.now(timezone.utc) - timedelta(minutes=61)
        share_location(self.db, {"email": "bob@example.com", "duration": "hour"}, now=an_hour_and_more_ago)
        self.assertEqual(self.as_user(self.bob)["received"], [])
        self.db.as_user(self.alice)
        self.assertEqual(sharing_status(self.db)["given"], [])  # an ended share goes
        refresh_live_position(self.db)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM live_positions").fetchone()["n"], 0)

        today = share_location(self.db, {"email": "bob@example.com", "duration": "today"})["given"][0]
        self.assertGreater(datetime.fromisoformat(today["expires_at"]), datetime.now(timezone.utc))
        stop_sharing(self.db, today["id"])
        self.assertEqual(self.as_user(self.bob)["received"], [])
        self.db.as_user(self.alice)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) AS n FROM live_positions").fetchone()["n"], 0)

        share = share_location(self.db, {"email": "bob@example.com"})["given"][0]
        with self.assertRaisesRegex(ValueError, "already stopped"):
            leave_share(self.db, share["id"])  # Alice cannot leave her own share
        (received,) = self.as_user(self.bob)["received"]
        with self.assertRaisesRegex(ValueError, "already stopped"):
            stop_sharing(self.db, received["id"])  # nor Bob stop it for her
        self.assertEqual(leave_share(self.db, received["id"])["received"], [])
        self.db.as_user(self.alice)
        self.assertEqual(sharing_status(self.db)["given"], [])

    def test_sharing_needs_another_account_here(self):
        for email, message in (("nobody@example.com", "No one on this server"), ("alice@example.com", "your own account"),
                               ("", "No one on this server")):
            with self.assertRaisesRegex(ValueError, message):
                share_location(self.db, {"email": email})
        with self.assertRaisesRegex(ValueError, "precision"):
            share_location(self.db, {"email": "bob@example.com", "precision": "fuzzy"})
        with self.assertRaisesRegex(ValueError, "duration"):
            share_location(self.db, {"email": "bob@example.com", "duration": "week"})

    def test_erasing_your_locations_stops_your_shares(self):
        share_location(self.db, {"email": "bob@example.com"})
        self.db.points.erase_all()
        self.assertEqual(sharing_status(self.db)["given"], [])
        self.assertEqual(self.as_user(self.bob)["received"], [])


if __name__ == "__main__":
    unittest.main()
