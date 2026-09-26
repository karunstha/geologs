# Reference feature audit

The four reference projects informed the product choices below. “Built” describes working code in this repository. “Planned” is not part of the runtime yet.

| Source | Valuable feature | Geotracker status | Decision |
|---|---|---|---|
| [location-history-mcp-server](https://github.com/lucas-1000/location-history-mcp-server) | Stay detection, visits, frequent places, manual labels | Built | Stays need two accurate samples spanning at least five minutes within 50 m. Repeat stops become place suggestions; saved places provide names and geofences. |
| Same | Spatial indexing with PostGIS | Built, testing underway | Docker Compose runs PostGIS. The alternate store has GiST indexes for points, places, and reverse cache centers; nearby and cache lookups use `ST_DWithin`. SQLite remains supported. |
| [tars-location-mcp](https://github.com/michelgrolet/tars-location-mcp) | Coverage gaps and stale current fixes | Built | MCP exposes per-source coverage and gaps; current location includes fix age and a stale flag. |
| Same | Overlapping historical imports | Partly built | Classic raw Takeout `locations` and CSV imports are idempotent. Semantic segment and `timelineObjects` visit/activity import remain planned. |
| Same | Chronological stays and day summaries | Built | MCP and REST return observed stays and a day summary in an IANA timezone, with a coverage caveat. |
| Same | Home history, cities/countries, travel companions, weather | Planned selectively | These need semantic places, home periods, consented people data, or external services. They are not inferred from bare GPS points. |
| [location_profiler](https://github.com/2015xli/location_profiler) | Place transitions and next destination | Built | Transitions are counted from observed stays. Recent transitions receive more weight. Prediction requires at least three departures and evidence newer than 180 days. |
| Same | Graph visualization and pruning | Planned | Browser lists top transitions; a graph view remains possible. Raw history is never pruned implicitly. |
| [mcp-geo](https://github.com/handsomegentlemen/mcp-geo) | Current location MCP access | Built | `get_current_location` uses a source-fed fix and reports staleness. Request/IP geolocation is too coarse for a personal arrival reminder and is not used as a fallback. |

## How the derived features work

- **Observed stays:** incremental per-source analysis processes only new points. A late import rebuilds just its source. Gaps longer than 30 minutes split stays. Low-accuracy points do not support a stay. The result is observed time, not proof of where the person spent a gap.
- **Frequent places:** stays matching saved place radii are named. Unmatched stays within 75 m form suggestions that the user can name. Visible suggestions and visited places receive cache-first reverse geocoded addresses when a provider is configured.
- **Transitions:** consecutive stays from the same source count as a transition when less than 24 hours apart. Scores decay with a 90-day half-life; raw counts remain available.
- **Prediction:** the current fix must be near an observed stay. Fewer than three departures or only old transitions return no destination. The result is a historical tendency, not a location guarantee.
- **Takeout import:** classic raw `locations` records are streamed in batches. Import never triggers live reminders or geocoder requests. Reimports ignore duplicate points. Other Google Timeline export shapes need explicit parsers that preserve semantic visit and activity data rather than inventing GPS fixes.

## Product additions beyond the references

Geotracker connects Home Assistant directly, has a source-independent ingest API, stores arrival reminders and webhook events, and offers a browser for map paths, speed, trips, observed stays, place suggestions, and transitions. Saved and observed places are enriched on demand through a conservative reverse cache.
