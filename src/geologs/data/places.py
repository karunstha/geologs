"""Saved places (zones you named) and name corrections (place_labels)."""
from __future__ import annotations

from .repository import Repository

PLACE_COLUMNS = "id,name,lat,lon,radius_m,address,category,provider_place_id"


class PlacesRepository(Repository):
    def saved(self):
        """Places you saved, by name; hidden ones (a deleted place a reminder still points at) are left out."""
        return self.rows(f"SELECT {PLACE_COLUMNS} FROM places WHERE COALESCE(saved,1)=1 ORDER BY name")

    def get(self, place_id):
        """Any place by id, hidden or not, or None."""
        return self.one(f"SELECT {PLACE_COLUMNS},saved FROM places WHERE id=?", (int(place_id),))

    def saved_place(self, place_id):
        rows = self.rows(f"SELECT {PLACE_COLUMNS} FROM places WHERE id=?", (int(place_id),))
        return rows[0] if rows else None

    def named(self, name):
        """The id of the place (saved or hidden) with a name, ignoring case, or None."""
        row = self.one("SELECT id FROM places WHERE lower(name)=lower(?)", (name,))
        return row["id"] if row else None

    def is_saved(self, place_id):
        return self.execute("SELECT id FROM places WHERE id=? AND COALESCE(saved,1)=1", (int(place_id),)).fetchone() is not None

    def add(self, name, lat, lon, radius, address, category, provider_place_id, saved=True):
        """A new place's id. Unsaved places are reminder destinations, kept off your list."""
        return self.execute("""INSERT INTO places(name,lat,lon,radius_m,address,category,provider_place_id,saved)
            VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
            (name, lat, lon, radius, address, category, provider_place_id, int(saved))).fetchone()["id"]

    def update(self, place_id, name, lat, lon, radius, address, category, provider_place_id):
        """Change a place (saving it again if it was hidden); False when there is no such place."""
        return bool(self.execute("""UPDATE places SET name=?,lat=?,lon=?,radius_m=?,address=?,category=?,
            provider_place_id=?,saved=1 WHERE id=?""",
            (name, lat, lon, radius, address, category, provider_place_id, int(place_id))).rowcount)

    def set_details(self, place_id, address, category, provider_place_id):
        self.execute("UPDATE places SET address=?,category=?,provider_place_id=? WHERE id=?",
                     (address, category, provider_place_id, place_id))

    def hide(self, place_id, placeholder_name):
        """Take a place off your list but keep its row (a reminder points at it)."""
        self.execute("UPDATE places SET saved=0,name=? WHERE id=?", (placeholder_name, int(place_id)))

    def delete(self, place_id):
        self.execute("DELETE FROM places WHERE id=?", (int(place_id),))

    def labels(self):
        """Name corrections. Without a time window one names every visit to its spot;
        with one (and usually a source) it names only the visit it was made for."""
        return self.rows("""SELECT id,name,lat,lon,radius_m,address,category,provider_place_id,source,started_at,ended_at,
            created_at,updated_at FROM place_labels ORDER BY name,id""")

    def label(self, label_id):
        return next((label for label in self.labels() if label["id"] == label_id), None)

    def add_label(self, name, lat, lon, radius, address, category, provider_place_id, source, started_at, ended_at, now):
        return self.execute("""INSERT INTO place_labels
            (name,lat,lon,radius_m,address,category,provider_place_id,source,started_at,ended_at,updated_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (name, lat, lon, radius, address, category, provider_place_id, source, started_at, ended_at, now, now)).fetchone()["id"]

    def update_label(self, label_id, name, lat, lon, radius, address, category, provider_place_id, source, started_at,
                     ended_at, now):
        """False when there is no such label."""
        return bool(self.execute("""UPDATE place_labels SET name=?,lat=?,lon=?,radius_m=?,address=?,category=?,
            provider_place_id=?,source=?,started_at=?,ended_at=?,updated_at=? WHERE id=?""",
            (name, lat, lon, radius, address, category, provider_place_id, source, started_at, ended_at, now,
             int(label_id))).rowcount)

    def delete_visit_labels(self, source):
        """Forget names you gave one tracker's visits (names for a spot stay)."""
        self.execute("DELETE FROM place_labels WHERE source=? AND started_at IS NOT NULL", (source,))

    def for_watermark(self):
        """Every place and name as they bear on a day's record, to tell when a stored one went stale."""
        return (self.rows(f"SELECT {PLACE_COLUMNS} FROM places ORDER BY id"),
                self.rows("SELECT id,name,lat,lon,radius_m,source,started_at,ended_at,updated_at FROM place_labels ORDER BY id"))

    def delete_label(self, label_id):
        """False when there is no such label."""
        return bool(self.execute("DELETE FROM place_labels WHERE id=?", (int(label_id),)).rowcount)
