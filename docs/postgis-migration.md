# PostGIS storage migration

`GEOLOGS_DATABASE_URL` names the PostgreSQL/PostGIS store, the only one GeoLogs reads; Docker Compose configures it. SQLite archives from older versions are copied in once with `migrate-sqlite`. The schema is implemented in `src/geologs/data/postgres.py`, and the one-time copy is in `src/geologs/data/migration.py`.

The runtime schema keeps raw `point_observations`, a materialized `timeline_fixes` layer, and derived stays. It also adds generated geography columns with GiST indexes for recorded points, saved places, and reverse-cache centers. `near` and reverse-cache lookups use indexed `ST_DWithin`; historical stays, geofences, and reminders retain the existing behavior. Reverse geocoding remains cache-first and on demand.

## Sequence

1. **Done:** Add a PostgreSQL connection behind the shared storage API (now `geologs.data.Database`) and select it with `GEOLOGS_DATABASE_URL`.
2. **Done:** Create the spatial schema, preserve IDs and unique source keys, and add indexed nearby and reverse-cache searches.
3. **Done:** Add `migrate-sqlite`, which copies an existing archive to an empty PostGIS database in one transaction and compares row counts.
4. **Next:** Move arrival candidate selection and saved-place visit matching into indexed SQL. Add concurrency tests for simultaneous source updates and reminder delivery.
5. **Next:** Load-test years of points and concurrent ingestion. Only then consider time partitioning; it adds maintenance cost and is unnecessary for a small archive.

## Key spatial schema

Illustrative PostGIS DDL for the spatial tables (the runtime uses the shared table names and generated geography columns in `postgres.py`):

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE location_points (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source text NOT NULL,
  external_id text,
  observed_at timestamptz NOT NULL,
  position geography(Point, 4326) NOT NULL,
  accuracy_m double precision,
  speed_mps double precision,
  speed_origin text,
  mode text NOT NULL,
  UNIQUE (source, external_id)
);
CREATE INDEX location_points_time_idx ON location_points (observed_at DESC);
CREATE INDEX location_points_position_idx ON location_points USING gist (position);

CREATE TABLE reverse_regions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider text NOT NULL,
  center geography(Point, 4326) NOT NULL,
  radius_m double precision NOT NULL CHECK (radius_m BETWEEN 0 AND 30),
  address jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX reverse_regions_center_idx ON reverse_regions USING gist (center);
CREATE INDEX reverse_regions_provider_expiry_idx ON reverse_regions (provider, expires_at);
```

Use a fixed 30 m prefilter so the GiST index narrows candidates, then check each record's conservative radius:

```sql
SELECT address
FROM reverse_regions
WHERE provider = $1
  AND expires_at > now()
  AND ST_DWithin(center, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography, 30)
  AND ST_DWithin(center, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography, radius_m)
ORDER BY ST_Distance(center, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography)
LIMIT 1;
```

`$2` is longitude and `$3` latitude. The lookup needs a short cache lifetime, provider identity, and a result type. Buildings/amenities reuse within 15 m; streets within 30 m; city-only responses are exact-point entries because a city bounding box may include unrelated addresses. For real administrative or parcel boundaries, store verified polygons separately and use polygon containment. Never treat a geocoder's bounding rectangle as its true boundary.

The cache is checked before calling the provider. Saved places and confirmed visited places now ask for an address when viewed or saved; raw GPS ingestion remains provider-free. A future background enrichment job should have a daily request budget, low rate, and retry queue.

## Query examples

Nearby recorded points:

```sql
SELECT id, observed_at, ST_Distance(position, $1::geography) AS distance_m
FROM location_points
WHERE ST_DWithin(position, $1::geography, $2)
  AND observed_at BETWEEN $3 AND $4
ORDER BY observed_at DESC
LIMIT $5;
```

Arrival candidate places use `ST_DWithin(place.center, new_point.position, 5000)` as an indexed prefilter and then compare against each place's configured radius. The previous source point, deduplication key, reminder state, and event insertion must be read and updated in one transaction to avoid duplicate alerts from concurrent pollers.
