"""Small dependency-free MCP JSON-RPC dispatcher for Streamable HTTP."""
import json
import sqlite3

from .core import deliver_webhook, geocode, reverse_geocode
from .analysis import day_summary, place_patterns, predict_next, stays, transitions, visited_places
from .postgres import INTEGRITY_ERRORS

DB_INPUT_ERRORS = (ValueError, KeyError, TypeError, sqlite3.IntegrityError) + INTEGRITY_ERRORS
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


NUMBER = {"type": "number"}
STRING = {"type": "string"}
INTEGER = {"type": "integer"}
TOOLS = [
    {"name": "get_current_location", "description": "Most recent GPS point with fix age, stale flag, speed and detected movement mode.", "inputSchema": schema({})},
    {"name": "get_location_history", "description": "GPS samples in a time window (ISO 8601 timestamps).", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_raw_observations", "description": "Received coordinate and accuracy versions with receipt time; exact retransmissions are coalesced.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_compact_timeline", "description": "Informational fixes: movement samples plus arrival and departure edges, with stationary repetition removed.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_location_at_time", "description": "Nearest recorded point to an ISO 8601 timestamp, including its time difference.", "inputSchema": schema({"timestamp": STRING}, ["timestamp"])},
    {"name": "search_locations_near", "description": "Recorded points within a radius of coordinates, optionally in a time window.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "start": STRING, "end": STRING, "source": STRING, "limit": INTEGER}, ["lat", "lon"])},
    {"name": "get_travel_stats", "description": "Sampled distance, movement hours, and mode counts for a time window.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING})},
    {"name": "get_trips", "description": "Recorded movement runs with path, distance, and estimated transport mode.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING})},
    {"name": "get_coverage", "description": "Source date ranges and gaps over 24 hours; use before drawing conclusions from missing location data.", "inputSchema": schema({})},
    {"name": "get_place_visits", "description": "Observed visits to a saved place, split by exits or gaps over 30 minutes.", "inputSchema": schema({"place_id": INTEGER, "start": STRING, "end": STRING}, ["place_id"])},
    {"name": "get_stays", "description": "Observed stops of at least five minutes within 50 m, with optional saved place labels. Sparse GPS cannot prove time in gaps.", "inputSchema": schema({"start": STRING, "end": STRING, "source": STRING, "limit": INTEGER})},
    {"name": "get_frequent_places", "description": "Saved and suggested places ranked by observed stop time and visits; unlabeled clusters can be named with save_place.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_visited_places", "description": "Saved and unsaved observed places ranked by total sampled time, with cache-first address labels.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_unlabeled_frequent_places", "description": "Frequent observed stop clusters that do not yet match a saved place.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "get_time_at_place", "description": "Observed visit count and sampled duration at a saved place; unobserved gaps are excluded.", "inputSchema": schema({"place_id": INTEGER, "start": STRING, "end": STRING}, ["place_id"])},
    {"name": "get_day_summary", "description": "Daily fixes, movement runs, observed visited places, and coverage for a local calendar day.", "inputSchema": schema({"date": STRING, "timezone": STRING, "source": STRING}, ["date"])},
    {"name": "get_place_transitions", "description": "Observed transitions between stop clusters, counted by source and grouped across visits.", "inputSchema": schema({"limit": INTEGER})},
    {"name": "predict_next_destination", "description": "Evidence-based next place estimate from at least three observed departures; may return no prediction.", "inputSchema": schema({})},
    {"name": "list_places", "description": "Saved named places and geofence radii.", "inputSchema": schema({})},
    {"name": "save_place", "description": "Create or edit a named place and arrival geofence.", "inputSchema": schema({"id": INTEGER, "name": STRING, "lat": NUMBER, "lon": NUMBER, "radius_m": NUMBER, "address": STRING}, ["name", "lat", "lon"])},
    {"name": "list_reminders", "description": "Location-triggered reminders with active state.", "inputSchema": schema({})},
    {"name": "create_reminder", "description": "Remind on next arrival at a saved place. One-shot by default; repeat supports a cooldown.", "inputSchema": schema({"text": STRING, "place_id": INTEGER, "repeat": {"type": "boolean"}, "cooldown_minutes": INTEGER}, ["text", "place_id"])},
    {"name": "set_reminder_active", "description": "Enable or disable a location reminder.", "inputSchema": schema({"id": INTEGER, "active": {"type": "boolean"}}, ["id", "active"])},
    {"name": "list_reminder_events", "description": "Triggered reminder inbox for delivery/notification workflows.", "inputSchema": schema({"since_id": INTEGER, "limit": INTEGER})},
    {"name": "ingest_location", "description": "Store one location from any source and evaluate arrival reminders. Location sources may also use POST /api/points.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER, "timestamp": STRING, "source": STRING, "external_id": STRING, "accuracy_m": NUMBER, "speed_mps": NUMBER}, ["lat", "lon"])},
    {"name": "geocode_address", "description": "Search an address near the latest recorded fix with the configured Geoapify or Photon provider; results are cached by area.", "inputSchema": schema({"query": STRING, "lat": NUMBER, "lon": NUMBER}, ["query"])},
    {"name": "reverse_geocode", "description": "Look up an address for coordinates with the configured Geoapify or Photon provider; results are cached.", "inputSchema": schema({"lat": NUMBER, "lon": NUMBER}, ["lat", "lon"])},
]


def call_tool(store, name, args):
    if name == "get_current_location":
        return store.latest()
    if name == "get_location_history":
        return store.points(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000))
    if name == "get_raw_observations":
        return store.observations(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000))
    if name == "get_compact_timeline":
        return store.timeline(args.get("start"), args.get("end"), args.get("source"), args.get("limit", 5000))
    if name == "get_location_at_time":
        return store.at_time(args["timestamp"])
    if name == "search_locations_near":
        return store.near(args["lat"], args["lon"], args.get("radius_m", 250), args.get("start"), args.get("end"), args.get("limit", 100), args.get("source"))
    if name == "get_travel_stats":
        return store.stats(args.get("start"), args.get("end"), args.get("source"))
    if name == "get_trips":
        return store.trips(args.get("start"), args.get("end"), args.get("source"))
    if name == "get_coverage":
        return store.coverage()
    if name == "get_place_visits":
        return store.place_visits(args["place_id"], args.get("start"), args.get("end"))
    if name == "get_stays":
        return stays(store, args.get("start"), args.get("end"), args.get("source"), args.get("limit", 500))
    if name == "get_frequent_places":
        return place_patterns(store, args.get("limit", 50))[0]
    if name == "get_visited_places":
        return visited_places(store, args.get("limit", 100))
    if name == "get_unlabeled_frequent_places":
        return [p for p in place_patterns(store, None)[0] if p["place_id"] is None][:min(int(args.get("limit", 50)), 500)]
    if name == "get_time_at_place":
        return store.place_visits(args["place_id"], args.get("start"), args.get("end"))
    if name == "get_day_summary":
        return day_summary(store, args["date"], args.get("timezone", "UTC"), args.get("source"))
    if name == "get_place_transitions":
        return transitions(store, args.get("limit", 50))
    if name == "predict_next_destination":
        return predict_next(store)
    if name == "list_places":
        return store.places(enrich=True)
    if name == "save_place":
        return store.save_place(args)
    if name == "list_reminders":
        return store.reminders()
    if name == "create_reminder":
        return store.create_reminder(args)
    if name == "set_reminder_active":
        return store.set_reminder_active(args["id"], args["active"])
    if name == "list_reminder_events":
        return store.events(args.get("since_id", 0), args.get("limit", 100))
    if name == "ingest_location":
        value = store.ingest(args)
        deliver_webhook(store, value["events"])
        return value
    if name == "geocode_address":
        return geocode(store, args["query"], args.get("lat"), args.get("lon"))
    if name == "reverse_geocode":
        return reverse_geocode(store, args["lat"], args["lon"])
    raise ValueError("unknown tool: " + str(name))


def dispatch(store, request):
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
            result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "geotracker", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = call_tool(store, params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": {"result": value}}
            except DB_INPUT_ERRORS as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": id_, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32603, "message": str(exc)}}
