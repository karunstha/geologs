# GeoLogs

A self-hosted personal location archive with a browser, arrival reminders, and MCP tools. It reads positions from sources you already have. Docker Compose runs PostgreSQL with PostGIS; SQLite remains available for a small local archive.

## Quick plan

1.  **Collect:** accept timestamped coordinates from Home Assistant, a cron job, CSV, HTTP, or an MCP tool. Keep source IDs so retries do not duplicate data.
2.  **Understand:** retain raw observations, maintain a compact arrival/movement/departure timeline, derive 15 minute stays (sitting still, or moving around one place such as a lake loop) and the trips between them, estimate speed, classify walking, running, cycling, or driving, and surface coverage gaps.
3.  **Act:** trigger a one-shot reminder when a fresh position enters a saved place. Keep completed and cancelled reminders in history and optionally POST triggered reminders to a webhook.
4.  **Explore:** open a local-day summary with stay-to-stay trips, visited places, the current named location, recorded fixes, and a path map. Choose another date or source, inspect trips and stays, and export that day's GeoJSON.
5.  **Extend:** add adapters for other location sources and providers without changing the archive or MCP tools.

## What this adds to the reference projects

-   [location-history-mcp-server](https://github.com/lucas-1000/location-history-mcp-server) has place recognition and history tools, but its documented path uses a companion iOS app, PostGIS, and optional Google Places. GeoLogs accepts existing sources and supports PostGIS and SQLite.
-   [tars-location-mcp](https://github.com/michelgrolet/tars-location-mcp) is strong on historical imports, coverage, and travel memory. Its documented live path uses OwnTracks; GeoLogs adds direct Home Assistant polling, arrival reminders, and a browser.
-   [location\_profiler](https://github.com/2015xli/location_profiler) models stay-point transitions and predicts destinations. GeoLogs focuses on a live source-to-reminder loop and raw point/trip exploration.
-   [mcp-geo](https://github.com/handsomegentlemen/mcp-geo) exposes the request's coarse geolocation. GeoLogs stores a personal history with precise source-fed points and geofences.

These comparisons refer to the projects' public descriptions as of September 2026.

The [reference feature audit](docs/reference-feature-audit.md) tracks which useful capabilities are built and which still need work.

## Run with Docker Compose

Copy `.env.example` to `.env` and replace both secrets with random values. Then:

```bash
docker compose up --build -d
```

Open `http://127.0.0.1:8765` and enter `GEOLOGS_TOKEN` in the browser sign-in panel. Set `GEOLOGS_PUBLISHED_PORT` in `.env` if port 8765 is occupied, then use that port for the browser, MCP clients, and the Home Assistant poller. Set `GEOLOGS_TIMEZONE` to an IANA timezone such as `America/Chicago`; it can later be changed from Settings. Run `docker compose up -d --force-recreate app` after changing Compose environment values. The browser keeps an HttpOnly, same-site session cookie for 30 days, so a new tab does not need the token again. Compose mounts only `.env` read-only for the app to check the current token on each request; changing the token invalidates the old browser session. PostgreSQL/PostGIS files live in `./data/postgres`, which is ignored by Git and must be included in your backups. The official PostGIS image is pinned to `linux/amd64`; ARM hosts run that service through Docker emulation.

If you already ran an earlier Compose version with a named `postgres_data` volume, copy it **before starting this version**:

```bash
./scripts/migrate-postgres-data
```

The script stops the old containers, copies the volume into `./data/postgres`, then starts the new Compose stack. It refuses to overwrite a populated target and keeps the original named volume as a recovery copy. Run it from the checkout on the host that owns the old Docker volume.

### Upgrading from Geotracker

GeoLogs was called Geotracker. To upgrade a Compose install from before the rename, pull the new code and run this once from the checkout while the old database container is still running:

```bash
./scripts/rename-from-geotracker
```

It renames the `GEOTRACKER_*` keys in `.env` to `GEOLOGS_*` and keeps their values. It renames the PostgreSQL role and database from `geotracker` to `geologs` in place, which keeps the data and the password, then rebuilds and restarts the stack (the database container is now `geologs-db`). It changes nothing if both names already exist in PostgreSQL or the old role's password is MD5-hashed, and it is safe to rerun. Afterwards:

-   Change cron entries from `./geotracker poll-ha` to `./geologs poll-ha`.
-   Sign in to the browser once more; the session cookie has a new name.
-   Database clients on port 15432 connect as user `geologs` to database `geologs`.
-   Hermes keeps working as configured. Its MCP server name, the token variable in `~/.hermes/.env`, and the webhook subscription (whose name is part of the `GEOLOGS_WEBHOOK_URL` value) belong to Hermes; rename them there only if you want to.

Outside Docker, the CLI still accepts `GEOTRACKER_*` settings and prints a reminder to rename them, and it keeps using `~/.local/share/geotracker/geotracker.db` until a `geologs.db` exists.

To copy an existing SQLite archive, stop the app container while retaining the database container, then run the migration against an **empty** PostGIS database:

```bash
docker compose stop app
docker compose run --rm -v "$PWD/geologs.db:/import/geologs.db:ro" app geologs migrate-sqlite /import/geologs.db
docker compose up -d app
```

The copy preserves IDs and checks row counts in one transaction. Keep the SQLite file as a backup until you have inspected the new archive.

## Run locally without Docker

Requires Python 3.11+. SQLite uses only the Python standard library. For PostgreSQL, install `pip install -e '.[postgres]'` and set `GEOLOGS_DATABASE_URL` to a PostgreSQL connection URL. When that variable is set, PostGIS is used; otherwise the SQLite path below is used.

```bash
export GEOLOGS_DB="$PWD/geologs.db"
export GEOLOGS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./geologs serve
```

Open `http://127.0.0.1:8765` and enter the same `GEOLOGS_TOKEN` in the sign-in panel. The CLI also reads a `.env` file from its working directory for variables that are not already set in the process environment. Restart the server after changing `.env`; an already-running process keeps its original token. The server listens only on loopback by default. If you bind a different host, a token is required; put a TLS reverse proxy in front of it for access beyond your machine.

An optional `pip install -e .` provides the same `geologs` command globally or in a virtual environment.

Your SQLite file stays wherever `GEOLOGS_DB` points; the default is `~/.local/share/geologs/geologs.db`. PostgreSQL data stays in its configured database.

## Connect a tracker app

Phone apps can send locations straight to GeoLogs, so nothing has to poll. Each app posts in its own format to `/api/ingest/<format>`; **Settings → Connect a tracker app** shows the addresses for your server.

| App | Address | How it signs in |
| --- | --- | --- |
| [OwnTracks](https://owntracks.org/booklet/tech/http/) (iOS, Android), HTTP mode | `/api/ingest/owntracks` | Identification: any username, the token as password (HTTP Basic), and a device name |
| [Overland](https://github.com/aaronpk/Overland-iOS) (iOS) | `/api/ingest/overland?token=…` | Token in the receiver URL |
| Traccar Client, OsmAnd online tracking | `/api/ingest/osmand?token=…` | Token in the server URL |
| GPSLogger, Tasker, iOS Shortcuts, scripts | `/api/ingest/generic?token=…` | Bearer header, Basic password, or `?token=` |

Set `GEOLOGS_INGEST_TOKEN` in `.env` and give phones that token, not `GEOLOGS_TOKEN`: it can send locations (including `POST /api/points`) but cannot read history, change settings, or use MCP, so a lost phone or a logged URL exposes nothing. The main token works too. Compose reads both from the mounted `.env`, so a new ingest token works without a restart. Phones outside your network need a reachable address; use HTTPS through a reverse proxy, or a private network such as Tailscale, rather than exposing plain HTTP. Prefer the Basic or Bearer forms where an app offers them, since a proxy may log URLs.

Each fix keeps the time the phone recorded it (`tst` in OwnTracks), so fixes queued while the phone was offline land at the right time when they arrive, and retries are recognized as duplicates. Reminders fire only for fixes from the last 30 minutes, so a backlog delivered late does not announce arrivals that are long over. Sources are named after the app and device, such as `owntracks:karun/pixel` or `overland:iphone`; add `?source=` to choose a name. A malformed fix is skipped rather than rejected, because a queueing app would otherwise resend the same request forever; the generic format reports how many were skipped.

**Settings → Devices** shows whether each device and app is getting through. **Trackers** lists every source, most trusted first, with its health (live, quiet, or idle), its newest fix, when it last arrived, and its fixes in the last 24 hours; drag or use the arrows to set which one wins when they disagree, and remove a retired tracker with its data after a confirmation that says how many samples and saved summaries it affects. A failed or old device under a tracker app can be forgotten; that clears only its status, and it comes back if it sends again.

**Settings → Access → Device tokens** give each phone or app its own token for sending locations. Create one with a name, optionally locked to one tracker or to every tracker of an app (`home_assistant:`, `owntracks:`, …); it is shown once, and only its SHA-256 hash is stored. Each token shows when it was last used and as which tracker, and can be revoked on its own. A device token is accepted wherever the ingest token is (Bearer header, HTTP Basic password, or `?token=`) and cannot read anything or manage tokens; a locked token sending as another tracker is refused with `403`. `GEOLOGS_INGEST_TOKEN` in `.env` keeps working alongside and is listed as shared. Once any device token exists, sending needs a token even when no `.env` token is set. The Home Assistant steps can create a token locked to Home Assistant trackers and fill it into the `secrets.yaml` line. API: `GET`/`POST /api/ingest-tokens`, `POST /api/ingest-tokens/delete`. Each tracker app shows its status (receiving, quiet, failing, or how many devices are failing) and, under it, each device's latest result, such as “Rejected 5 min ago: wrong or missing token”. A rejected phone is still told apart by the name it sends (OwnTracks' user and device, or the `device`/`id` parameter); a request that names nothing is listed as an unidentified device. The same data is available at `GET /api/sources`. If the database cannot be reached, API requests answer `503` with the reason instead of dropping the connection, and the status indicator in the header shows it.

The generic format takes flat fields from a query string, a form, a JSON object, or a JSON array: `lat`/`latitude`, `lon`/`lng`/`longitude`, `timestamp` (epoch seconds or milliseconds, or ISO 8601 with a timezone), `accuracy`, `speed_mps` (or `speed`, in m/s) or `speed_kmh`, `device`, and optional `external_id`. The OsmAnd format reads `speed` in knots, as Traccar Client sends it, and also accepts Traccar Client 9's JSON body. OwnTracks payloads with an encryption key are not supported, and OwnTracks messages other than locations (region transitions, cards) are acknowledged and ignored.

## Connect Home Assistant

The easiest way is to let Home Assistant send each position the moment it changes. **Settings → Devices → Home Assistant** shows a `rest_command` to paste into `configuration.yaml` (with this server's address filled in) and a `secrets.yaml` line for your phone token, then an automation to paste into Home Assistant's YAML editor. Set **GeoLogs address, as Home Assistant reaches it** first: everything Home Assistant calls uses it, and a `localhost` address (which on Home Assistant's machine means Home Assistant itself) is flagged. Instead of pasting, **Import blueprint into Home Assistant** opens the GeoLogs blueprint through My Home Assistant (`/ha/geologs-location.yaml`, served without sign-in because Home Assistant downloads it; it holds no secrets); Home Assistant reports “Unknown error” when it cannot download it from that address. Positions arrive under the same names as the poller below (`home_assistant:device_tracker.…`), with Home Assistant's `last_updated` as the fix time and ID, so history continues and repeats are recognised.

### Or poll it with cron


Create a long-lived Home Assistant token from your profile's Security tab and identify a GPS `device_tracker` entity with `latitude` and `longitude` attributes. The poller uses the [Home Assistant state API](https://developers.home-assistant.io/docs/api/rest/) and records each successful poll as a sample, even when Home Assistant has not changed its state. Fill these fields in this project's `.env` file:

```dotenv
HA_URL=http://homeassistant.local:8123
HA_TOKEN=your-home-assistant-token
HA_ENTITY=device_tracker.my_phone
GEOLOGS_URL=http://127.0.0.1:8765
```

Use the port from `GEOLOGS_PUBLISHED_PORT` in `GEOLOGS_URL`. `GEOLOGS_TOKEN` is already in the same file. Check that the selected entity has current coordinates, then test one poll from the project directory:

```bash
./geologs poll-ha
```

For ongoing collection, run it every minute from cron or a systemd timer. A cron entry must change to this project directory so the CLI reads its `.env`; use absolute paths for both. Keep `.env` private because it contains both tokens. In a Docker setup, the host poller posts to the published app port; it does not need direct database access. A local non-Docker poller can also use `GEOLOGS_DATABASE_URL` or `GEOLOGS_DB` directly.

Home Assistant may update a tracker at a slower interval than cron. Each successful poll increments the raw sample count. The compact timeline keeps the first and latest sample at a stationary period and retains the intervening samples while moving. The `recorded` field in the poll result counts newly stored samples; `inserted` counts new source events, and `duplicates` counts repeated source events. Repeated polls do not trigger another arrival reminder. Each sample has its own speed estimate from the preceding recorded sample; unchanged coordinates are idle instead of inheriting an earlier moving speed. Speed derived from two positions is only an estimate; sparse GPS samples and location drift can misclassify activity. Only fixes within 30 minutes of the server clock can trigger reminders; historical CSV imports never trigger them.

## Ingest another source

Send one point or an array of up to 1,000 points to `POST /api/points`:

```bash
curl -X POST http://127.0.0.1:8765/api/points \
  -H "Authorization: Bearer $GEOLOGS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source":"my-script","external_id":"sample-123","timestamp":"2026-09-26T14:00:00Z","lat":41.88,"lon":-87.63,"accuracy_m":15,"speed_mps":0}'
```

Required fields: `lat`, `lon`. Use a timezone-aware ISO 8601 `timestamp` and stable `source`/`external_id` for reliable history and retries. `speed_mps` and `accuracy_m` are optional. The server can estimate speed if consecutive fixes are 5–900 seconds apart, both are reasonably accurate, and the implied speed is plausible. A single point with no speed is classified as `unknown`.

CSV files can contain `timestamp,lat,lon` plus optional `accuracy_m,speed_mps,external_id`:

```bash
./geologs import-csv locations.csv --source old-phone
```

Classic Google Takeout `Records.json` or `Location History.json` files with a top-level `locations` array can be imported without loading the whole file into memory:

```bash
./geologs import-takeout Records.json
```

The importer supports `latitudeE7`, `longitudeE7`, `timestamp` or `timestampMs`, and optional `accuracy`. It is safe to rerun overlapping exports. Modern semantic segment and `timelineObjects` exports need a separate parser and are not accepted by this command. Historical imports never trigger reminders or geocoding. After repairing imported data, use `docker compose exec app geologs rebuild-stays` for the Docker database, or `./geologs rebuild-stays` for a local SQLite archive.

## Observed stays and place patterns

The archive derives two kinds of stay incrementally from raw observations, including repeated Home Assistant polls. Both need at least 15 minutes of samples with accuracy no worse than 100 m.

-   A **stationary stay** keeps every sample within 50 m, with no gap above 30 minutes. A fix worse than 100 m accuracy neither confirms nor ends it, and up to two consecutive stray fixes within five minutes are ignored when the next fix is back inside, so indoor GPS jumps do not split a visit.
-   An **area stay** is time spent moving around one place: running laps of a lake, walking a park, or pottering through a large store. A window of travel counts when it fits inside a bounding box of up to 3 km (tightest first), contains no move at 25 km/h or faster, and circulates: its path must be at least 1.8 times its extent. One lap of a loop scores about 2.2, an out-and-back 2.0, and ordinary travel that passes through about 1.0. Everything between parking and driving away becomes one visit when it circulates as a whole, so an out-and-back trail run is not cut in half. An area stay records its activity (walking, running, or cycling), distance, and radius; a small area at walking pace or slower is a visit with no activity. A loop that starts and ends at the same stationary stay, such as a dog walk or a run from home, is a round trip rather than a visit.

Each stay records whether the travel since the stay before it went somewhere else: at least three accurate fixes, spanning four minutes or more, more than 150 m outside the corridor joining the two stays after allowing for each fix's accuracy. Consecutive stays at one place are one visit when that travel went nowhere else, so walking between two buildings of a campus, moving the car to another lot, or an indoor GPS jump appears as movement within the visit rather than a trip to the place you were already at. Leaving for lunch and coming back remains a round trip, labelled with where it went: its longest stop of two minutes or more (“stopped 9 min near Kroger”), or else how far out it went. Moves under 1 km inside one place are shown as walking unless the device reported a faster speed, because a phone that holds its position and then jumps makes a short walk look like a ride. A single ambiguous “ride” between two drives is shown as driving, since it is the car you arrived in. When several sources recorded the same day, the timeline takes the source ranked highest under **Settings → Devices → Tracker priority** (drag to reorder; unranked sources follow, busiest first) and lets each next one fill only the times those above it have no data, so it never places you in two spots. The current location is the newest fix, unless a higher-ranked tracker reported within 15 minutes of it. When the device recording a visit changes partway through (switching from the Home Assistant app to OwnTracks, say), stays from the two sources at the same place, under 30 minutes apart with no trip between, are one visit. Apps such as OwnTracks and Overland report only while you move, so silence is read as staying put: a quiet stretch of up to 16 hours that ends at the same spot is one visit, and one that ends elsewhere keeps you at the stay until about when you would have left to reach the next fix (its distance at 30 km/h, or the reported speed). Other unobserved stretches over 30 minutes appear in the timeline as “No location data”, such as a silence that ends hundreds of kilometres away. Existing observations are rebuilt automatically when the stay algorithm changes, so upgrading finds earlier lake runs without collecting more data. Repeated stops are grouped into frequent places. The **Places** page opens with a search for past visits (each result opens its day in History), then one card with three tabs: **Visited** (every place with a confirmed stay, by time spent, with a bar comparing each to your most-visited place), **Saved** (your zones, with **Add place**), and **Routines** (your usual routes between places and, once there are at least three departures from where you are, where you usually go next). In a busy place the nearest named place can be the wrong neighbour. **Edit place** on a map pin or in the day's Places tab corrects a place; **Fix this visit's name** on a selected stay corrects just that visit. Both list the named places around it, nearest first, and save the name you pick (or type) as a name correction, not a saved place. A correction saved without an address, such as one made through MCP with just a name, gets the street address of its spot, so visit search can still answer “what is Alex’s address?”; a named place nearby lends its address only when it lies inside the correction. Choose what it applies to. **Only this visit** (the default; “only the visits on” a day when fixing a pin) covers stays from that device that overlap the visit's time at that spot, so other visits there keep their own names, even after stays are recalculated. **Every visit to this spot** covers past and future visits within 50 m of a stop, or the area's extent plus a margin. A visit-only correction wins over an every-visit one for its visit, and both win over saved places and provider lookups, so history, daily summaries, and visit search use them; the current location uses a visit-only correction while you are still there, and name-only reminders use every-visit corrections. Fixing the same visit or spot again replaces its correction, and **Use automatic name** removes it. Saved places can be deleted from the Places page; one that a reminder still uses is hidden rather than removed, so the reminder keeps working. A saved place is a zone, not a name for everything inside it: save a university once and its buildings stay separate places (“Engineering Hall · In University”), with a walk between them shown as a trip. Inside a zone a visit is named after the named place right where you were, within 60 m or the stay's own extent, so a house is not named after a shop down the street; when nothing named is that close, the zone's name is used. Home is the exception and names everything inside it. Saved places, including Home, get visit counts and sampled time for everything inside them, so one afternoon on campus is one visit however many buildings it took in. Daily summaries list time per zone with the places in it, and visit search finds the places inside a zone by its name. These are lower bounds when GPS coverage is sparse.

The History page opens on the configured local day. `point_observations` is the append-only raw audit trail. `timeline_fixes` is the compact informational layer: movement samples remain, while a stationary run keeps its arrival and latest/departure edges. `observed_stays_v2` and place clusters are derived from raw observations. “Timeline fixes” shows key fixes out of raw samples, and the Samples tab can switch between both layers. Raw samples are fetched in 50-row pages. Samples, stays, common places, transitions, trips, and visited places can focus the map. The Day timeline lists stays and the trips between them in order; select one to highlight it on the map, where trips and area stays are coloured by mode. Map popups show each point's coordinates with a copy button. The lists below the map scroll within their own panel, and choosing a point there scrolls the page only when the map is off screen. A trip begins at the departure edge of one confirmed stay and ends at the arrival edge of the next confirmed stay from the same source. Traffic lights and short pauses therefore remain inside one trip. A trip that returns to where it started is kept as a round trip when it went at least 150 m beyond the starting place and covered at least 300 m; smaller excursions are GPS drift. Coverage gaps are kept as separate route lines instead of being joined across missing data. Modes are estimates from distance-weighted sampled speeds, with each move reduced by its fixes' reported accuracy, so one GPS jump cannot turn a walk into a drive; speeds computed from positions need at least 500 m before they can mean driving, while device-reported speeds are trusted over any distance: by default a median below 6.5 km/h is walking, below 15 km/h running, and below 25 km/h cycling. Faster medians, or at least a third of the distance at 35 km/h or more, are driving. All four speeds can be changed in **Settings → Stays & activities**. Running and cycling overlap between about 12 and 16 km/h, so a slow ride can read as a run. When the guess is wrong (slow traffic read as cycling, a bus read as driving), select the trip or area stay in the Day timeline and choose **Walk, Run, Ride, Drive, or Transit**; moves inside a visit can be changed the same way, and **Automatic** restores the guess. Transit is never guessed from speed. A correction is stored by source and time window, so it still applies to the trip that overlaps most of that window after trips are recalculated; it wins over the speed and context rules, and totals, daily summaries, and MCP tools (`set_travel_mode`) use it. When Home Assistant repeats a position for several polls, a move is timed from the first poll of that position, which keeps a run from looking like a drive. A past day's address includes `#date=YYYY-MM-DD`, so it can be bookmarked, and the arrow keys move between days. Below the map, the day's details have four tabs: **Places** (each place that day, to show on the map or rename), **Speed** (the whole day on a time axis, coloured by travel mode, with stops drawn flat and missing coverage shaded), **Samples** (key fixes or raw samples, each with what it was part of), where a bad fix can be deleted, singly or by ticking several, after a confirmation that names the device, the time span, the days whose summaries are rebuilt, and any reminder it set off, and **Data** (which devices recorded the day, how many fixes each sent, and any gaps). A day with nothing recorded shows that plainly, with a way to the latest day that has data.

The map applies local interpolation that passes through every included GPS fix and never sends coordinates to a routing provider. Sources are routed independently, so interleaved devices no longer break one another's paths. Fixes with accuracy worse than 150 m are omitted; gaps over 15 minutes and jumps implying more than 200 km/h remain disconnected. Stored speed does not decide whether a fix is drawn. The map does not know the road graph, so sparse coordinates can produce a line across a lake or away from a road; the line is a recorded GPS estimate rather than turn-by-turn road matching. The Places page lists saved places first and observed places in a second tab, ranked by total observed time and visit count. Stop clusters that resolve to the same provider place ID are combined; equal normalized place names within 2.5 km are also combined so a large park or lake is listed once without merging distant chain locations. A place with no name nearby is known by its street address, and indoor GPS can drift 100 m or more between visits, picking a different house number each time; such address-only places on the same street within 200 m are combined under the address where you spent the most time. Named places never merge this way. Place categories select local icons for common types such as shops, salons, water, universities, bars, parks, food, and healthcare. A saved place named `Home` or `My Home` is omitted from visited-place lists and dashboard counts, while it remains available in saved places, stays, and persisted daily summaries.

The Settings page provides an IANA timezone dropdown and controls speed handling for newly received samples. Settings is split into pages: **Devices**, **Stays & stops**, **Movement**, **Place names**, **Days & summaries**, **Appearance**, and **Access**. **Appearance** chooses light, dark, or matching the system for this browser; the sun or moon in the top bar switches between light and dark too. **Stays & stops** sets the minimum stay (15 minutes by default, 5–120) and quick stops; **Movement** sets the activity speeds above. Saving either recalculates stays and trips across the whole history, marks stored daily summaries out of date, and keeps travel modes you corrected by hand.

A quick stop is a shorter stop on the way somewhere, such as filling up: still for at least the quick-stop time (3 minutes by default, or off), with every fix within 30 m and accurate to 50 m, entered and left while tracked (no gap over 5 minutes on either side). Stillness must come from fresh fixes: a Home Assistant state held for several polls can hide driving, so only the time until its last new state counts. Creeping traffic moves too far, red lights are usually too short, and a pause during a run around one place stays part of that visit. A quick stop splits the trip around it and counts as a visit. A sparse app can go quiet at the pump: a quiet stretch of two minutes or more that ends within the stop radius (plus accuracy) too slowly to be travel counts as being still. A stop's spread is measured from its centre, less each fix's accuracy, because its first fix is often you still rolling in; frequent fixes that keep heading one way (40 m or more along a fairly straight path) are creeping traffic. A quick stop with fixes at least every 90 seconds needs nothing more; a sparser one is kept only where there is a place to have stopped at: a saved place, a name you gave the spot, or a named place within 75 m (looked up when a day is opened, read from the lookup cache elsewhere). With no place there, it was probably the road or a red light, so the day timeline shows it as a **Possible stop** with **Keep**. A stray fix only holds a stay open while its accuracy can explain the jump (150 m); a sharp fix farther away is a departure. When the detector gets it wrong, use **Add a stay** at the top of the day timeline (it suggests the stillest stretch not already a stay), **Add a stop on this trip** on a selected trip, or **Make a stay** from samples ticked in the Samples tab; select a stay to remove it. These changes are stored by source and time window (`stay_edits`), survive recalculation, and are listed with **Undo** in the day's Data tab; `GET`/`POST /api/stay-edits` and `POST /api/stay-edits/delete` do the same. The default speed behavior uses source-reported speed when present and calculates from consecutive coordinates when it is missing. “Always calculate” ignores reported speed. A source-speed adjustment in km/h is applied only when reported speed is enabled; negative values correct a source that consistently reads high. The built-in Home Assistant poller currently sends coordinates and accuracy without speed, so values marked `est.` are calculated by GeoLogs and are unaffected by the source-speed adjustment.

Raw storage and arrival reminders happen immediately. Stay analysis processes only each source's new tail and does not expose a stay until nearby samples span the minimum stay (15 minutes by default). The first request for a day's summary calculates its stay-anchored trips and routes, then saves the complete result with a raw-data watermark. Repeated requests, including requests after a server restart, read that result from the database. A new raw sample changes the watermark; saved-place edits, algorithm upgrades, and explicit analysis rebuilds clear or version the cache. Place patterns are derived from confirmed stays when requested. Waiting for 100 samples would add about 100 minutes of lag to a one-minute Home Assistant poll without adding a time-based confidence signal, so analysis is incremental rather than gated by an arbitrary point count.

After midnight, the server stores the completed previous day in `daily_summaries`. **Settings → Daily summaries** can also save today so far every 1–12 hours, so MCP tools can read the day before it ends; opening a day always brings its record up to date. A day on which nothing was recorded is never stored: asking for it returns an empty summary, and the Summaries page says nothing was recorded. The catalog keeps one record per date: the configured timezone across all sources. An overview requested for another timezone or a single source is computed but not stored, and changing the timezone in Settings drops records stored for the old one; they are rebuilt when requested. Opening a day on the **Summaries** page builds or refreshes its record whatever the schedule (the tab opens on today, or the latest saved day when nothing was recorded today). It lists every stored day and shows exactly what is stored for it, both readable and as raw JSON, notes when the day's data changed after it was stored, and can refresh it or build the record for any day with data (`GET /api/daily-summary/stored?date=` reads without rebuilding). The record contains a compact overview, places and observed time including Home, trips and distances, activity totals, fix counts, and reminders created, triggered, cancelled, or still active at day end. Late data updates the record through its input watermark. `get_daily_overview` creates or refreshes any requested historical day, while `list_daily_overviews` returns the indexed catalog and totals.

Each stored record also keeps a **brief**: the same day as a small JSON of plain-text lines, for an LLM to read at a fraction of the tokens (about a sixth of the full record). It has the date with its timezone, a one-line summary (places, trips, distance, and time per activity), a chronological timeline of stays, trips, and data gaps (“08:30-08:42 drove 9.6 km from Home to Lake Harriet”, “08:42-09:42 at Lake Harriet, ran 9.0 km”), places with time spent, and the reminders set and triggered. When an observed return Home occurs before 06:00 after being away across midnight, both adjoining days explicitly note it: the evening says it happened the next day, and the morning says it happened after midnight. This is a return-home fact, not a claim about where someone slept or why they were away. It states only what happened: no ids, coordinates, seconds, false flags, or empty lists, and no reminders still pending. Times are local `HH:MM`; a stay that crosses midnight reads “until 07:59” or “from 21:40”, and the latest stay says when it was last seen (“still there at 23:59”). The Summaries page shows the stored brief with its approximate size next to the full record, and a button to copy it. `GET /api/daily-brief?date=&end_date=` and the MCP tool `get_daily_brief` return briefs for up to 31 consecutive days, building any that are missing or out of date.

Consecutive stays create transition counts. A next-destination estimate needs at least three observed departures and recent evidence; older transitions receive less weight. The current-location tool reports fix age and whether it is stale. Day summaries accept an IANA timezone and include a coverage note. No place or person is inferred from an IP address.

## Reminders and notifications

Create a reminder at a saved place, search for another place near the latest fix, or enter destination coordinates and a 20–5,000 m radius. An unsaved reminder destination remains attached to that reminder and does not appear under Saved Places. A reminder can also name a kind of place instead of one location: **Any place named “Starbucks”** fires the first time you have been still for about three minutes within 150 m of a place whose name matches, anywhere. Every named place in that radius is checked, not just the nearest, and each query word must start a word of the name or be a close spelling of one (“starbuks” matches “Starbucks Coffee”). The places around a stop are looked up once per stop, so this costs one cached nearby-place lookup per stop of three minutes or more, and only while a name reminder is active. The number of provider requests behind that lookup depends on the selected provider. A new point inside after an outside or stale previous fix creates an event and completes the reminder. Active reminders can be cancelled; triggered and cancelled reminders remain as passive history and cannot be re-enabled. Low accuracy readings larger than the geofence radius or 150 m are ignored for triggering.

The browser's arrival inbox persists events. Browser notifications work while that tab is open and permission is granted. For alerts when the browser is closed, set `GEOLOGS_WEBHOOK_URL` to an HTTPS endpoint (or loopback HTTP). Each event is POSTed as JSON; `GEOLOGS_WEBHOOK_TOKEN` adds a bearer header. This can feed a Home Assistant automation, `ntfy`, or your own notifier. Failed webhook attempts remain marked in the event log; automatic retries are not implemented yet.

## Geocoding

-   Set `GEOAPIFY_API_KEY` to enable place/address search and coordinate labeling. [Geoapify's free plan](https://www.geoapify.com/pricing/) currently lists 3,000 credits/day; creating an API key requires an account. Place and address search looks within 100 km of your latest fix first and then everywhere, so a match across town is listed before an exact name on another continent; every result shows its distance from you. A place is named after the nearest named place to its position: the sample-weighted average of its stays, each of which is already the average of its fixes. GeoLogs asks the [Places API](https://apidocs.geoapify.com/docs/places/) for named places of visitable types (shops, food, education, parks, water, healthcare, and similar; not roads, parking, transit stops, or ATMs) inside a circle around that position, ordered by distance, and uses the first. The circle is 150 m, which reaches a big-box store from its parking lot, or the smallest of 300, 600, or 1,000 m that covers an area stay's extent. When nothing named is inside it, Reverse Geocoding supplies the street address. A lookup makes two requests of up to 20 places each, which costs two credits: Geoapify cannot combine accommodation, airport, beach, national park, sport, and tourism categories with the others in one request (adding any of them made it return none of the rest), so those are asked for separately and the nearest named place of both is used. Home and name corrections always take precedence, including for a round trip's stop; inside another saved place, the zone names a spot only when no named place is right there. Raw GPS fixes are never automatically geocoded. Nearest-place naming can pick a small shop next to a large venue when the venue's reference point is farther away; correct the visit's name (every visit to this spot) to fix it.
-   Set `MAPBOX_ACCESS_TOKEN` to use [Mapbox's Search Box API](https://docs.mapbox.com/api/search/search-box/). Automatic place names come from its reverse lookup restricted to points of interest (`types=poi`, up to 10 results). That endpoint takes no radius, so GeoLogs measures each result's distance and uses the nearest named one inside the same search circle, skipping ATMs, parking, and transit stops. When none is inside, a second lookup with `types=address,street` supplies the address. The nearby-place picker uses the separate category-search endpoint across 13 visit-relevant category groups, merges it with exact reverse hits, removes duplicate Mapbox IDs, and enforces the requested circle locally. This can make up to 14 Mapbox requests on an uncached nearby lookup; results are region-cached. Place search uses `/suggest` with `proximity`, which finds shops and venues by name near you (the `/forward` endpoint read “Tea Capital” as tea shops in Kuwait's Capital governorate). Suggestions carry no coordinates, so GeoLogs fetches them with `/retrieve`, in the same session, only for the result you pick. The [free tier](https://www.mapbox.com/pricing) currently covers 50,000 Search Box requests a month. **Mapbox allows Search Box results for temporary use only; GeoLogs caches and stores place names (lookup cache, saved places, daily summaries), which goes beyond those terms.** Settings shows this notice when Mapbox is selected.
-   Set `GEOLOGS_PHOTON_URL` to use your own [Photon](https://github.com/komoot/photon/blob/master/docs/api-v1.md) instance for forward/reverse geocoding, with the same search radius and the nearest named result that is not a road or boundary.

When more than one provider is configured, choose one under **Settings → Place names & search**. Automatic uses the first configured of Photon, Geoapify, and Mapbox, which keeps a self-hosted Photon in charge as before. A provider without its key cannot be selected; if a selected provider's key is later removed, Automatic's choice is used. Each provider has its own lookup cache, and day summaries are keyed by provider, so switching looks names up again as days are viewed.

Saved places, raw paths, trip estimates, reminders, and MCP history work without any provider key. The browser loads Leaflet from its CDN and OpenStreetMap map tiles, so map browsing sends viewport/tile requests to those services. It does not send your stored GPS history to the tile service.

Nearest-place lookups and reverse geocoding share an indexed region cache, kept separately for each search radius. A named place, precise building, or amenity is reused within 15 m, and a street result within 30 m. The spatial index narrows candidates; an exact distance check confirms each hit. These small circles are heuristics, not verified parcel boundaries, so reused results are marked `approximate`. City and other broad results are cached at the requested point only because their actual boundaries are unknown. Cache entries expire after 180 days by default (`GEOLOGS_REVERSE_CACHE_DAYS`). The returned coordinates remain the coordinates you requested, even when the label came from a nearby cache entry. This avoids repeated provider calls without assigning one place to a broad area.

PostGIS stores point and reverse-cache geography under GiST indexes and uses `ST_DWithin` for nearby searches. A city bounding box alone is not a reliable address boundary. The [PostGIS migration notes](docs/postgis-migration.md) describe what is implemented and what still needs scale testing.

Time and source indexes cover raw observations, canonical points, and stays. GiST indexes cover point, saved-place, and reverse-cache geography. Reminder state and trigger time have dedicated indexes, and daily summaries have an indexed date/timezone key. Daily overview lookup is exact by date, timezone, and source, so pgvector is intentionally not required.

## Finding visits

Questions such as “what day did I go to Costco?” are answered by `search_visits` (MCP) and `GET /api/visits?q=costco`, and by **Find a visit** on the Places page. The search matches every query word as a word prefix against each observed place's name, address, and category, then returns matching places and each visit's local date, weekday, arrival, departure, and duration, newest first. `sams club` finds “Sam's Club”, `gas` finds “Costco Gasoline”, and `supermarket` finds places in the `commercial.supermarket` category.

pgvector embeddings are deliberately not used for this. Place names are short proper nouns, and semantic similarity would rank Sam's Club or Walmart close to Costco, which is a wrong answer to a factual question. Embeddings would also need a model and would send place names to it. The agent already supplies the semantic step: when “the warehouse store” finds nothing, it can retry with `costco` or a category such as `supermarket`. A vector column would become useful only for a separate semantic search feature over narrative summaries.

## MCP

The running server exposes MCP at `http://127.0.0.1:8765/mcp` using stateless Streamable HTTP. Start it with `docker compose up --build -d` or `./geologs serve`, then point an MCP client at that URL. For clients that accept a URL and headers in JSON, the connection looks like:

```json
{
  "mcpServers": {
    "geologs": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer YOUR_GEOLOGS_TOKEN"}
    }
  }
}
```

Use the token from `GEOLOGS_TOKEN` when one is configured. The endpoint accepts JSON-RPC messages at `POST /mcp`; clients must send `Content-Type: application/json` and `Accept: application/json, text/event-stream`. It responds with JSON for requests and `202 Accepted` for notifications. This server does not issue session IDs or provide a standalone event stream, so `GET /mcp` returns `405 Method Not Allowed`. Available tools cover current/raw/compact history, nearby/time lookup, recorded trips, stats, coverage, observed stays and places, visit search by place name or category, saved place visits and observed time, detailed day data, persisted daily overviews and their plain-text briefs, transitions, next-destination estimates, one-shot reminders and their history, ingestion, geocoding, and reverse geocoding. No raw SQL execution is exposed.

### Hermes Agent

Put the GeoLogs token in `~/.hermes/.env`:

```dotenv
GEOLOGS_MCP_TOKEN=the-value-of-GEOLOGS_TOKEN
```

Add the Streamable HTTP server to `~/.hermes/config.yaml`. Use the published port from this project's `.env`:

```yaml
mcp_servers:
  geologs:
    url: "http://127.0.0.1:8765/mcp"
    headers:
      Authorization: "Bearer ${GEOLOGS_MCP_TOKEN}"
```

Run `hermes mcp test geologs`, then start a new chat or use `/reload-mcp`. Hermes registers tools with names such as `mcp_geologs_create_reminder`. Ask naturally: “Using GeoLogs, remind me to pick up salt the next time I arrive at Walmart.” Hermes can use a saved place ID or pass a place name and coordinates directly without saving the destination. GeoLogs evaluates the arrival from incoming Home Assistant samples; Hermes does not need to remain in the chat.

The MCP connection lets Hermes manage reminders, but Streamable HTTP does not push a triggered event into an idle Hermes session. To deliver the event through a configured Hermes channel, enable the Hermes webhook gateway and create a direct-delivery route:

```bash
hermes gateway setup
hermes webhook subscribe geologs-reminders \
  --deliver telegram \
  --deliver-only \
  --prompt "Location reminder: {text} — arrived at {place}" \
  --description "Deliver GeoLogs arrival reminders"
```

Replace `telegram` with the Hermes delivery platform you use. Copy the URL and HMAC secret printed by Hermes into the GeoLogs `.env`. When GeoLogs runs in Docker and Hermes runs on the host, use the host alias in the URL:

```dotenv
GEOLOGS_WEBHOOK_URL=http://host.docker.internal:8644/webhooks/geologs-reminders
GEOLOGS_WEBHOOK_SECRET=the-secret-printed-by-hermes
```

Run `docker compose up -d --force-recreate app` after changing these values. GeoLogs signs webhook requests with Hermes's replay-protected V2 HMAC headers. `GEOLOGS_WEBHOOK_TOKEN` remains available for non-Hermes endpoints that use bearer authentication.

## API overview

`GET /api/current`, `/api/points`, `/api/observations`, `/api/timeline`, `/api/settings`, `/api/stats`, `/api/trips`, `/api/coverage`, `/api/near`, `/api/stays`, `/api/frequent-places`, `/api/visited-places`, `/api/visits`, `/api/transitions`, `/api/predict-next`, `/api/insights`, `/api/day`, `/api/daily-summary`, `/api/daily-summaries`, `/api/daily-brief`, `/api/sources`, `/api/places`, `/api/place-visits`, `/api/reminders`, `/api/reminder-history`, `/api/events`, `/api/geocode`, `/api/reverse`, `/api/export.geojson`; `GET` or `POST /api/ingest/{owntracks,overland,osmand,generic}`; `POST /api/points`, `/api/observations/delete` (`{ids}` previews, `{ids, confirm: true}` deletes), `/api/settings`, `/api/places`, `/api/reminders`, `/api/reminders/cancel`, `/mcp`. `/api/observations` accepts `limit` and `offset` for raw-data pagination.

All data `/api/*` routes and `/mcp` require authentication when a token is configured. API clients can use the bearer token; the browser exchanges the token at `POST /api/session` for an HttpOnly session cookie. Requests from a different browser origin are rejected. The API has no account or multiuser support; run one archive per person.

## Next features

Good next additions are: PostGIS scale and concurrency testing; semantic Google Timeline and OwnTracks imports; home periods and city/country trip summaries; dwell/leave geofences; smarter activity detection using sensor data; geofence hysteresis and accuracy confidence; provider quotas and bulk enrichment jobs; configurable raw retention and privacy zones; optional offline road-graph map matching; and a background webhook retry queue.

## Browser app structure

The browser app is plain HTML, CSS, and native ES modules served from `src/geologs/web`, with no build step:

```text
web/
  index.html            page markup; loads the stylesheets in cascade order and js/main.js
  css/
    tokens.css          colours, type, radii, shadows, and sizes for light and dark themes
    base.css            element defaults and utilities
    layout.css          app shell: top bar, navigation (a bottom tab bar on phones), page headers
    components/         buttons, forms, cards, lists, tabs, chips, dialogs
    map.css             Leaflet styling
    pages/              history, places, reminders, summaries, settings
  js/
    main.js             entry point: wires each view, then loads data and keeps it fresh
    core/               DOM helpers, shared state, formatting, and the API client
    ui/icons.js         travel-mode and place icons, source names, status pills
    map/map.js          everything drawn on the Leaflet map
    views/              one module per page, plus the timeline, place dialogs, sign-in, and page switching
    app/refresh.js      loading data for every view and polling for arrival reminders
```

Each view exports an `init…()` function that `main.js` calls once to attach its event handlers, so modules never act on each other while they load. Responsive rules live in the same file as the component they adjust. The server serves `index.html` and anything under `/css/` and `/js/` (only `.css`, `.js`, and `.html`), with ETags so unchanged files are answered with `304`.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
for file in $(find src/geologs/web/js -name '*.js'); do node --check "$file"; done
```

The Python tests also check that every file `index.html` loads is served, and that every module import names something its target exports.
