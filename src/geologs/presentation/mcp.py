"""Small dependency-free MCP JSON-RPC dispatcher for Streamable HTTP."""
import json

from .. import config
from ..services.corrections import save_mode_override
from ..services.day import day_summary
from ..services.fuel import fuel_costs
from ..services.ingest import record_locations
from ..services.location import current_location
from ..services.overview import daily_briefs, daily_overview, daily_overviews
from ..services.patterns import place_patterns, predict_next, resolve_place_patterns, search_visits, transitions, visited_places
from ..services.places import geocode, nearby_places, reverse_geocode
from ..services.reminders import cancel_reminder, create_reminder
from ..services.samples import near, stats
from ..services.saved_places import delete_label, delete_place, place_visits, save_place, save_place_label
from ..services.stays import anchored_trips, stays
from .errors import INPUT_ERRORS, input_message, internal_failure

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


NUMBER = {"type": "number"}
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
TOOLS = [
    {"name": "get_current_location", "description": "Most recent GPS point with saved or reverse-geocoded place name, copyable coordinates, fix age, stale flag, speed, and detected movement mode.", "inputSchema": schema({})},
    {"name": "get_location_history", "description": "GPS samples in a time window (ISO 8601 timestamps).", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_raw_observations", "description": "Paginated received coordinate and accuracy versions with receipt time; exact retransmissions are coalesced.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER, "offset": INTEGER})},
    {"name": "get_compact_timeline", "description": "Informational fixes: movement samples plus arrival and departure edges, with stationary repetition removed.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_location_at_time", "description": "Nearest recorded point to an ISO 8601 timestamp, including its time difference.", "inputSchema": schema({"timestamp": STRING}, ["timestamp"])},
    {"name": "search_locations_near", "description": "Recorded points within a radius of coordinates, optionally in a time window.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "start": STRING, "end": STRING, "source": STRING, "limit": INTEGER}, ["lat", "lon"])},
    {"name": "get_travel_stats", "description": "Sampled distance, movement hours, and mode counts for a time window.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING})},
    {"name": "get_trips", "description": "Trips between consecutive stays, with route segments, distance, duration, endpoints, coverage, and estimated mode (walking, running, cycling, or driving). Round trips that return to their starting place, such as a run from home, are included with loop=true.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING})},
    {"name": "get_coverage", "description": "Source date ranges and gaps over 24 hours; use before drawing conclusions from missing location data.", "inputSchema": schema({})},
    {"name": "get_place_visits", "description": "Observed visits to a saved place, which is a zone: time anywhere inside it, across the buildings in it, split by leaving or gaps over 30 minutes.", "inputSchema": schema({"place_id": INTEGER, "start": STRING, "end": STRING}, ["place_id"])},
    {"name": "get_stays", "description": "Observed stays at least as long as the stay length in Settings (15 minutes by default), with the saved place (zone) each lies in as zone_name; place_name is set only by a name correction or Home. kind=stationary is a stop within 50 m; kind=area is time moving around one place (a lake loop, park, or large store) with an activity of walking, running, or cycling, its distance_m, and radius_m. Sparse GPS cannot prove time in gaps.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "search_visits", "description": "Answer questions like 'what day did I go to Costco?' or 'when was I last at the lake?'. Finds observed visits whose place name, address, or category contains every query word (word prefixes, case-insensitive), and returns matching places plus each visit's local date, weekday, arrival, departure, and duration, newest first. Matching is literal, not semantic: if nothing matches, retry with a brand, a place type such as 'supermarket', 'park', or 'water', or a street name.", "inputSchema": schema({"query": STRING, "start": STRING, "end": STRING, "limit": INTEGER, "timezone": STRING}, ["query"])},
    {"name": "get_frequent_places", "description": "Saved and suggested places ranked by observed stop time and visits; unlabeled clusters can be named with save_place.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_visited_places", "description": "Saved and unsaved observed places except Home, ranked by total sampled time, with cache-first place labels.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_unlabeled_frequent_places", "description": "Frequent observed stop clusters that do not yet match a saved place.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_time_at_place", "description": "Observed visit count and sampled duration inside a saved place (a zone, across its buildings); unobserved gaps are excluded.", "inputSchema": schema({"place_id": INTEGER, "start": STRING, "end": STRING}, ["place_id"])},
    {"name": "get_day_summary", "description": "Detailed daily fixes, stays (stationary and area), trips between them, activity totals, observed visited places excluding Home, and coverage for a local calendar day.", "inputSchema": schema({"date": STRING, "timezone": STRING, "source": STRING}, ["date"])},
    {"name": "get_daily_overview", "description": "Compact persisted overview for one local day: chronological visits, places including Home and time at each, trips, distances, activity totals (walking, running, cycling, driving), fixes, and reminder activity. Designed for efficient LLM retrieval.", "inputSchema": schema({"date": STRING, "timezone": STRING, "source": STRING}, ["date"])},
    {"name": "get_fuel_costs", "description": "Fuel spent on drives over the last N days (default 30): drives, distance in km, litres, cost in the configured currency, and the average cost per day. Uses the car's fuel economy and the fuel price in Settings (your own, or the US EIA weekly average for your region). configured is false until both are set.", "inputSchema": schema({"days": INTEGER})},
    {"name": "get_daily_brief", "description": "Cheapest way to read what happened on one or more local days (date to end_date, at most 31): plain-text lines for the day's summary, a chronological timeline of stays, trips, and data gaps with local HH:MM times, places with time spent, and reminders set or triggered. Use get_daily_overview when exact timestamps, coordinates, or per-trip fields are needed.", "inputSchema": schema({"date": STRING, "end_date": STRING}, ["date"])},
    {"name": "list_daily_overviews", "description": "List persisted daily overview records and totals without loading every full summary.", "inputSchema": schema({"start": STRING, "end": STRING, "limit": INTEGER})},
    {"name": "get_place_transitions", "description": "Observed transitions between stop clusters, counted by source and grouped across visits.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "predict_next_destination", "description": "Evidence-based next place estimate from at least three observed departures; may return no prediction.", "inputSchema": schema({})},
    {"name": "list_places", "description": "Saved places: named zones with a radius. Visits inside keep the name of the building or place itself (shown with the zone), except Home, which names everything inside it. Use this first to resolve a place_id for a location reminder.", "inputSchema": schema({})},
    {"name": "save_place", "description": "Create or edit a saved place: a named zone (such as a university campus) and arrival geofence. It groups and totals the places visited inside it; it does not rename them. To fix what one visit or spot is called, use set_place_label.", "inputSchema": schema({"id": INTEGER, "name": STRING, "lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "address": STRING, "category": STRING, "provider_place_id": STRING}, ["name", "lat", "lon"])},
    {"name": "delete_place", "description": "Delete a saved place (zone). A place that a reminder uses is hidden instead so the reminder keeps working.", "inputSchema": schema({"id": INTEGER}, ["id"])},
    {"name": "list_place_labels", "description": "Name corrections pinned to locations. Each one names every visit whose position falls inside its radius, ahead of saved places and provider lookups.", "inputSchema": schema({})},
    {"name": "set_place_label", "description": "Correct the name of a visited place: pin a name to the visit's coordinates (radius_m 20-1000, default 50; use the area's extent for an area stay). History, summaries, and visit search use it instead of the provider's guess. Re-labelling within 25 m replaces the earlier label. Without an address, the street address of the spot is looked up and stored. Use list_nearby_places to find the right name.", "inputSchema": schema({"name": STRING, "lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "address": STRING, "category": STRING, "provider_place_id": STRING}, ["name", "lat", "lon"])},
    {"name": "set_travel_mode", "description": "Correct how a trip (target 'trip') or an area stay such as a lake loop (target 'stay') was travelled: walking, running, cycling, driving, or transit; 'auto' restores the speed-based guess. Identify it by source and its start/end times from get_day_summary or get_trips; the correction applies to the trip that overlaps most of that window, so small recalculations do not lose it.", "inputSchema": schema({"source": STRING, "target": STRING, "started_at": STRING, "ended_at": STRING, "mode": STRING}, ["source", "started_at", "ended_at", "mode"])},
    {"name": "remove_place_label", "description": "Remove a name correction so visits there are named automatically again.", "inputSchema": schema({"id": INTEGER}, ["id"])},
    {"name": "list_reminders", "description": "Active one-shot location reminders waiting for arrival.", "inputSchema": schema({})},
    {"name": "list_reminder_history", "description": "Past triggered or cancelled location reminders.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "create_reminder", "description": "Create a one-shot reminder. Three kinds: at a saved place_id (arrival geofence); at an unsaved destination (place_name with lat/lon, or place_name alone for the nearest geocoding match); or match_name alone for any place with that name, such as 'Starbucks', which fires after about three minutes stopped within 150 m of a place whose name matches (word prefixes, tolerant of small typos).", "inputSchema": schema({"text": STRING, "place_id": INTEGER, "place_name": STRING, "match_name": STRING, "lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "address": STRING, "category": STRING, "provider_place_id": STRING}, ["text"])},
    {"name": "cancel_reminder", "description": "Cancel an active location reminder. Cancelled reminders remain in history and cannot be re-enabled.", "inputSchema": schema({"id": INTEGER}, ["id"])},
    {"name": "list_reminder_events", "description": "Triggered reminder inbox for delivery/notification workflows.", "inputSchema": schema({"since_id": INTEGER, "limit": INTEGER})},
    {"name": "ingest_location", "description": "Store one location from any source and evaluate arrival reminders. Location sources may also use POST /api/points.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "timestamp": STRING, "source": STRING, "external_id": STRING, "accuracy_m": NUMBER, "speed_mps": NUMBER}, ["lat", "lon"])},
    {"name": "geocode_address", "description": "Search for a place or address near the latest recorded fix with the places provider chosen in Settings (Geoapify, Mapbox, or Photon); results are cached by area.", "inputSchema": schema({"query": STRING, "lat": NUMBER, "lon": NUMBER}, ["query"])},
    {"name": "list_nearby_places", "description": "Named places around coordinates, nearest first, from the places provider chosen in Settings. Use it to correct a visited place that was named after the wrong neighbour: pick the right one and save_place at the visit's coordinates with a small radius.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "limit": INTEGER}, ["lat", "lon"])},
    {"name": "reverse_geocode", "description": "Name coordinates after the nearest named place within radius_m (default 150 m) using the places provider chosen in Settings (Geoapify, Mapbox, or Photon), falling back to the street address when nothing named is nearby. For a stay, pass its averaged position and extent. Results are spatially cached.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER}, ["lat", "lon"])},
]


def call_tool(db, name, args):
    if name == "get_current_location":
        return current_location(db)
    if name == "get_location_history":
        return db.points.points(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000))
    if name == "get_raw_observations":
        return db.points.observations(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000), args.get("offset", 0))
    if name == "get_compact_timeline":
        return db.points.timeline(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000))
    if name == "get_location_at_time":
        return db.points.nearest_in_time(args["timestamp"])
    if name == "search_locations_near":
        return near(db, args["lat"], args["lon"], args.get("radius_m", 250), args.get("start"), args.get("end"), args.get("limit", 100), args.get("source"))
    if name == "get_travel_stats":
        return stats(db, args.get("start"), args.get("end"), args.get("source"))
    if name == "get_trips":
        return anchored_trips(db, args.get("start"), args.get("end"), args.get("source"))
    if name == "get_coverage":
        return db.points.coverage()
    if name == "get_place_visits":
        return place_visits(db, args["place_id"], args.get("start"), args.get("end"))
    if name == "get_stays":
        return stays(db, args.get("start"), args.get("end"), args.get("source"), args.get("limit", 500))
    if name == "get_fuel_costs":
        return fuel_costs(db, min(max(int(args.get("days", 30)), 1), 366))
    if name == "search_visits":
        return search_visits(db, args["query"], args.get("start"), args.get("end"), args.get("limit", 50), args.get("timezone"))
    if name == "get_frequent_places":
        return resolve_place_patterns(db, place_patterns(db, None))[0][:min(int(args.get("limit", 50)), 5000)]
    if name == "get_visited_places":
        return visited_places(db, args.get("limit", 100))
    if name == "get_unlabeled_frequent_places":
        return [p for p in resolve_place_patterns(db, place_patterns(db, None))[0]
                if p["place_id"] is None and not p.get("label_id")][:min(int(args.get("limit", 50)), 500)]
    if name == "get_time_at_place":
        return place_visits(db, args["place_id"], args.get("start"), args.get("end"))
    if name == "get_day_summary":
        return day_summary(db, args["date"], args.get("timezone") or db.settings.get()["timezone"], args.get("source"))
    if name == "get_daily_overview":
        return daily_overview(db, args["date"], args.get("timezone"), args.get("source"))
    if name == "get_daily_brief":
        return daily_briefs(db, args["date"], args.get("end_date"))
    if name == "list_daily_overviews":
        return daily_overviews(db, args.get("start"), args.get("end"), args.get("limit", 90))
    if name == "get_place_transitions":
        return transitions(db, args.get("limit", 50))
    if name == "predict_next_destination":
        return predict_next(db)
    if name == "list_places":
        return db.places.saved()
    if name == "save_place":
        return save_place(db, args)
    if name == "delete_place":
        return delete_place(db, args["id"])
    if name == "list_place_labels":
        return db.places.labels()
    if name == "set_place_label":
        return save_place_label(db, args)
    if name == "set_travel_mode":
        return save_mode_override(db, args)
    if name == "remove_place_label":
        return delete_label(db, args["id"])
    if name == "list_reminders":
        return db.reminders.active()
    if name == "list_reminder_history":
        return db.reminders.history(args.get("limit", 200))
    if name == "create_reminder":
        return create_reminder(db, args)
    if name == "cancel_reminder":
        return cancel_reminder(db, args["id"])
    if name == "list_reminder_events":
        return db.reminders.events(args.get("since_id", 0), args.get("limit", 100))
    if name == "ingest_location":
        return record_locations(db, [args])
    if name == "geocode_address":
        return geocode(db, args["query"], args.get("lat"), args.get("lon"))
    if name == "list_nearby_places":
        return nearby_places(db, args["lat"], args["lon"], args.get("radius_m"), args.get("limit", 15))
    if name == "reverse_geocode":
        return reverse_geocode(db, args["lat"], args["lon"], args.get("radius_m"))
    raise ValueError("unknown tool: " + str(name))


def dispatch(db, request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in request:
        return None
    id_ = request["id"]
    method = request.get("method")
    params = request.get("params") or {}
    try:
        if method == "initialize":
            offered = params.get("protocolVersion", "2025-11-25")
            version = offered if offered in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "geologs", "version": config.app_version()}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = call_tool(db, params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": {"result": value}}
            except INPUT_ERRORS as exc:
                result = {"content": [{"type": "text", "text": input_message(exc)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": id_, "result": result}
    except Exception:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32603, "message": internal_failure(f"MCP {method}")}}
