# Privacy

A location archive is sensitive. This page says where the data is, who can see it, and what leaves your server, so you can decide what to turn on, and tell the people you run it for.

## Where the data is

Everything is in your PostgreSQL database and your `.env` file. Nothing is sent to the GeoLogs authors, and there is no telemetry.

## Who can see what

-   **Each user** sees only their own archive. This is enforced by the database itself (row-level security), and the test suite checks it on every route and every MCP tool.
-   **Admins** manage accounts and see how much each person recorded (counts and the time of the latest fix), never where. An admin can delete an account, or only its location data, but cannot read it.
-   **Anyone with a link you share** can watch that one day's replay, without signing in, until the link expires or you turn it off (Account → Shared links). A link shows the day as it was when you made it: the ways you went and the stops, but by default not the first and last 500 m of the day or anything within 500 m of Home, with times counted from the start rather than the time of day; you choose that, and whether place names show, when you make it. Nothing else of your archive is reachable through it. The link's address is its only key, so anyone you send it to can pass it on; GeoLogs keeps only a hash of it, and the page tells browsers and search engines not to pass it on or index it. Chat and social apps that show a posted link fetch the page and its picture from your server: the picture is drawn by your browser from what the link itself shows (never more of the day), with its title and totals. The link can also be embedded in another web page, which plays it the same way. Deleting your location data, or your account, turns your links off.
-   **A video you save** (Replay → Video) is a file on your device, and shows what that replay shows: your own replay is the whole day, Home included. To post a video without them, make a link and save the video from the link's page.
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

Installed as an app (or once a browser has opened GeoLogs over HTTPS), the page keeps what the views you opened last read, in that browser alone, so it can open them without a connection. That copy is deleted when you sign out, and whenever the server answers that you are signed out.

## For operators

-   Use HTTPS, keep the database off the internet, and back up `.env` and the database together.
-   Passwords are stored as scrypt hashes, and sessions and tokens only as hashes. Turn on two-factor sign-in for admin accounts (Account → Two-factor sign-in): an admin account can change everyone's.
-   In many places, location data is personal data. If you run GeoLogs for people other than yourself, check what the law where you are requires of you: telling users what you keep, letting them export or delete it, and keeping it secure.
