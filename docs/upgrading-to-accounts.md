# Upgrading a server to accounts

This release turns GeoLogs into a multi-user archive. Your existing data becomes the **owner**'s (the first account, an admin), and everything you use today keeps working: `GEOLOGS_TOKEN`, the `.env` ingest token, device tokens, MCP clients and the reminder webhook all act as the owner.

What changes on the server:

-   The database gains accounts, and every row of your data gains an owner. This runs once, automatically, when the new version starts (about a second and a half for a year of fixes in testing).
-   Requests log in to PostgreSQL as a new role, `geologs_app`, which can only see each user's own rows. Compose **will not start** without its password, `GEOLOGS_APP_DB_PASSWORD`, in `.env`.
-   There is no going back without the backup in step 1: the old version cannot read the upgraded database.

Run every command on the server, in the GeoLogs checkout (`~/docker/geologs`), unless it says otherwise.

## 0. Before you start (on your Mac)

The new version must be on GitHub for the server to pull it. Commit the `postgres-only` branch and push it, or merge it into `main` and push that:

```bash
git push origin postgres-only
```

This release drops `./geologs poll-ha`. If the server still has a cron entry for it, remove it (`crontab -e`); Home Assistant sends positions itself with the REST command in **Settings → Devices → Home Assistant**.

Pick a quiet time. Trackers that send during the upgrade (a minute or two) get an error; OwnTracks and Overland keep their fixes and send them again later.

## 1. Back up

Check the server is healthy first, so you are backing up something good:

```bash
docker compose ps                       # db and app are both "Up"; db is "(healthy)"
docker compose exec db psql -U geologs -d geologs -c "SELECT COUNT(*) FROM point_observations"
```

If the second command says `role "geologs" does not exist`, the earlier rename from Geotracker has not run on this server: run `./scripts/rename-from-geotracker` first (see the README), then start this step again.

Take a database dump and keep the settings:

```bash
mkdir -p ~/geologs-backups
docker compose exec -T db pg_dump -U geologs -d geologs -Fc > ~/geologs-backups/geologs-$(date +%F).dump
cp .env ~/geologs-backups/env-$(date +%F)
```

Check the dump is complete before going on: it should be several megabytes or more, and list your tables.

```bash
ls -lh ~/geologs-backups/
docker compose exec -T db pg_restore --list < ~/geologs-backups/geologs-$(date +%F).dump | grep -c "TABLE DATA"   # 20 or more
```

Copy the dump off the server too (to your Mac, say), so a disk failure cannot take both:

```bash
# on your Mac
scp karun-thinkcentre:geologs-backups/geologs-*.dump ~/Backups/
```

Write down the commit you are running now, for rolling back:

```bash
git rev-parse HEAD > ~/geologs-backups/previous-commit
```

## 2. Add the new settings to `.env`

The app's database login needs its own password, different from `POSTGRES_PASSWORD`:

```bash
echo "GEOLOGS_APP_DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)" >> .env
```

Optional, and can be added later (then run `docker compose up -d app` again):

```bash
# Where people open GeoLogs; links in emails point here.
GEOLOGS_PUBLIC_URL=https://geologs.example.com
# A mail server, for invites and password resets.
GEOLOGS_SMTP_HOST=smtp.example.com
GEOLOGS_SMTP_PORT=587
GEOLOGS_SMTP_SECURITY=starttls
GEOLOGS_SMTP_USERNAME=…
GEOLOGS_SMTP_PASSWORD=…
GEOLOGS_SMTP_FROM=GeoLogs <geologs@example.com>
```

Without them, GeoLogs works as before; invites and reset links are shown in the admin portal for you to pass on.

## 3. Upgrade

```bash
git fetch origin
git checkout postgres-only        # or: git checkout main && git pull, if you merged it
git pull
docker compose build app
docker compose up -d
docker compose logs -f app
```

Watch the log until it says `GeoLogs browser: http://0.0.0.0:8765`, then press Ctrl-C. Starting takes longer than usual this once (the upgrade, then new indexes), from seconds to about a minute depending on how much you have recorded. The database container is not rebuilt: its image is unchanged.

If the log shows an error instead, go to **Rolling back**.

## 4. Check

In the database: requests now use the app's own login, and every fix belongs to the owner.

```bash
docker compose exec db psql -U geologs -d geologs -c "SELECT usename, COUNT(*) FROM pg_stat_activity WHERE datname='geologs' GROUP BY 1"
# geologs_app is listed (geologs may appear briefly, at start-up only)
docker compose exec db psql -U geologs -d geologs -c "SELECT user_id, COUNT(*) FROM point_observations GROUP BY 1"
# one row: user_id 1, with the same count as in step 1
```

In the browser:

1.  Open GeoLogs. A browser that was signed in stays signed in (as the owner). Otherwise choose **Sign in with the server's access token** and enter `GEOLOGS_TOKEN`.
2.  Home shows today, with its map, timeline and photo. Open a day from a few weeks ago: its stays and trips are as you remember them.
3.  Settings → Devices: your phone's latest fix arrives within its usual interval.
4.  The account button (top right) opens **Account**. Add your email and set a password, so you can sign in without the token; consider turning on **Two-factor sign-in**.
5.  **Admin** appears in the menu. Users lists one account (you), with your fixes counted within a few minutes.

Also check what talks to GeoLogs from outside: an MCP client (such as Hermes) answers a question about today, and a reminder still reaches the webhook. Both use `GEOLOGS_TOKEN`, so they act as the owner, as before.

One thing may be slow, once: the first fix that arrives out of order (a phone sending what it kept while offline) finds every stay of that tracker again, which takes a minute or two for a few years of history. After that, late fixes take well under a second.

## 5. Afterwards

-   Keep the backup until you are happy with the new version, a week or two.
-   Invite people from **Admin → Invites**, or with `docker compose exec app geologs invite friend@example.com`.
-   The browser shows a few other changes from this release: Home no longer has the source selector or GeoJSON export, and a phone sending with a wrong token now appears in **Admin → Server** instead of Settings → Devices.

## Rolling back

Only if the new version will not start, or your data looks wrong. This restores the database as it was in step 1: **fixes received since the upgrade are lost**.

```bash
docker compose stop app
docker compose exec db psql -U geologs -d postgres -c "DROP DATABASE geologs WITH (FORCE)"
docker compose exec db psql -U geologs -d postgres -c "CREATE DATABASE geologs OWNER geologs"
docker compose exec -T db pg_restore -U geologs -d geologs < ~/geologs-backups/geologs-YYYY-MM-DD.dump
git checkout "$(cat ~/geologs-backups/previous-commit)"
docker compose build app
docker compose up -d
```

`pg_restore` may print a warning about the `postgis` extension already existing; that is harmless. The `geologs_app` role stays in PostgreSQL but the old version never uses it. Leave `GEOLOGS_APP_DB_PASSWORD` in `.env` (the old version ignores it) for the next attempt.

Then check as in step 4 (the browser part) that your data is back.
