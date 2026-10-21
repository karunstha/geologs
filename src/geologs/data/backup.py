"""One user's whole archive as rows to write out and read back in: a backup that clones an archive
onto another server. Stays and the timeline are not kept; they are found again from the fixes."""
from __future__ import annotations

from .repository import Repository
from .settings import IMPORT_STATUS_KEY

# What a backup holds, table by table, in the order it is written: a table comes before the tables
# it points at. A fix and its point are saved together, so reading point_observations before points
# (and events, source_state and reminders before what they name) never finds a row whose target
# is missing, though fixes keep arriving while a backup is written.
TABLES = {
    "point_observations": "point_id legacy_point_id source timestamp lat lon accuracy_m speed_mps speed_origin received_at fingerprint",
    "events": "reminder_id point_id triggered_at webhook_status",
    "source_state": "source point_id",
    "points": "id source external_id timestamp lat lon accuracy_m speed_mps speed_origin mode",
    "reminders": "id text place_id active repeat cooldown_minutes created_at last_triggered_at cancelled_at destination_name destination_address destination_category destination_provider_place_id match_name",
    "places": "id name lat lon radius_m address category provider_place_id saved",
    "place_labels": "name lat lon radius_m address category provider_place_id created_at updated_at source started_at ended_at",
    "stay_edits": "source action started_at ended_at lat lon created_at",
    "mode_overrides": "source target started_at ended_at mode created_at updated_at",
    "path_exclusions": "source started_at ended_at mode distance_m created_at",
    "known_paths": "source started_at ended_at mode distance_m min_lat min_lon max_lat max_lon coordinates",
    "trip_tracks": "source started_at ended_at mode from_lat from_lon to_lat to_lon from_name to_name distance_m observed_m pieces",
    "place_routes": "mode from_lat from_lon to_lat to_lon from_name to_name distance_m observed_m min_lat min_lon max_lat max_lon parts members latest seed",
    "daily_summaries": "summary_date timezone source watermark points distance_km trip_count place_count reminder_count summary brief created_at updated_at",
    "app_settings": "key value",
    "ingest_devices": "kind device last_attempt_at last_status last_message last_success_at received",
    "ingest_tokens": "name token_hash hint source created_at last_used_at last_source",
    "api_tokens": "name token_hash hint created_at last_used_at",
}
# Account tables without row-level security: their rows are filtered by owner here.
OWNED = ("ingest_tokens", "api_tokens")
# Tables restored by key: a row whose key is already here is kept as it is (settings take the
# backup's value). The rest have no key; a row identical to one already here is skipped.
KEYED = ("known_paths", "trip_tracks", "place_routes", "path_exclusions", "daily_summaries", "ingest_devices",
         "ingest_tokens", "api_tokens")


def columns(table):
    return TABLES[table].split()


