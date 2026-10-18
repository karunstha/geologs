# Connecting trackers and importing data

## Connect a tracker app

Phone apps can send locations straight to GeoLogs, so nothing has to poll. Each app posts in its own format to `/api/ingest/<format>`; **Settings → Connect a tracker app** shows the addresses for your server.

| App | Address | How it signs in |
| --- | --- | --- |
| [OwnTracks](https://owntracks.org/booklet/tech/http/) (iOS, Android), HTTP mode | `/api/ingest/owntracks` | Identification: any username, the token as password (HTTP Basic), and a device name |
| [Overland](https://github.com/aaronpk/Overland-iOS) (iOS) | `/api/ingest/overland?token=…` | Token in the receiver URL |
| Traccar Client, OsmAnd online tracking | `/api/ingest/osmand?token=…` | Token in the server URL |
| GPSLogger, Tasker, iOS Shortcuts, scripts | `/api/ingest/generic?token=…` | Bearer header, Basic password, or `?token=` |

Set `GEOLOGS_INGEST_TOKEN` in `.env` and give phones that token, not `GEOLOGS_TOKEN`: it can send locations (including `POST /api/points`) but cannot read history, change settings, or use MCP, so a lost phone or a logged URL exposes nothing. The main token works too. Compose reads both from the mounted `.env`, so a new ingest token works without a restart. Phones outside your network need a reachable address; use HTTPS through a reverse proxy, or a private network such as Tailscale, rather than exposing plain HTTP. Prefer the Basic or Bearer forms where an app offers them, since a proxy may log URLs.

Each fix keeps the time the phone recorded it (`tst` in OwnTracks), so fixes queued while the phone was offline land at the right time when they arrive, and retries are recognized as duplicates. Reminders fire only for fixes from the last 30 minutes, so a backlog delivered late does not announce arrivals that are long over. Sources are named after the app and device, such as `owntracks:alex/pixel` or `overland:iphone`; add `?source=` to choose a name. A malformed fix is skipped rather than rejected, because a queueing app would otherwise resend the same request forever; the generic format reports how many were skipped.

**Settings → Devices** shows whether each device and app is getting through. **Trackers** lists every source, most trusted first, with its health (live, quiet, or idle), its newest fix, when it last arrived, and its fixes in the last 24 hours; drag or use the arrows to set which one wins when they disagree, and remove a retired tracker with its data after a confirmation that says how many samples and saved summaries it affects. A failed or old device under a tracker app can be forgotten; that clears only its status, and it comes back if it sends again.

**Settings → Access → Device tokens** give each phone or app its own token for sending locations. Create one with a name, optionally locked to one tracker or to every tracker of an app (`home_assistant:`, `owntracks:`, …); it is shown once, and only its SHA-256 hash is stored. Each token shows when it was last used and as which tracker, and can be revoked on its own. A device token is accepted wherever the ingest token is (Bearer header, HTTP Basic password, or `?token=`) and cannot read anything or manage tokens; a locked token sending as another tracker is refused with `403`. `GEOLOGS_INGEST_TOKEN` in `.env` keeps working alongside and is listed as shared. Once any device token exists, sending needs a token even when no `.env` token is set. The Home Assistant steps can create a token locked to Home Assistant trackers and fill it into the `secrets.yaml` line. API: `GET`/`POST /api/ingest-tokens`, `POST /api/ingest-tokens/delete`. Each tracker app shows its status (receiving, quiet, failing, or how many devices are failing) and, under it, each device's latest result, such as “Rejected 5 min ago: wrong or missing token”. A rejected phone is still told apart by the name it sends (OwnTracks' user and device, or the `device`/`id` parameter); a request that names nothing is listed as an unidentified device. The same data is available at `GET /api/sources`. If the database cannot be reached, API requests answer `503` with the reason instead of dropping the connection, and the status indicator in the header shows it.

The generic format takes flat fields from a query string, a form, a JSON object, or a JSON array: `lat`/`latitude`, `lon`/`lng`/`longitude`, `timestamp` (epoch seconds or milliseconds, or ISO 8601 with a timezone), `accuracy`, `speed_mps` (or `speed`, in m/s) or `speed_kmh`, `device`, and optional `external_id`. The OsmAnd format reads `speed` in knots, as Traccar Client sends it, and also accepts Traccar Client 9's JSON body. OwnTracks payloads with an encryption key are not supported, and OwnTracks messages other than locations (region transitions, cards) are acknowledged and ignored.

## Connect Home Assistant

Home Assistant sends each position the moment it changes. **Settings → Devices → Home Assistant** shows a `rest_command` to paste into `configuration.yaml` (with this server's address filled in) and a `secrets.yaml` line for your phone token, then an automation to paste into Home Assistant's YAML editor. Set **GeoLogs address, as Home Assistant reaches it** first: everything Home Assistant calls uses it, and a `localhost` address (which on Home Assistant's machine means Home Assistant itself) is flagged. Instead of pasting, **Import blueprint into Home Assistant** opens the GeoLogs blueprint through My Home Assistant (`/ha/geologs-location.yaml`, served without sign-in because Home Assistant downloads it; it holds no secrets); Home Assistant reports “Unknown error” when it cannot download it from that address. Positions arrive as `home_assistant:device_tracker.…`, with Home Assistant's `last_updated` as the fix time and ID, so repeats are recognised.

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

The importer supports `latitudeE7`, `longitudeE7`, `timestamp` or `timestampMs`, and optional `accuracy`. It is safe to rerun overlapping exports. Modern semantic segment and `timelineObjects` exports need a separate parser and are not accepted by this command. Historical imports never trigger reminders or geocoding. After repairing imported data, run `docker compose exec app geologs rebuild-stays` (add `--user EMAIL` for someone other than the owner).
