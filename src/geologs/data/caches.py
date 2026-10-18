"""Derived values kept to save work: whole analyses, place lookups, and reverse lookups by area."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .. import config
from ..domain.geo import metres
from ..domain.timing import parse_time
from .repository import Repository


class CachesRepository(Repository):
    """Cached values are not part of the work that looked them up.

    A cache write is kept at once when nothing else is waiting to be
    committed, so a page that looks up many places never holds the
    database's write lock across the provider calls between them. When the
    caller has uncommitted work of its own, the write joins it instead and is
    kept (or dropped) with it: a lookup never commits half of a caller's change.
    """

    def _keep(self, query, args):
        pending = self.db.pending_writes
        cursor = self.execute(query, args)
        if not pending:
            self.db.commit()
        return cursor
    def analysis(self, cache_key, watermark):
        row = self.execute("SELECT value FROM analysis_cache WHERE cache_key=? AND watermark=?", (cache_key, watermark)).fetchone()
        return json.loads(row["value"]) if row else None

    def save_analysis(self, cache_key, watermark, value):
        self._keep("""INSERT INTO analysis_cache(cache_key,watermark,value,created_at) VALUES (?,?,?,?)
            ON CONFLICT(user_id,cache_key) DO UPDATE SET watermark=excluded.watermark,value=excluded.value,created_at=excluded.created_at""",
            (cache_key, watermark, json.dumps(value, separators=(",", ":")), parse_time(None)))

    def clear_analysis(self):
        """Forget every cached analysis: something they were built from changed."""
        self.execute("DELETE FROM analysis_cache")

    def lookup(self, key, provider):
        row = self.execute("SELECT value FROM geocode_cache WHERE cache_key=? AND provider=?", (key, provider)).fetchone()
        return json.loads(row["value"]) if row else None

    def save_lookup(self, key, provider, value):
        self._keep("""INSERT INTO geocode_cache(cache_key,provider,value,created_at) VALUES (?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,value=excluded.value,created_at=excluded.created_at""",
            (key, provider, json.dumps(value), parse_time(None)))

    def reverse_region(self, provider, lat, lon):
        """Regions near the position (indexed), then an exact distance check for a safe cache hit."""
        now = parse_time(None)
        candidates = self.rows("""SELECT id,provider,center_lat,center_lon,radius_m,value,created_at,expires_at
            FROM reverse_regions WHERE provider=? AND expires_at>?
              AND ST_DWithin(center,ST_SetSRID(ST_MakePoint(?,?),4326)::geography,30)
              AND ST_DWithin(center,ST_SetSRID(ST_MakePoint(?,?),4326)::geography,radius_m)
            ORDER BY id DESC LIMIT 64""", (provider, now, lon, lat, lon, lat))
        for row in candidates:
            if metres(lat, lon, row["center_lat"], row["center_lon"]) <= row["radius_m"]:
                return json.loads(row["value"])
        return None

    def forget_reverse_regions(self):
        """Drop every cached reverse lookup; each was made at a position you were at."""
        self.execute("DELETE FROM reverse_regions")

    def forget_reverse_regions_near(self, lat, lon, radius_m):
        """Drop cached reverse lookups made within radius_m of a position."""
        self.execute("""DELETE FROM reverse_regions
            WHERE ST_DWithin(center,ST_SetSRID(ST_MakePoint(?,?),4326)::geography,?)""", (lon, lat, radius_m))

    def save_reverse_region(self, provider, lat, lon, radius_m, value):
        """Keep a lookup for reuse within radius_m (at most 30 m) for GEOLOGS_REVERSE_CACHE_DAYS (180)."""
        radius = max(0.0, min(float(radius_m), 30.0))
        now = datetime.now(timezone.utc)
        days = config.reverse_cache_days()
        pending = self.db.pending_writes
        self.execute("""INSERT INTO reverse_regions
            (provider,center_lat,center_lon,radius_m,value,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?)""", (provider, lat, lon, radius, json.dumps(value), now.isoformat(timespec="seconds"), (now + timedelta(days=days)).isoformat(timespec="seconds")))
        if not pending:
            self.db.commit()
