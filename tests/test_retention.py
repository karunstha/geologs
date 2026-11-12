"""Data retention: old raw samples are thinned, and the days they recorded freeze as they were found."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from geologs.domain.retention import freeze_target, next_freeze_line, without_live_doubles, years_ago
from geologs.services import open_database
from geologs.services.backup import export_backup
from geologs.services.corrections import save_mode_override, save_stay_edit
from geologs.services.day import day_summary
from geologs.services.imports import import_file
from geologs.services.ingest import ingest
from geologs.services.retention import retention_status, run_retention, set_retention
from geologs.services.saved_places import save_label
from geologs.services.stays import rebuild_stays
from scratch_db import scratch_database
from test_geologs import HOME, line, minute_fixes, shifted, still

OFFICE = shifted(HOME, 4000, 0)
FROZEN_DAY = datetime(2026, 9, 27, 13, tzinfo=timezone.utc)
LIVE_DAY = datetime(2026, 9, 29, 13, tzinfo=timezone.utc)
# A year after the frozen day, retention keeping one year freezes the days before 2026-09-29.
NOW = datetime(2027, 9, 29, 12, tzinfo=timezone.utc)


def commute(start):
    """Home, a drive to the office, a day there, and the drive back."""
    return minute_fixes([still(HOME, 40), line(HOME, OFFICE, 12), still(OFFICE, 60), line(OFFICE, HOME, 12),
                         still(HOME, 40)], start=start)


def picture(day):
    """What a day shows, in the terms a frozen day must keep."""
    return ([(stay["started_at"], stay["ended_at"], stay["kind"]) for stay in day["stays"]],
            [(trip["start"], trip["end"], trip["mode"], trip["distance_km"]) for trip in day["trips"]])


class RetentionRuleTests(unittest.TestCase):
    def test_the_line_sits_at_local_midnight_years_ago(self):
        zone = ZoneInfo("America/Chicago")
        self.assertEqual(freeze_target(NOW, 1, zone), "2026-09-29T05:00:00+00:00")
        self.assertIsNone(freeze_target(NOW, 0, zone))
        self.assertEqual(years_ago(datetime(2028, 2, 29).date(), 1).isoformat(), "2027-02-28")

    def test_the_line_moves_a_batch_at_a_time_and_never_back(self):
        zone = ZoneInfo("UTC")
        target = "2026-09-29T00:00:00+00:00"
        self.assertEqual(next_freeze_line(None, target, "2025-01-10T08:00:00+00:00", zone), "2025-02-10T00:00:00+00:00")
        self.assertEqual(next_freeze_line("2026-09-20T00:00:00+00:00", target, None, zone), target)
        self.assertIsNone(next_freeze_line(target, target, None, zone))
        self.assertIsNone(next_freeze_line("2026-10-01T00:00:00+00:00", target, None, zone))
        self.assertIsNone(next_freeze_line(None, target, "2026-09-30T00:00:00+00:00", zone))

    def test_a_frozen_stay_wins_over_the_live_one_found_again_from_the_line(self):
        night = {"source": "phone", "started_at": "2026-09-28T20:00:00+00:00", "ended_at": "2026-09-29T07:00:00+00:00",
                 "frozen": True}
        again = {"source": "phone", "started_at": "2026-09-29T00:00:10+00:00", "ended_at": "2026-09-29T07:00:00+00:00"}
        before = {"source": "phone", "started_at": "2026-09-28T10:00:00+00:00", "ended_at": "2026-09-28T11:00:00+00:00"}
        later = {"source": "phone", "started_at": "2026-09-29T09:00:00+00:00", "ended_at": "2026-09-29T10:00:00+00:00"}
        other = {**again, "source": "watch"}
        kept = without_live_doubles([night, again, before, later, other], "2026-09-29T00:00:00+00:00")
        self.assertEqual(kept, [night, later, other])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = open_database(scratch_database(self))
        self.addCleanup(lambda: self.db.close())
        ingest(self.db, commute(FROZEN_DAY))
        ingest(self.db, commute(LIVE_DAY))

    def freeze(self):
        """Keep one year, confirmed, and let the background job run until it has nothing left to do."""
        preview = set_retention(self.db, {"years": 1}, NOW)
        self.assertFalse(preview["saved"])
        self.assertGreater(preview["preview"]["samples_removed"], 0)
        self.assertEqual(preview["preview"]["freeze_date"], "2026-09-29")
        self.assertTrue(set_retention(self.db, {"years": 1, "confirm": True}, NOW)["saved"])
        runs = 0
        while run_retention(self.db, NOW):
            runs += 1
        self.assertEqual(runs, 1)

    def test_a_frozen_day_shows_what_was_found_with_far_fewer_samples(self):
        before = picture(day_summary(self.db, "2026-09-27", "UTC"))
        live_before = picture(day_summary(self.db, "2026-09-29", "UTC"))
        samples = len(self.db.points.observations("2026-09-27T00:00:00Z", "2026-09-28T00:00:00Z", limit=None))
        self.assertEqual(len(before[1]), 2)  # there and back
        self.freeze()
        self.assertEqual(retention_status(self.db)["frozen_before_date"], "2026-09-29")
        kept = len(self.db.points.observations("2026-09-27T00:00:00Z", "2026-09-28T00:00:00Z", limit=None))
        self.assertLess(kept, samples / 2)
        frozen = day_summary(self.db, "2026-09-27", "UTC")
        self.assertTrue(frozen["frozen"])
        self.assertTrue(all(trip["frozen"] for trip in frozen["trips"]))
        self.assertEqual(picture(frozen), before)
        live = day_summary(self.db, "2026-09-29", "UTC")
        self.assertFalse(live["frozen"])
        self.assertEqual(picture(live), live_before)

    def test_a_rebuild_and_a_late_fix_leave_a_frozen_day_alone(self):
        before = picture(day_summary(self.db, "2026-09-27", "UTC"))
        live_before = picture(day_summary(self.db, "2026-09-29", "UTC"))
        self.freeze()
        rebuild_stays(self.db)
        self.assertEqual(picture(day_summary(self.db, "2026-09-27", "UTC")), before)
        self.assertEqual(picture(day_summary(self.db, "2026-09-29", "UTC")), live_before)
        # A phone sends a fix it kept offline, from the frozen day's office hours.
        ingest(self.db, [{"source": "phone", "external_id": "late", "lat": OFFICE[0] + .01, "lon": OFFICE[1],
                          "timestamp": (FROZEN_DAY + timedelta(minutes=80)).isoformat(), "accuracy_m": 8}])
        self.assertEqual(picture(day_summary(self.db, "2026-09-27", "UTC")), before)
        self.assertEqual(picture(day_summary(self.db, "2026-09-29", "UTC")), live_before)

    def test_a_frozen_day_takes_no_edits(self):
        self.freeze()
        trip = day_summary(self.db, "2026-09-27", "UTC")["trips"][0]
        window = {"source": "phone", "started_at": trip["start"], "ended_at": trip["end"]}
        with self.assertRaisesRegex(ValueError, "frozen"):
            save_mode_override(self.db, {**window, "mode": "cycling"})
        with self.assertRaisesRegex(ValueError, "frozen"):
            save_stay_edit(self.db, {**window, "action": "add"})
        with self.assertRaisesRegex(ValueError, "frozen"):
            save_label(self.db, {"name": "Cafe", "lat": OFFICE[0], "lon": OFFICE[1], **window})
        # A name for every visit is not an edit of one day: it names the frozen day too.
        save_label(self.db, {"name": "Office", "lat": OFFICE[0], "lon": OFFICE[1], "radius_m": 100})
        names = [trip["to_name"] for trip in day_summary(self.db, "2026-09-27", "UTC")["trips"]]
        self.assertIn("Office", names)
        live = day_summary(self.db, "2026-09-29", "UTC")["trips"][0]
        save_mode_override(self.db, {"source": "phone", "started_at": live["start"], "ended_at": live["end"], "mode": "cycling"})

    def test_keeping_more_years_or_turning_it_off_needs_no_confirmation_and_never_unfreezes(self):
        self.freeze()
        self.assertTrue(set_retention(self.db, {"years": 5}, NOW)["saved"])
        self.assertTrue(set_retention(self.db, {"years": 0}, NOW)["saved"])
        self.assertIsNone(run_retention(self.db, NOW))
        status = retention_status(self.db)
        self.assertEqual((status["years"], status["frozen_before_date"]), (0, "2026-09-29"))
        with self.assertRaisesRegex(ValueError, "years must be one of"):
            set_retention(self.db, {"years": 4}, NOW)

    def test_a_backup_carries_frozen_days(self):
        before = picture(day_summary(self.db, "2026-09-27", "UTC"))
        self.freeze()
        path = Path(self.tmp.name) / "backup.json.gz"
        with open(path, "wb") as out:
            export_backup(self.db, out)
        other = open_database(scratch_database(self))
        self.addCleanup(other.close)
        result = import_file(other, str(path), "ignored")
        self.assertEqual(result["restored"]["trips"], 2)
        self.assertGreater(result["restored"]["frozen_stays"], 0)
        restored = day_summary(other, "2026-09-27", "UTC")
        self.assertTrue(restored["frozen"])
        self.assertEqual(picture(restored), before)


if __name__ == "__main__":
    unittest.main()
