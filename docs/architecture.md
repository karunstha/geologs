# Architecture

## Code structure

The Python package is split into layers, each importing only what lies beneath it:

```text
src/geologs/
  domain/        the rules, with no database, network, or framework code: finding stays one fix at a
                 time, area stays, travel modes, trips and their routes, visits, place naming, your
                 corrections, path matching, fuel cost, briefs, and settings validation
  data/          every SQL statement: Database (one PostgreSQL/PostGIS connection working for one user, its
                 schema, row-level security, migrations and transactions) and a repository per table group,
                 each keeping to its own tables; accounts, sessions and tokens are in users.py
  integrations/  services outside GeoLogs: place providers (Photon, Geoapify, Mapbox), EIA fuel prices,
                 webhooks, and Google Takeout files
  services/      the use cases: each joins the rules to storage and outside services in one transaction
                 (ingest and record_locations, stays, day, overview, places, reminders, paths, fuel, ...)
  presentation/  ways in: the REST API and browser app (http.py, web/), MCP, the command line, tracker
                 apps' payload formats, and what a client is told when a request fails
  config.py      everything read from the environment, in one place
```

`domain` imports only the standard library; `integrations` and `data` add `domain`; `services` may use all
three; `presentation` uses `services` and `domain`, never `data` or `integrations` directly; every layer but
`domain` may read `config`. Each use case runs in one transaction (`@use_case`): one called by another joins
it, and only the outermost commits, so no part saves half of the whole. Cached values (place lookups, a
day's analysis) are kept at once unless the caller has uncommitted work. Reading never writes your data:
the server's background worker learns paths, fills in place details, saves daily summaries, and fetches
fuel prices. Open the archive with `geologs.services.open_database()`, which also lets an upgrade
recalculate stays. `tests/test_architecture.py` keeps these rules: the imports, no SQL outside `data`,
no environment reads outside `config`, and SQLite only in the one-time importer (`migrate-sqlite`).

## Browser app structure

The browser app is plain HTML, CSS, and native ES modules served from `src/geologs/presentation/web`, with no build step:

```text
web/
  index.html            page markup; loads the stylesheets in cascade order and js/main.js
  css/
    tokens.css          colours, type, radii, shadows, and sizes for light and dark themes
    base.css            element defaults and utilities
    layout.css          app shell: top bar, navigation (a bottom tab bar on phones), page headers
    components/         buttons, forms, cards, lists, tabs, chips, dialogs
    map.css             Leaflet styling
    pages/              home, places, reminders, summaries, settings, account, admin
  js/
    main.js             entry point: wires each view, then loads data and keeps it fresh
    core/               DOM helpers, shared state, formatting, and the API client
    ui/icons.js         travel-mode and place icons, source names, status pills
    map/map.js          everything drawn on the Leaflet map
    views/              one module per page, plus the timeline, place dialogs, sign-in, the account and admin pages, and page switching
    app/refresh.js      loading data for every view and polling for arrival reminders
```

Each view exports an `init…()` function that `main.js` calls once to attach its event handlers, so modules never act on each other while they load. Responsive rules live in the same file as the component they adjust. The server serves `index.html` and anything under `/css/` and `/js/` (only `.css`, `.js`, and `.html`), with ETags so unchanged files are answered with `304`.
