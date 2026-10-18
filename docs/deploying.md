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

## Updating

Pull the new code, rebuild, and restart:

```bash
git pull && docker compose build app && docker compose up -d
```

Tables upgrade themselves when the server starts. Take a backup first, and read the release notes: [Upgrading](upgrading.md) covers versions that change what you must do.
