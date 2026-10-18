# Configuration

GeoLogs reads its settings from environment variables. With Docker Compose they come from `.env` (copy `.env.example`); `geologs serve` also reads a `.env` in its working directory for any variable the environment does not already set. Values are read when needed, so a token in a mounted `.env` file takes effect without a restart; after changing Compose settings, run `docker compose up -d --force-recreate app`.

## Required

| Variable | What it is |
| --- | --- |
| `POSTGRES_PASSWORD` | The database owner's password. The owner creates and upgrades the tables when the server starts. |
| `GEOLOGS_APP_DB_PASSWORD` | The password of `geologs_app`, the login every request uses. It can only see each user's own rows. Use a different value from the one above. |
| `GEOLOGS_TOKEN` | The server's own key. It signs in as the owner (the first account, an admin), and works as a bearer token for the API and MCP. |

Generate each with `openssl rand -base64 32 | tr -d '/+='`.

## Server

| Variable | Default | What it is |
| --- | --- | --- |
| `GEOLOGS_DATABASE_URL` | | The PostgreSQL/PostGIS database, as its owner. Compose sets it. |
| `GEOLOGS_TIMEZONE` | `UTC` | The timezone new users start with (an IANA name such as `America/Chicago`); each user changes theirs in Settings. |
| `GEOLOGS_PUBLISHED_PORT` | `8765` | The port Compose publishes the app on. |
| `GEOLOGS_BIND` | `127.0.0.1` | Which address that port is published on. The default reaches the app from this machine only (and from a proxy or tunnel running on it). Set `0.0.0.0` to reach it from other devices on your network, over plain HTTP. |
| `GEOLOGS_HOST`, `GEOLOGS_PORT` | `127.0.0.1`, `8765` | Where `geologs serve` listens. Beyond loopback, `GEOLOGS_TOKEN` is required. |
| `GEOLOGS_DB_POOL_SIZE` | `10` | Database connections kept for requests (2–100). A request waits up to 10 seconds for one, then gets a 503. |
| `GEOLOGS_PUBLIC_URL` | | Where people open GeoLogs (`https://geologs.example.com`). Links in emails point here. |
| `GEOLOGS_TOKEN_FILE` | | A file holding `GEOLOGS_TOKEN` (and `GEOLOGS_INGEST_TOKEN`), read on every request. Compose mounts `.env` this way. |
| `GEOLOGS_ENV_FILE` | `.env` | Which file the command line reads. |

## Trackers

| Variable | What it is |
| --- | --- |
| `GEOLOGS_INGEST_TOKEN` | A token phones use to send locations. It cannot read anything, so a lost phone exposes nothing. Per-device tokens made in Settings are better; see [Connecting trackers](trackers.md). |

## Email (optional)

Needed for invites and password resets by email. Without it, links are shown to the admin to pass on. See [Accounts](accounts.md#email).

| Variable | Default | What it is |
| --- | --- | --- |
| `GEOLOGS_SMTP_HOST` | | The mail server. |
| `GEOLOGS_SMTP_PORT` | 587 (465 for `ssl`, 25 for `none`) | Its port. |
| `GEOLOGS_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none`. |
| `GEOLOGS_SMTP_USERNAME`, `GEOLOGS_SMTP_PASSWORD` | | Its login, if it needs one. |
| `GEOLOGS_SMTP_FROM` | | The sender, such as `GeoLogs <geologs@example.com>`. |

## Outside services (all optional)

Coordinates or place names leave your server only for the services you turn on; see [Privacy](privacy.md).

| Variable | What it is |
| --- | --- |
| `GEOAPIFY_API_KEY` | Place search and naming through [Geoapify](https://www.geoapify.com/). |
| `MAPBOX_ACCESS_TOKEN` | The same through [Mapbox](https://docs.mapbox.com/api/search/search-box/). |
| `GEOLOGS_PHOTON_URL` | The same through your own [Photon](https://github.com/komoot/photon) server. |
| `GEOLOGS_REVERSE_CACHE_DAYS` | How long a place lookup is reused (1–3650, default 180). |
| `UNSPLASH_ACCESS_KEY` | A photo of the town behind Home. |
| `EIA_API_KEY` | US fuel prices for fuel costs. |

## Reminder webhook (optional)

See [MCP and the API](mcp-and-api.md#reminder-webhooks).

| Variable | What it is |
| --- | --- |
| `GEOLOGS_WEBHOOK_URL` | Where fired reminders are POSTed (HTTPS, or HTTP to this machine). |
| `GEOLOGS_WEBHOOK_TOKEN` | Sent as a bearer token. |
| `GEOLOGS_WEBHOOK_SECRET` | Signs each request body. |
