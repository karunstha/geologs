# MCP and the API

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

## Reminder webhooks

MCP lets an assistant create and manage reminders, but it cannot push a triggered reminder into an idle chat. To be told when one fires, set `GEOLOGS_WEBHOOK_URL` to an HTTPS endpoint (or loopback HTTP) that delivers messages: a notifier, a chat bot, or an automation. Each fired reminder is POSTed as JSON.

-   `GEOLOGS_WEBHOOK_TOKEN` adds an `Authorization: Bearer` header.
-   `GEOLOGS_WEBHOOK_SECRET` signs the body: the request carries `X-Webhook-Timestamp` (Unix seconds) and `X-Webhook-Signature-V2`, the hex HMAC-SHA256 of `timestamp.body` keyed with the secret, so a receiver can reject forged and replayed requests.
-   When GeoLogs runs in Docker and the receiver runs on the host, use `host.docker.internal` in the URL.
-   Only the owner's reminders are sent: the webhook belongs to the server's owner, not to other users.

Run `docker compose up -d --force-recreate app` after changing these values. A failed delivery is recorded on the event.

## API overview

`GET /api/current`, `/api/points`, `/api/observations`, `/api/timeline`, `/api/settings`, `/api/stats`, `/api/trips`, `/api/coverage`, `/api/near`, `/api/stays`, `/api/frequent-places`, `/api/visited-places`, `/api/visits`, `/api/transitions`, `/api/predict-next`, `/api/insights`, `/api/day`, `/api/daily-summary`, `/api/daily-summaries`, `/api/period-summary`, `/api/daily-brief`, `/api/route-map`, `/api/sources`, `/api/places`, `/api/place-visits`, `/api/reminders`, `/api/reminder-history`, `/api/events`, `/api/geocode`, `/api/reverse`, `/api/export.geojson`; `GET` or `POST /api/ingest/{owntracks,overland,osmand,generic}`; `POST /api/points`, `/api/observations/delete` (`{ids}` previews, `{ids, confirm: true}` deletes), `/api/settings`, `/api/places`, `/api/reminders`, `/api/reminders/cancel`, `/api/reminders/delete` (one ended reminder by `id`), `/api/reminders/clear-history`, `/mcp`. `/api/observations` accepts `limit` and `offset` for raw-data pagination.

All data `/api/*` routes and `/mcp` require authentication when a token is configured: a personal API token or `GEOLOGS_TOKEN` (the owner) as a bearer, or a browser session. The browser signs in at `POST /api/session` with an email and password (or the owner's `GEOLOGS_TOKEN`) and gets an HttpOnly session cookie; `DELETE /api/session` signs it out, and `GET /api/me` says who is signed in. Requests from a different browser origin are rejected. See **Accounts** for users.
