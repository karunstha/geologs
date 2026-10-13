"""Reminders and the events that fired them."""
from __future__ import annotations

from ..domain.timing import parse_time
from .repository import Repository

# What a reminder reads as; legacy columns (repeat, cooldown_minutes) stay inside.
REMINDER = ("r.id,r.text,r.place_id,r.active,r.created_at,r.last_triggered_at,r.cancelled_at,r.destination_name,"
            "r.destination_address,r.destination_category,r.destination_provider_place_id,r.match_name")


class RemindersRepository(Repository):
    def active(self):
        """Active one-shot reminders waiting for their next arrival."""
        return self.rows(f"""SELECT {REMINDER},COALESCE(r.destination_name,p.name) AS place_name,
            p.lat AS place_lat,p.lon AS place_lon,p.radius_m,
            COALESCE(r.destination_address,p.address) AS place_address,
            COALESCE(r.destination_category,p.category) AS category
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.active=1 ORDER BY r.created_at DESC,r.id DESC""")

    def get_active(self, reminder_id):
        return next((item for item in self.active() if item["id"] == reminder_id), None)

    def history(self, limit=200):
        return self.rows(f"""SELECT {REMINDER},COALESCE(r.destination_name,p.name) AS place_name,
            COALESCE(r.destination_category,p.category) AS category,e.id AS event_id,e.triggered_at,e.webhook_status,
            CASE WHEN r.last_triggered_at IS NULL THEN 'cancelled' ELSE 'triggered' END AS status
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            LEFT JOIN events e ON e.id=(SELECT MAX(latest.id) FROM events latest WHERE latest.reminder_id=r.id)
            WHERE r.active=0 ORDER BY COALESCE(r.last_triggered_at,r.cancelled_at,r.created_at) DESC,r.id DESC LIMIT ?""",
            (min(max(int(limit), 1), 1000),))

    def add(self, text, place_id, created_at, destination, match_name=None):
        return self.execute("""INSERT INTO reminders
            (text,place_id,repeat,cooldown_minutes,created_at,destination_name,destination_address,
             destination_category,destination_provider_place_id,match_name)
            VALUES (?,?,0,1440,?,?,?,?,?,?) RETURNING id""",
            (text, place_id, created_at, destination["name"], destination["address"],
             destination["category"], destination["provider_place_id"], match_name)).fetchone()["id"]

    def cancel(self, reminder_id, at):
        """False when no active reminder has that id."""
        return bool(self.execute("UPDATE reminders SET active=0,cancelled_at=? WHERE id=? AND active=1",
                                 (at, int(reminder_id))).rowcount)

    def geofenced(self):
        """Active reminders that wait for arrival at a place, with its position and radius."""
        return self.rows(f"""SELECT {REMINDER}, p.lat AS place_lat, p.lon AS place_lon, p.radius_m,
            COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r JOIN places p ON p.id=r.place_id WHERE r.active=1""")

    def name_only(self):
        """Active reminders that wait for a stop near any place with a matching name."""
        return self.rows("SELECT id,text,match_name FROM reminders WHERE active=1 AND match_name IS NOT NULL")

    def trigger(self, reminder_id, at, only_active=False):
        """Mark a reminder fired; with only_active, False when another source fired it first."""
        query = "UPDATE reminders SET last_triggered_at=?,cancelled_at=NULL,active=0,repeat=0 WHERE id=?"
        return bool(self.execute(query + (" AND active=1" if only_active else ""), (at, reminder_id)).rowcount)

    def record_event(self, reminder_id, point_id, triggered_at):
        return self.execute("INSERT INTO events(reminder_id,point_id,triggered_at) VALUES (?,?,?) RETURNING id",
                            (reminder_id, point_id, triggered_at)).fetchone()["id"]

    def set_webhook_status(self, event_id, status):
        self.execute("UPDATE events SET webhook_status=? WHERE id=?", (status, event_id))

    def events(self, since_id=0, limit=100):
        return self.rows("""SELECT e.*,r.text,COALESCE(r.destination_name,p.name) AS place_name FROM events e
            JOIN reminders r ON r.id=e.reminder_id LEFT JOIN places p ON p.id=r.place_id
            WHERE e.id>? ORDER BY e.id DESC LIMIT ?""", (int(since_id), min(int(limit), 500)))

    def events_between(self, start, end, source=None):
        clauses = ["e.triggered_at>=?", "e.triggered_at<=?"]
        args = [parse_time(start), parse_time(end)]
        if source:
            clauses.append("pnt.source=?")
            args.append(source)
        return self.rows("""SELECT e.id,e.reminder_id,e.triggered_at,e.webhook_status,
            r.text,COALESCE(r.destination_name,p.name) AS place_name,r.match_name FROM events e JOIN reminders r ON r.id=e.reminder_id
            LEFT JOIN places p ON p.id=r.place_id JOIN points pnt ON pnt.id=e.point_id WHERE """
            + " AND ".join(clauses) + " ORDER BY e.triggered_at", args)

    def created_between(self, start, end):
        return self.rows("""SELECT r.id,r.text,r.created_at,r.active,r.last_triggered_at,
            COALESCE(r.destination_name,p.name) AS place_name,r.match_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.created_at>=? AND r.created_at<=? ORDER BY r.created_at""",
            (parse_time(start), parse_time(end)))

    def cancelled_between(self, start, end):
        return self.rows("""SELECT r.id,r.text,r.cancelled_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id
            WHERE r.cancelled_at>=? AND r.cancelled_at<=? ORDER BY r.cancelled_at""",
            (parse_time(start), parse_time(end)))

    def active_at(self, timestamp):
        at = parse_time(timestamp)
        return self.rows("""SELECT r.id,r.text,r.created_at,COALESCE(r.destination_name,p.name) AS place_name
            FROM reminders r LEFT JOIN places p ON p.id=r.place_id WHERE r.created_at<=?
            AND (r.last_triggered_at IS NULL OR r.last_triggered_at>?)
            AND (r.cancelled_at IS NULL OR r.cancelled_at>?) ORDER BY r.created_at""", (at, at, at))

    def triggers_of_points(self, point_ids):
        """Reminders that the given points fired."""
        if not point_ids:
            return []
        return self.rows(f"""SELECT e.id,e.triggered_at,r.text AS reminder FROM events e
            JOIN reminders r ON r.id=e.reminder_id WHERE e.point_id IN ({",".join("?" * len(point_ids))})
            ORDER BY e.triggered_at""", point_ids)

    def triggers_of_source(self, source):
        """Reminders that a tracker's points fired."""
        return self.rows("""SELECT e.triggered_at,r.text AS reminder FROM events e JOIN reminders r ON r.id=e.reminder_id
            JOIN points p ON p.id=e.point_id WHERE p.source=? ORDER BY e.triggered_at""", (source,))

    def delete_events_of_points(self, point_ids):
        self.execute(f"DELETE FROM events WHERE point_id IN ({','.join('?' * len(point_ids))})", point_ids)

    def delete_events_of_source(self, source):
        self.execute("DELETE FROM events WHERE point_id IN (SELECT id FROM points WHERE source=?)", (source,))

    def for_watermark(self, start, end, source=None):
        """Reminder events and reminders as they bear on a day's record, to tell when a stored one went stale."""
        clauses = ["e.triggered_at>=?", "e.triggered_at<=?"]
        args = [parse_time(start), parse_time(end)]
        if source:
            clauses.append("p.source=?")
            args.append(source)
        events = self.one("""SELECT COUNT(*) AS count,COALESCE(MAX(e.id),0) AS max_id
            FROM events e JOIN points p ON p.id=e.point_id WHERE """ + " AND ".join(clauses), args)
        reminders = self.rows("""SELECT id,text,place_id,active,created_at,last_triggered_at,cancelled_at
            FROM reminders WHERE (created_at>=? AND created_at<=?)
            OR (last_triggered_at>=? AND last_triggered_at<=?)
            OR (cancelled_at>=? AND cancelled_at<=?)
            OR (created_at<=? AND (last_triggered_at IS NULL OR last_triggered_at>?)
                AND (cancelled_at IS NULL OR cancelled_at>?)) ORDER BY id""",
            (parse_time(start), parse_time(end), parse_time(start), parse_time(end),
             parse_time(start), parse_time(end), parse_time(end), parse_time(end), parse_time(end)))
        return (events["count"], events["max_id"]), reminders

    def points_to(self, place_id):
        """Whether any reminder (active or not) points at a place."""
        return self.execute("SELECT 1 FROM reminders WHERE place_id=? LIMIT 1", (int(place_id),)).fetchone() is not None
