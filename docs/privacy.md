# Privacy

A location archive is sensitive. This page says where the data is, who can see it, and what leaves your server, so you can decide what to turn on, and tell the people you run it for.

## Where the data is

Everything is in your PostgreSQL database and your `.env` file. Nothing is sent to the GeoLogs authors, and there is no telemetry.

## Who can see what

-   **Each user** sees only their own archive. This is enforced by the database itself (row-level security), and the test suite checks it on every route and every MCP tool.
-   **Admins** manage accounts and see how much each person recorded (counts and the time of the latest fix), never where. An admin can delete an account, which erases its data, but cannot read it.
-   **Whoever runs the server** can read the database, its backups and `.env`, and so everyone's data. If you run GeoLogs for other people, tell them so, and protect backups as you would the database. An account's data is erased by deleting the account.

## What leaves your server

Only what you turn on. Each service sees the requests below, from your server, so its operator learns that this address asked.

| Service | When | What it receives |
| --- | --- | --- |
| Nominatim (OpenStreetMap) | Always, to name the town on the Home page | A position rounded to about a kilometre |
| Open-Meteo | Always, to choose the photo | The same rounded position and a date |
| Unsplash | With `UNSPLASH_ACCESS_KEY` | The town name and weather words |
| Geoapify, Mapbox or your Photon | With a key or URL, to name visits and search places | The position of a stay (or a search's text and your latest position) |
| EIA | With `EIA_API_KEY` | Nothing about you: a US region and a fuel grade |
| Your mail server | With `GEOLOGS_SMTP_*` | Invite, reset and confirmation emails |
| Your webhook | With `GEOLOGS_WEBHOOK_URL` | The owner's fired reminders |

The browser also loads the map's tiles from OpenStreetMap (which sees the area the map shows) and Leaflet from unpkg.com. Raw GPS fixes are never sent to any provider.

## For operators

-   Use HTTPS, keep the database off the internet, and back up `.env` and the database together.
-   Passwords are stored as scrypt hashes, and sessions and tokens only as hashes. Turn on two-factor sign-in for admin accounts (Account → Two-factor sign-in): an admin account can change everyone's.
-   In many places, location data is personal data. If you run GeoLogs for people other than yourself, check what the law where you are requires of you: telling users what you keep, letting them export or delete it, and keeping it secure.
