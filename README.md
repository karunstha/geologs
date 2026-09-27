# Geotracker

A self-hosted personal location archive with a browser, arrival reminders, and MCP tools. It reads positions from sources you already have. Docker Compose runs PostgreSQL with PostGIS; SQLite remains available for a small local archive.

## Quick plan

1.  **Collect:** accept timestamped coordinates from Home Assistant, a cron job, CSV, HTTP, or an MCP tool. Keep source IDs so retries do not duplicate data.
2.  **Understand:** retain raw observations, maintain a compact arrival/movement/departure timeline, derive stays and places, estimate speed, classify walking/running or cycling/driving trips with stop tolerance, and surface coverage gaps.
3.  **Act:** trigger one-shot or repeating reminders when a fresh position enters a saved place. Save every event in an inbox and optionally POST it to a webhook.
4.  **Explore:** open a local-day summary with movement runs, visited places, recorded fixes, and a path map. Choose another date or source, inspect trips and stays, and export that day's GeoJSON.
5.  **Extend:** add adapters for other location sources and providers without changing the archive or MCP tools.

## What this adds to the reference projects

-   [location-history-mcp-server](https://github.com/lucas-1000/location-history-mcp-server) has place recognition and history tools, but its documented path uses a companion iOS app, PostGIS, and optional Google Places. Geotracker accepts existing sources and supports PostGIS and SQLite.
-   [tars-location-mcp](https://github.com/michelgrolet/tars-location-mcp) is strong on historical imports, coverage, and travel memory. Its documented live path uses OwnTracks; Geotracker adds direct Home Assistant polling, arrival reminders, and a browser.
-   [location\_profiler](https://github.com/2015xli/location_profiler) models stay-point transitions and predicts destinations. Geotracker focuses on a live source-to-reminder loop and raw point/trip exploration.
-   [mcp-geo](https://github.com/handsomegentlemen/mcp-geo) exposes the request's coarse geolocation. Geotracker stores a personal history with precise source-fed points and geofences.

These comparisons refer to the projects' public descriptions as of September 2026.

The [reference feature audit](docs/reference-feature-audit.md) tracks which useful capabilities are built and which still need work.

## Run with Docker Compose

Copy `.env.example` to `.env` and replace both secrets with random values. Then:

```bash
docker compose up --build -d
```

Open `http://127.0.0.1:8765` and enter `GEOTRACKER_TOKEN` in the browser sign-in panel. Set `GEOTRACKER_PUBLISHED_PORT` in `.env` if port 8765 is occupied, then use that port for the browser, MCP clients, and the Home Assistant poller. Run `docker compose up -d --force-recreate app` after changing the port. The browser keeps an HttpOnly, same-site session cookie for 30 days, so a new tab does not need the token again. Compose mounts only `.env` read-only for the app to check the current token on each request; changing the token invalidates the old browser session. Other Compose environment changes still need a container recreation. PostgreSQL/PostGIS files live in `./data/postgres`, which is ignored by Git and must be included in your backups. The official PostGIS image is pinned to `linux/amd64`; ARM hosts run that service through Docker emulation.

If you already ran an earlier Compose version with a named `postgres_data` volume, copy it **before starting this version**:

```bash
./scripts/migrate-postgres-data
```

The script stops the old containers, copies the volume into `./data/postgres`, then starts the new Compose stack. It refuses to overwrite a populated target and keeps the original named volume as a recovery copy. Run it from the checkout on the host that owns the old Docker volume.

To copy an existing SQLite archive, stop the app container while retaining the database container, then run the migration against an **empty** PostGIS database:

```bash
docker compose stop app
docker compose run --rm -v "$PWD/geotracker.db:/import/geotracker.db:ro" app geotracker migrate-sqlite /import/geotracker.db
docker compose up -d app
```

The copy preserves IDs and checks row counts in one transaction. Keep the SQLite file as a backup until you have inspected the new archive.

## Run locally without Docker

Requires Python 3.11+. SQLite uses only the Python standard library. For PostgreSQL, install `pip install -e '.[postgres]'` and set `GEOTRACKER_DATABASE_URL` to a PostgreSQL connection URL. When that variable is set, PostGIS is used; otherwise the SQLite path below is used.

```bash
export GEOTRACKER_DB="$PWD/geotracker.db"
export GEOTRACKER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./geotracker serve
```

Open `http://127.0.0.1:8765` and enter the same `GEOTRACKER_TOKEN` in the sign-in panel. The CLI also reads a `.env` file from its working directory for variables that are not already set in the process environment. Restart the server after changing `.env`; an already-running process keeps its original token. The server listens only on loopback by default. If you bind a different host, a token is required; put a TLS reverse proxy in front of it for access beyond your machine.

An optional `pip install -e .` provides the same `geotracker` command globally or in a virtual environment.

Your SQLite file stays wherever `GEOTRACKER_DB` points; the default is `~/.local/share/geotracker/geotracker.db`. PostgreSQL data stays in its configured database.

## Connect Home Assistant with cron

Create a long-lived Home Assistant token from your profile's Security tab and identify a GPS `device_tracker` entity with `latitude` and `longitude` attributes. The poller uses the [Home Assistant state API](https://developers.home-assistant.io/docs/api/rest/) and records each successful poll as a sample, even when Home Assistant has not changed its state. Fill these fields in this project's `.env` file:

```dotenv
HA_URL=http://homeassistant.local:8123
HA_TOKEN=your-home-assistant-token
HA_ENTITY=device_tracker.my_phone
GEOTRACKER_URL=http://127.0.0.1:8765
```

Use the port from `GEOTRACKER_PUBLISHED_PORT` in `GEOTRACKER_URL`. `GEOTRACKER_TOKEN` is already in the same file. Check that the selected entity has current coordinates, then test one poll from the project directory:

```bash
./geotracker poll-ha
```

For ongoing collection, run it every minute from cron or a systemd timer. A cron entry must change to this project directory so the CLI reads its `.env`; use absolute paths for both. Keep `.env` private because it contains both tokens. In a Docker setup, the host poller posts to the published app port; it does not need direct database access. A local non-Docker poller can also use `GEOTRACKER_DATABASE_URL` or `GEOTRACKER_DB` directly.

Home Assistant may update a tracker at a slower interval than cron. Each successful poll increments the raw sample count. The compact timeline keeps the first and latest sample at a stationary period and retains the intervening samples while moving. The `recorded` field in the poll result counts newly stored samples; `inserted` counts new source events, and `duplicates` counts repeated source events. Repeated polls do not trigger another arrival reminder. Each sample has its own speed estimate from the preceding recorded sample; unchanged coordinates are idle instead of inheriting an earlier moving speed. Speed derived from two positions is only an estimate; sparse GPS samples and location drift can misclassify activity. Only fixes within 30 minutes of the server clock can trigger reminders; historical CSV imports never trigger them.

## Ingest another source

Send one point or an array of up to 1,000 points to `POST /api/points`:

```bash
curl -X POST http://127.0.0.1:8765/api/points \
  -H "Authorization: Bearer $GEOTRACKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source":"my-script","external_id":"sample-123","timestamp":"2026-09-26T14:00:00Z","lat":41.88,"lon":-87.63,"accuracy_m":15,"speed_mps":0}'
```

Required fields: `lat`, `lon`. Use a timezone-aware ISO 8601 `timestamp` and stable `source`/`external_id` for reliable history and retries. `speed_mps` and `accuracy_m` are optional. The server can estimate speed if consecutive fixes are 5–900 seconds apart, both are reasonably accurate, and the implied speed is plausible. A single point with no speed is classified as `unknown`.

CSV files can contain `timestamp,lat,lon` plus optional `accuracy_m,speed_mps,external_id`:

```bash
./geotracker import-csv locations.csv --source old-phone
```

Classic Google Takeout `Records.json` or `Location History.json` files with a top-level `locations` array can be imported without loading the whole file into memory:

```bash
./geotracker import-takeout Records.json
```

The importer supports `latitudeE7`, `longitudeE7`, `timestamp` or `timestampMs`, and optional `accuracy`. It is safe to rerun overlapping exports. Modern semantic segment and `timelineObjects` exports need a separate parser and are not accepted by this command. Historical imports never trigger reminders or geocoding. After repairing imported data, use `docker compose exec app geotracker rebuild-stays` for the Docker database, or `./geotracker rebuild-stays` for a local SQLite archive.

## Observed stays and place patterns

The archive derives stays incrementally from raw observations, including repeated Home Assistant polls: at least two samples with accuracy no worse than 100 m within 50 m, spanning at least five minutes, with no gap above 30 minutes. Existing observations are analyzed when the updated server first starts, so a new stay can appear without collecting more data. Repeated stops are grouped into frequent-place suggestions; you can name them in the browser. Saved places also get observed visit counts and sampled time. These are lower bounds when GPS coverage is sparse.

The History page opens on the browser's local day. `point_observations` is the append-only raw audit trail. `timeline_fixes` is the compact informational layer: movement samples remain, while a stationary run keeps its arrival and latest/departure edges. `observed_stays_v2` and place clusters are derived from raw observations. “Timeline fixes” shows key fixes out of raw samples, and the Samples tab can switch between both layers. Raw samples are fetched in 50-row pages. Samples, stays, common places, transitions, trips, and visited places can focus the map. Driving, walking, and combined running/cycling counts are connected travel runs. A short stop such as a traffic light stays in the same run; a stop of five minutes or a gap over 15 minutes normally ends it. When repeated polls contain the same source event ID, the tracker treats them as one held source state for up to 15 minutes. A following fresh fix is tested against that full observation window instead of only the final repeated poll. These are estimates from sampled speeds and displacement, and running is not distinguished from cycling.

The map applies local interpolation that passes through every included GPS fix and never sends coordinates to a routing provider. Sources are routed independently, so interleaved devices no longer break one another's paths. Fixes with accuracy worse than 150 m are omitted; gaps over 15 minutes and jumps implying more than 200 km/h remain disconnected. Stored speed does not decide whether a fix is drawn. The map does not know the road graph, so sparse coordinates can produce a line across a lake or away from a road; the line is a recorded GPS estimate rather than turn-by-turn road matching. The Places page lists saved places first and all observed places in a second tab, ranked by total observed time and visit count. An observed place can be saved directly for reminders.

The Settings page controls speed handling for newly received samples. The default uses source-reported speed when present and calculates from consecutive coordinates when it is missing. “Always calculate” ignores reported speed. A source-speed adjustment in km/h is applied only when reported speed is enabled; negative values correct a source that consistently reads high. The built-in Home Assistant poller currently sends coordinates and accuracy without speed, so values marked `est.` are calculated by Geotracker and are unaffected by the source-speed adjustment.

Raw storage and arrival reminders happen immediately. Stay analysis processes only each source's new tail and does not expose a stay until nearby samples span at least five minutes. The first request for a day's summary calculates its trips and routes, then saves the complete result with a raw-data watermark. Repeated requests, including requests after a server restart, read that result from the database. A new raw sample changes the watermark; saved-place edits and explicit analysis rebuilds clear the cache. Place patterns are derived from confirmed stays when requested. Waiting for 100 samples would add about 100 minutes of lag to a one-minute Home Assistant poll without adding a time-based confidence signal, so analysis is incremental rather than gated by an arbitrary point count.

Consecutive stays create transition counts. A next-destination estimate needs at least three observed departures and recent evidence; older transitions receive less weight. The current-location tool reports fix age and whether it is stale. Day summaries accept an IANA timezone and include a coverage note. No place or person is inferred from an IP address.

## Reminders and notifications

Save a place with coordinates and a 20–5,000 m radius. Then create a reminder such as “Pick up salt” at Walmart. A new point inside after an outside or stale previous fix creates an event. One-shot reminders deactivate; repeating reminders honor a cooldown. Low accuracy readings larger than the geofence radius or 150 m are ignored for triggering.

The browser's arrival inbox persists events. Browser notifications work while that tab is open and permission is granted. For alerts when the browser is closed, set `GEOTRACKER_WEBHOOK_URL` to an HTTPS endpoint (or loopback HTTP). Each event is POSTed as JSON; `GEOTRACKER_WEBHOOK_TOKEN` adds a bearer header. This can feed a Home Assistant automation, `ntfy`, or your own notifier. Failed webhook attempts remain marked in the event log; automatic retries are not implemented yet.

## Geocoding

-   Set `GEOAPIFY_API_KEY` to enable address search and reverse geocoding. [Geoapify's free plan](https://www.geoapify.com/pricing/) currently lists 3,000 credits/day; creating an API key requires an account. Address searches favor the latest recorded fix. Saved and confirmed visited places are reverse geocoded through the cache; raw GPS fixes are never automatically reverse geocoded.
-   Set `GEOTRACKER_PHOTON_URL` to use your own [Photon](https://github.com/komoot/photon/blob/master/docs/api-v1.md) instance for forward/reverse geocoding. This takes precedence over Geoapify for geocoding.

Saved places, raw paths, trip estimates, reminders, and MCP history work without any provider key. The browser loads Leaflet from its CDN and OpenStreetMap map tiles, so map browsing sends viewport/tile requests to those services. It does not send your stored GPS history to the tile service.

Reverse geocoding uses an indexed region cache. A precise building or amenity result is reused within 15 m, and a street result within 30 m. The spatial index narrows candidates; an exact distance check confirms each hit. These small circles are heuristics, not verified parcel boundaries, so reused results are marked `approximate`. City and other broad results are cached at the requested point only because their actual boundaries are unknown. Cache entries expire after 180 days by default (`GEOTRACKER_REVERSE_CACHE_DAYS`). The returned coordinates remain the coordinates you requested, even when the address came from a nearby cache entry. This avoids repeated provider calls without assuming that an entire city bounding box has one address.

PostGIS stores point and reverse-cache geography under GiST indexes and uses `ST_DWithin` for nearby searches. A city bounding box alone is not a reliable address boundary. The [PostGIS migration notes](docs/postgis-migration.md) describe what is implemented and what still needs scale testing.

## MCP

The running server exposes MCP at `http://127.0.0.1:8765/mcp` using stateless Streamable HTTP. Start it with `docker compose up --build -d` or `./geotracker serve`, then point an MCP client at that URL. For clients that accept a URL and headers in JSON, the connection looks like:

```json
{
  "mcpServers": {
    "geotracker": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer YOUR_GEOTRACKER_TOKEN"}
    }
  }
}
```

Use the token from `GEOTRACKER_TOKEN` when one is configured. The endpoint accepts JSON-RPC messages at `POST /mcp`; clients must send `Content-Type: application/json` and `Accept: application/json, text/event-stream`. It responds with JSON for requests and `202 Accepted` for notifications. This server does not issue session IDs or provide a standalone event stream, so `GET /mcp` returns `405 Method Not Allowed`. Available tools cover current/raw/compact history, nearby/time lookup, recorded trips, stats, coverage, observed stays and places, saved place visits and observed time, day summaries, transitions, next-destination estimates, reminders and events, ingestion, geocoding, and reverse geocoding. No raw SQL execution is exposed.

## API overview

`GET /api/current`, `/api/points`, `/api/observations`, `/api/timeline`, `/api/settings`, `/api/stats`, `/api/trips`, `/api/coverage`, `/api/near`, `/api/stays`, `/api/frequent-places`, `/api/visited-places`, `/api/transitions`, `/api/predict-next`, `/api/insights`, `/api/day`, `/api/places`, `/api/place-visits`, `/api/reminders`, `/api/events`, `/api/geocode`, `/api/reverse`, `/api/export.geojson`; `POST /api/points`, `/api/settings`, `/api/places`, `/api/reminders`, `/api/reminders/active`, `/api/events/ack`, `/mcp`. `/api/observations` accepts `limit` and `offset` for raw-data pagination.

All data `/api/*` routes and `/mcp` require authentication when a token is configured. API clients can use the bearer token; the browser exchanges the token at `POST /api/session` for an HttpOnly session cookie. Requests from a different browser origin are rejected. The API has no account or multiuser support; run one archive per person.

## Next features

Good next additions are: PostGIS scale and concurrency testing; semantic Google Timeline and OwnTracks imports; home periods and city/country trip summaries; dwell/leave geofences; smarter activity detection using sensor data; geofence hysteresis and accuracy confidence; provider quotas and bulk enrichment jobs; configurable raw retention and privacy zones; optional offline road-graph map matching; and a background webhook retry queue.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --check src/geotracker/web/app.js
```
