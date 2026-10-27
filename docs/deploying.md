# Deploying GeoLogs

## With Docker Compose

```bash
git clone https://github.com/karunstha/geologs.git && cd geologs
cp .env.example .env     # set POSTGRES_PASSWORD, GEOLOGS_APP_DB_PASSWORD and GEOLOGS_TOKEN
docker compose up --build -d
```

Open `http://127.0.0.1:8765`, choose **Sign in with the server's access token**, and enter `GEOLOGS_TOKEN`. Then open the account button (top right) to add your email and a password, so you can sign in without the token. See [Configuration](configuration.md) for every setting.

-   The app listens on this machine only (`127.0.0.1`); see below for reaching it from other devices.
-   The database lives in `./data/postgres` (not in Git); include it in your backups.
-   `postgis/postgis` is published for amd64 only, so on ARM hosts the compose file pins the database to `linux/amd64` and Docker emulates it.
-   The app container is read-only and runs as an unprivileged user. Compose mounts `.env` read-only so the token is read on each request.

## Running without Docker

Requires Python 3.11+ and a PostgreSQL database with PostGIS.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export GEOLOGS_DATABASE_URL="postgresql://geologs:PASSWORD@127.0.0.1:5432/geologs"
export GEOLOGS_APP_DB_PASSWORD="…"      # the app's own login; created at start-up
export GEOLOGS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/geologs serve
```

The server listens on loopback by default. The `./geologs` script in a checkout runs the same command without installing the package.

## Putting it on the internet

Phones, other devices and other people need an address that works from outside your network. Use HTTPS in front of GeoLogs: a reverse proxy (Caddy, nginx, Traefik), a tunnel such as Cloudflare Tunnel, or a private network such as Tailscale. Never expose plain HTTP: tokens and passwords cross it.

-   Set `GEOLOGS_PUBLIC_URL` to the public address.
-   Compose publishes the app's port, and the database's, on `127.0.0.1` only, so a proxy on the same machine reaches the app and nothing else on the network can reach either. To reach the app directly from other devices (a phone on your Wi-Fi, a Home Assistant box), set `GEOLOGS_BIND=0.0.0.0` in `.env`; the database's port stays on `127.0.0.1`.
-   With a proxy or tunnel in another container, put the app on a shared Docker network instead of publishing a port. The proxy connects to the container's own port, `8765`:

    ```yaml
    services:
      app:
        networks:
          internal:
          proxy:
            aliases: [geologs]    # the name the proxy uses: http://geologs:8765
    networks:
      internal:
      proxy:
        external: true            # the network your proxy or tunnel is on
    ```

    Put the database on `internal` only.
-   Do not put a login wall (Cloudflare Access and the like) in front of the whole site: phones and MCP clients cannot pass a browser login. Protect `/admin`-related paths only, if at all.
-   GeoLogs sets the session cookie `Secure` when the proxy sends `X-Forwarded-Proto: https`.

## Backups

```bash
docker compose exec -T db pg_dump -U geologs -d geologs -Fc > geologs-$(date +%F).dump
```

Keep `.env` too: without it a restored database has no passwords to log in with. Restore with `pg_restore` into an empty database. Every account's data is in the one database, so one dump covers everyone.

### Moving one account to another server

A full backup clones one person's archive onto another GeoLogs server, even one that already has other accounts. **Settings → Import & export → Download backup** (or `geologs backup FILE [--user EMAIL]`) writes a gzipped file holding every location and what was made of them by hand: saved places and place names, corrections to stays and travel modes, excluded and learned routes, reminders and what they fired, daily summaries, settings, and the device and API tokens. Make your account on the new server and import the file there (in the browser, or `geologs import FILE [--user EMAIL]`). The tokens come along, so phones and MCP clients keep working once they point at the new address.

The account itself does not move: its password, two-step sign-in and sessions stay on the old server. Stays are found again from the locations after the import, which takes a while for years of history. What is already on the new server is kept, so a phone that started sending there first loses nothing, and importing the same backup twice adds nothing.

## Updating

For a source checkout, pull the new code, rebuild, and restart:

```bash
git pull && docker compose build app && docker compose up -d
```

Tables upgrade themselves when the server starts. Take a backup first, and read the release notes: [Upgrading](upgrading.md) covers versions that change what you must do.

### Approved automatic app updates

GeoLogs can install releases promoted to the `stable-auto` channel. This updates the **app container only**. The database image, Compose configuration and updater script remain under your control. Releases with database upgrade code or required Compose settings changed are rejected by the promotion workflow and need a [manual upgrade](upgrading.md).

Prerequisites: Docker Compose 2.24.4 or newer (for `!reset`), a public `ghcr.io/karunstha/geologs` package, and [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/) on the Docker host. The host user running the updater needs Docker access. First deploy the checkout normally with `docker compose up --build -d` and ensure `.env` has `GEOLOGS_TOKEN`.

Run one update by hand from the checkout:

```bash
./scripts/auto-update
```

It checks the registry digest and release signature, records the image version, writes and validates a PostgreSQL dump in `./backups`, then starts the app by immutable digest. It waits for Docker health and an authenticated `/api/status` request. If either fails, it puts the previous app image back. The database is **not** restored automatically: when a release changes stored data, restoring the old image alone may be unsafe. A failed rollout keeps its backup for a manual restore. Keep a separate off-host backup too.

Schedule the command for a quiet period. For example, in the Docker host user's crontab, replacing `/path/to/geologs` with the absolute checkout path:

```cron
0 3 * * 0 cd /path/to/geologs && /path/to/geologs/scripts/auto-update >> /path/to/geologs/update-status/updater.log 2>&1
```

The updater locks concurrent runs and keeps eight database dumps by default. Set `GEOLOGS_BACKUP_RETENTION` in the cron environment to change that count. The status and log directories stay out of Git. **Admin → Server → Software** shows the running version and the last check. To pause updates, remove or comment out the cron entry; the current pinned image continues running. To inspect the pin, read `update-status/active.env`. Use the two Compose files and both env files when managing an auto-updated installation manually:

```bash
docker compose -f docker-compose.yaml -f docker-compose.auto.yaml \
  --env-file .env --env-file update-status/active.env ps
```

The updater intentionally does not `git pull`: that could change its own code or Compose settings during an unattended run. Update the checkout and review release notes during a manual maintenance window when those files change.