class BackupRepository(Repository):
    def table_rows(self, table):
        """Every row of one of TABLES, as lists in its column order, read a few thousand at a time."""
        names = columns(table)
        query, args = f"SELECT {','.join(names)} FROM {table}", ()
        if table in OWNED:
            query, args = query + " WHERE user_id=%s", (self.db.user_id,)
        elif table == "app_settings":
            query, args = query + " WHERE key<>%s", (IMPORT_STATUS_KEY,)
        with self.db.conn.connection.cursor(name=f"geologs_backup_{table}") as cursor:
            cursor.itersize = 5000
            cursor.execute(query, args)
            for row in cursor:
                yield [row[name] for name in names]

    def stage(self, table, names, rows):
        """Load a backup's rows of `table` (its `names` columns, those this version knows) into a
        scratch table of the transaction, to be restored by restore(). Returns how many."""
        known = [name for name in names if name in TABLES[table]]
        positions = [names.index(name) for name in known]
        self.db.pending_writes = True
        self.db.conn.execute(f"""CREATE TEMP TABLE IF NOT EXISTS restore_{table} ON COMMIT DROP AS
            SELECT {','.join(columns(table))} FROM {table} WITH NO DATA""")
        count = 0
        with self.db.conn.connection.cursor() as cursor:
            with cursor.copy(f"COPY restore_{table} ({','.join(known)}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row([row[position] for position in positions])
                    count += 1
        return count

    def restore(self):
        """Add the staged rows to this user's archive, with new ids, and return how many of each table
        were new. What is already here stays: restoring the same backup twice adds nothing."""
        for table in TABLES:
            self.db.conn.execute(f"""CREATE TEMP TABLE IF NOT EXISTS restore_{table} ON COMMIT DROP AS
                SELECT {','.join(columns(table))} FROM {table} WITH NO DATA""")
        added = {}
        added["points"] = self.execute("""INSERT INTO points(source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode)
            SELECT source,external_id,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,mode FROM restore_points
            ON CONFLICT DO NOTHING""").rowcount
        # The backup's ids of points, places and reminders, and those they have here.
        self.execute("""CREATE TEMP TABLE restore_point_ids ON COMMIT DROP AS
            SELECT r.id AS old_id,p.id FROM restore_points r JOIN points p
            ON p.source=r.source AND p.timestamp=r.timestamp AND p.lat=r.lat AND p.lon=r.lon""")
        self.execute("""INSERT INTO restore_point_ids SELECT r.id,p.id FROM restore_points r
            JOIN points p ON p.source=r.source AND p.external_id=r.external_id
            WHERE NOT EXISTS (SELECT 1 FROM restore_point_ids m WHERE m.old_id=r.id)""")
        self.db.conn.execute("CREATE UNIQUE INDEX ON restore_point_ids(old_id)")
        self.db.conn.execute("ANALYZE restore_point_ids")
        # A fix already here (from an earlier import of part of this history, under the same tracker
        # name) is not recorded twice. Fixes repeated within the backup all come in, as they were.
        added["point_observations"] = self.execute("""INSERT INTO point_observations
            (point_id,legacy_point_id,source,timestamp,lat,lon,accuracy_m,speed_mps,speed_origin,received_at,fingerprint)
            SELECT m.id,l.id,r.source,r.timestamp,r.lat,r.lon,r.accuracy_m,r.speed_mps,r.speed_origin,r.received_at,r.fingerprint
            FROM restore_point_observations r JOIN restore_point_ids m ON m.old_id=r.point_id
            LEFT JOIN restore_point_ids l ON l.old_id=r.legacy_point_id
            WHERE NOT EXISTS (SELECT 1 FROM point_observations o WHERE o.point_id=m.id AND o.timestamp=r.timestamp
                AND o.lat=r.lat AND o.lon=r.lon AND o.accuracy_m IS NOT DISTINCT FROM r.accuracy_m)
            ON CONFLICT DO NOTHING""").rowcount
        added["source_state"] = self.execute("""INSERT INTO source_state(source,point_id)
            SELECT r.source,m.id FROM restore_source_state r JOIN restore_point_ids m ON m.old_id=r.point_id
            ON CONFLICT DO NOTHING""").rowcount
        place_columns = [name for name in columns("places") if name != "id"]
        added["places"] = self.execute(f"""INSERT INTO places({','.join(place_columns)})
            SELECT {','.join(place_columns)} FROM restore_places ON CONFLICT DO NOTHING""").rowcount
        self.execute("""CREATE TEMP TABLE restore_place_ids ON COMMIT DROP AS
            SELECT r.id AS old_id,p.id FROM restore_places r JOIN places p ON lower(p.name)=lower(r.name)""")
        reminder_columns = [name for name in columns("reminders") if name not in ("id", "place_id")]
        added["reminders"] = self.execute(f"""INSERT INTO reminders(place_id,{','.join(reminder_columns)})
            SELECT m.id,{','.join('r.' + name for name in reminder_columns)} FROM restore_reminders r
            LEFT JOIN restore_place_ids m ON m.old_id=r.place_id
            WHERE NOT EXISTS (SELECT 1 FROM reminders x WHERE x.text=r.text AND x.created_at=r.created_at)""").rowcount
        self.execute("""CREATE TEMP TABLE restore_reminder_ids ON COMMIT DROP AS
            SELECT r.id AS old_id,MIN(x.id) AS id FROM restore_reminders r
            JOIN reminders x ON x.text=r.text AND x.created_at=r.created_at GROUP BY r.id""")
        added["events"] = self.execute("""INSERT INTO events(reminder_id,point_id,triggered_at,webhook_status)
            SELECT rm.id,pm.id,r.triggered_at,r.webhook_status FROM restore_events r
            JOIN restore_reminder_ids rm ON rm.old_id=r.reminder_id JOIN restore_point_ids pm ON pm.old_id=r.point_id
            WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.reminder_id=rm.id AND e.point_id=pm.id
                AND e.triggered_at=r.triggered_at)""").rowcount
        added["app_settings"] = self.execute("""INSERT INTO app_settings(key,value)
            SELECT key,value FROM restore_app_settings WHERE key<>? ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value
            WHERE app_settings.value IS DISTINCT FROM excluded.value""", (IMPORT_STATUS_KEY,)).rowcount
        for table in KEYED:
            names = columns(table)
            owner = ",user_id" if table in OWNED else ""
            select = ",".join(names) + (",?" if owner else "")
            added[table] = self.execute(f"""INSERT INTO {table}({','.join(names)}{owner})
                SELECT {select} FROM restore_{table} ON CONFLICT DO NOTHING""",
                (self.db.user_id,) if owner else ()).rowcount
        for table in ("place_labels", "stay_edits", "mode_overrides"):
            names = columns(table)
            same = " AND ".join(f"x.{name} IS NOT DISTINCT FROM r.{name}" for name in names)
            added[table] = self.execute(f"""INSERT INTO {table}({','.join(names)})
                SELECT {','.join('r.' + name for name in names)} FROM restore_{table} r
                WHERE NOT EXISTS (SELECT 1 FROM {table} x WHERE {same})""").rowcount
        return added

    def staged_sources(self):
        """The trackers of the staged fixes, whose stays a restore finds again."""
        return [row["source"] for row in self.rows("SELECT DISTINCT source FROM restore_points ORDER BY source")]
