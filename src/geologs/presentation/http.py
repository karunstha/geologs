"""Local dashboard/API and stateless MCP Streamable HTTP transport."""
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config
from ..domain.access import token_allows_source
from ..services import open_database
from ..services.ambient import ambient
from ..services.corrections import delete_stay_edit, save_mode_override, save_stay_edit
from ..services.day import day_summary
from ..services.devices import create_ingest_token, delete_ingest_device, delete_ingest_token, ingest_token_for, record_ingest, source_activity
from ..services.fuel import fuel_cost_windows, fuel_status, refresh_prices, update_fuel
from ..services.ingest import record_locations
from ..services.location import current_location
from ..services.overview import daily_briefs, daily_overview, daily_overviews, save_scheduled_summaries, stored_daily_record
from ..services.paths import exclude_path, include_path, learn_path_history, learn_recent_paths, list_paths
from ..services.patterns import insights, place_overview, place_patterns, predict_next, resolve_place_patterns, search_visits, transitions, visited_places
from ..services.places import geocode, nearby_places, place_details, places_provider, reverse_geocode
from ..services.reminders import cancel_reminder, create_reminder
from ..services.samples import delete_samples, delete_source, export_geojson, near, stats
from ..services.saved_places import delete_label, delete_place, enrich_saved_places, place_visits, save_place, save_place_label
from ..services.settings import place_providers, save_settings
from ..services.stays import anchored_trips, stays
from .errors import INPUT_ERRORS, input_message, internal_failure
from .ingest_formats import claimed_source, FORMATS as INGEST_FORMATS
from .mcp import dispatch, SUPPORTED_VERSIONS

WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".yaml": "text/yaml; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoLogs/0.1"
    protocol_version = "HTTP/1.1"
    # A client that stops sending (a stalled phone, or a slow-loris) frees its
    # thread after this long instead of holding it.
    timeout = 30

    def log_message(self, format_, *args):
        # Avoid access logs containing private location queries or tokens.
        pass

    def send_json(self, status, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, status, message):
        self.send_json(status, {"error": message})

    def request_length(self):
        """The declared body length, or -1 when it is not a whole number of bytes (a negative
        length would make the body read wait until the client hangs up)."""
        value = (self.headers.get("Content-Length") or "0").strip()
        return int(value) if value.isascii() and value.isdigit() else -1

    def send_empty(self, status, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def mcp_error(self, status, code, message, request_id=None):
        self.close_connection = True
        self.send_json(status, {"jsonrpc": "2.0", "id": request_id,
                                "error": {"code": code, "message": message}}, {"Connection": "close"})

    def mcp_headers_valid(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.mcp_error(415, -32600, "Content-Type must be application/json")
            return False
        accepted = {part.split(";", 1)[0].strip().lower() for part in self.headers.get("Accept", "").split(",")}
        if not {"application/json", "text/event-stream"}.issubset(accepted):
            self.mcp_error(406, -32600, "Accept must include application/json and text/event-stream")
            return False
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_VERSIONS:
            self.mcp_error(400, -32600, "Unsupported MCP protocol version")
            return False
        return True

    def authorized(self):
        token = config.api_token()
        if not token:
            return True
        bearer = self.headers.get("Authorization", "")
        if secrets.compare_digest(bearer, "Bearer " + token):
            return True
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        session = cookies.get("geologs_session")
        return bool(session and secrets.compare_digest(session.value, self.session_value(token)))

    @staticmethod
    def session_value(token):
        return hmac.digest(token.encode(), b"geologs-browser-session-v1", "sha256").hex()

    def session_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"geologs_session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"

    def safe_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.netloc == self.headers.get("Host") and parsed.scheme in ("http", "https")

    def ingest_authorized(self, query):
        """Tracking apps authenticate however they can: a bearer header, the password
        of HTTP Basic auth (OwnTracks), or ?token= in the URL (Overland, Traccar Client,
        GPSLogger). The main token, the .env ingest token, or a device token from
        Settings is accepted; a device token is kept on self.device_token so its
        tracker lock can be checked once the points are read. With no token
        configured anywhere, anyone may send (a loopback-only server)."""
        self.device_token = None
        main, ingest = config.api_token(), config.ingest_token()
        if main and self.authorized():
            return True
        supplied = []
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied.append(header[7:])
        elif header.startswith("Basic "):
            try:
                supplied.append(base64.b64decode(header[6:], validate=True).decode().partition(":")[2])
            except (binascii.Error, UnicodeDecodeError):
                pass
        if query.get("token"):
            supplied.append(query["token"])
        if any(secrets.compare_digest(value, token) for value in supplied for token in (main, ingest) if token):
            return True
        try:
            db = open_database()
        except Exception:
            return not main and not ingest
        try:
            for value in supplied:
                row = ingest_token_for(db, value)
                if row:
                    self.device_token = row
                    return True
            return not main and not ingest and not db.devices.has_tokens()
        finally:
            db.close()

    def token_refuses(self, sources):
        """The message when a device token is locked to another tracker, else None."""
        locked = self.device_token["source"] if self.device_token else None
        stray = next((source for source in sources if not token_allows_source(locked, source)), None)
        if stray is None:
            return None
        return f"This token can only send as {locked}" + (" trackers" if locked.endswith(":") else "") + f", not {stray}"

    def send_web_file(self, path):
        """The browser app: index.html, stylesheets and modules under /css/ and /js/, and the Home
        Assistant blueprint under /ha/ (public: Home Assistant fetches it, and it holds no secrets)."""
        relative = "index.html" if path == "/" else path.lstrip("/")
        parts = relative.split("/")
        kind = WEB_TYPES.get(Path(relative).suffix)
        if kind is None or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            self.error_json(404, "not found"); return
        resource = files("geologs.presentation").joinpath("web", *parts)
        if not resource.is_file():
            self.error_json(404, "not found"); return
        body = resource.read_bytes()
        tag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        # Revalidate every time: unchanged files cost a 304, and an upgrade is never stale.
        headers = {"Cache-Control": "no-cache", "ETag": tag, "X-Content-Type-Options": "nosniff"}
        unchanged = self.headers.get("If-None-Match") == tag
        self.send_response(304 if unchanged else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", "0" if unchanged else str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not unchanged:
            self.wfile.write(body)

    def open_store(self):
        """The store, or a 503 the browser can show (instead of a dropped connection) when the database is down."""
        try:
            return open_database()
        except Exception as exc:
            print("GeoLogs database unavailable: " + str(exc), flush=True)
            self.error_json(503, "Database unavailable: " + str(exc).splitlines()[0][:300])
            return None

    def note_ingest(self, kind, status, message=None, device=None, accepted=0):
        try:
            db = open_database()
        except Exception:
            return
        try:
            record_ingest(db, kind, status, message, device, accepted)
        except Exception:
            pass
        finally:
            db.close()

    def receive_locations(self, kind, query):
        """POST/GET /api/ingest/<format>: points from a tracking app, answered the way it expects."""
        adapter = INGEST_FORMATS.get(kind)
        claimed = claimed_source(kind, query, self.headers) if adapter else None
        if not self.ingest_authorized(query):
            self.close_connection = True
            if adapter:
                self.note_ingest(kind, 401, "Wrong or missing token", claimed)
            self.send_json(401, {"error": "token required"}, {"WWW-Authenticate": 'Basic realm="GeoLogs"'})
            return
        if not self.safe_origin():
            self.close_connection = True
            self.error_json(403, "cross-origin request denied"); return
        if adapter is None:
            self.error_json(404, "unknown format; use one of: " + ", ".join(INGEST_FORMATS)); return
        length = self.request_length()
        if not 0 <= length <= 2_000_000:
            self.close_connection = True
            self.error_json(413 if length > 0 else 400, "invalid request size"); return
        body = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            if not body.strip():
                payload = None
            elif content_type == "application/x-www-form-urlencoded":
                payload = {key: value[0] for key, value in parse_qs(body.decode()).items()}
            else:
                payload = json.loads(body)
            points, skipped, reply = adapter(payload, query, self.headers)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "invalid request body"
            self.note_ingest(kind, 400, message, claimed)
            self.error_json(400, message); return
        source = points[-1]["source"] if points else claimed
        refused = self.token_refuses([point["source"] for point in points])
        if refused:
            self.close_connection = True
            self.note_ingest(kind, 403, refused, source)
            self.error_json(403, refused); return
        db = self.open_store()
        if db is None:
            return
        try:
            # Retries and acknowledged non-location messages still prove the app gets through.
            result = record_locations(db, points, kind, source, self.device_token["id"] if self.device_token else None,
                                      f"{skipped} malformed fix{'' if skipped == 1 else 'es'} skipped" if skipped else None)
            inserted, duplicates = result["inserted"], result["duplicates"]
        except INPUT_ERRORS as exc:
            self.note_ingest(kind, 400, input_message(exc), source)
            self.error_json(400, input_message(exc)); return
        except Exception:
            self.error_json(500, internal_failure(self.command + " " + urlparse(self.path).path)); return
        finally:
            db.close()
        self.send_json(200, reply if reply is not None else
                       {"inserted": inserted, "duplicates": duplicates, "skipped": skipped})

    def guarded(self):
        if not self.authorized():
            if self.command == "POST":
                self.close_connection = True
            self.error_json(401, "Bearer token required")
            return False
        if not self.safe_origin():
            if self.command == "POST":
                self.close_connection = True
            self.error_json(403, "cross-origin request denied")
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path.startswith(("/css/", "/js/", "/ha/")):
            self.send_web_file(url.path)
            return
        if url.path == "/mcp":
            if self.guarded():
                self.send_empty(405, {"Allow": "POST"})
            return
        if url.path.startswith("/api/ingest/"):
            self.receive_locations(url.path[len("/api/ingest/"):], {key: value[0] for key, value in parse_qs(url.query).items()})
            return
        if not self.guarded():
            return
        q = {key: value[0] for key, value in parse_qs(url.query).items()}
        db = self.open_store()
        if db is None:
            return
        try:
            if url.path == "/api/status":
                active = places_provider(db)
                value = {"ok": True, "geocoding": active is not None, "geocoder": active, "auth": bool(config.api_token()),
                         "places_providers": [{"id": name, **info} for name, info in place_providers().items()]}
            elif url.path == "/api/current": value = current_location(db)
            elif url.path == "/api/points": value = db.points.points(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000))
            elif url.path == "/api/observations": value = db.points.observations(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000), q.get("offset", 0))
            elif url.path == "/api/timeline": value = db.points.timeline(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000))
            elif url.path == "/api/settings": value = db.settings.get()
            elif url.path == "/api/stats": value = stats(db, q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/trips": value = anchored_trips(db, q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/coverage": value = db.points.coverage()
            elif url.path == "/api/near": value = near(db, q["lat"], q["lon"], q.get("radius_m", 250), q.get("start"), q.get("end"), q.get("limit", 100), q.get("source"))
            elif url.path == "/api/place-visits": value = place_visits(db, q["place_id"], q.get("start"), q.get("end"))
            elif url.path == "/api/stays": value = stays(db, q.get("start"), q.get("end"), q.get("source"), q.get("limit", 500))
            elif url.path == "/api/frequent-places": value = resolve_place_patterns(db, place_patterns(db, None))[0][:min(int(q.get("limit", 50)), 5000)]
            elif url.path == "/api/visited-places": value = visited_places(db, q.get("limit", 100))
            elif url.path == "/api/place-overview": value = place_overview(db, q.get("limit", 100))
            elif url.path == "/api/visits": value = search_visits(db, q.get("q", ""), q.get("start"), q.get("end"), q.get("limit", 50), q.get("timezone"))
            elif url.path == "/api/transitions": value = transitions(db, q.get("limit", 50))
            elif url.path == "/api/predict-next": value = predict_next(db)
            elif url.path == "/api/insights": value = insights(db, min(int(q.get("limit", 50)), 100))
            elif url.path == "/api/ambient": value = ambient(db, q["lat"], q["lon"], q.get("date"), q.get("timezone"))
            elif url.path == "/api/day": value = day_summary(db, q["date"], q.get("timezone") or db.settings.get()["timezone"], q.get("source"))
            elif url.path == "/api/daily-summary": value = daily_overview(db, q["date"], q.get("timezone"), q.get("source"))
            elif url.path == "/api/daily-summaries": value = daily_overviews(db, q.get("start"), q.get("end"), q.get("limit", 90))
            elif url.path == "/api/daily-summary/stored": value = stored_daily_record(db, q["date"])
            elif url.path == "/api/sources":
                value = {**source_activity(db), "ingest_token": bool(config.ingest_token()), "formats": list(INGEST_FORMATS),
                         "now": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            elif url.path == "/api/daily-brief": value = daily_briefs(db, q["date"], q.get("end_date"))
            elif url.path == "/api/places": value = db.places.saved()
            elif url.path == "/api/place-labels": value = db.places.labels()
            elif url.path == "/api/ingest-tokens":
                value = {"tokens": db.devices.tokens(), "env_token": bool(config.ingest_token())}
            elif url.path == "/api/fuel":
                costs = fuel_cost_windows(db, (7, 30))
                value = {**fuel_status(db), "last_7_days": costs[7], "last_30_days": costs[30]}
            elif url.path == "/api/stay-edits": value = db.corrections.stay_edits(q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/mode-overrides": value = db.corrections.mode_overrides()
            elif url.path == "/api/reminders": value = db.reminders.active()
            elif url.path == "/api/reminder-history": value = db.reminders.history(q.get("limit", 200))
            elif url.path == "/api/events": value = db.reminders.events(q.get("since_id", 0), q.get("limit", 100))
            elif url.path == "/api/geocode":
                # The page fetches coordinates only for the result you pick (/api/geocode/place).
                value = geocode(db, q.get("q", ""), q.get("lat"), q.get("lon"), resolve=0)
            elif url.path == "/api/geocode/place": value = place_details(db, q.get("id"), q.get("session"))
            elif url.path == "/api/reverse": value = reverse_geocode(db, q["lat"], q["lon"], q.get("radius_m"))
            elif url.path == "/api/nearby-places": value = nearby_places(db, q["lat"], q["lon"], q.get("radius_m"), q.get("limit", 15))
            elif url.path == "/api/export.geojson": value = export_geojson(db, q.get("start"), q.get("end"))
            elif url.path == "/api/paths": value = list_paths(db, q.get("limit", 200))
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, value)
        except INPUT_ERRORS as exc:
            self.error_json(400, input_message(exc))
        except Exception:
            self.error_json(500, internal_failure(self.command + " " + urlparse(self.path).path))
        finally:
            db.close()

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/ingest/"):
            self.receive_locations(path[len("/api/ingest/"):], {key: value[0] for key, value in parse_qs(url.query).items()})
            return
        if path == "/api/session":
            if not self.safe_origin():
                self.error_json(403, "cross-origin request denied"); return
            length = self.request_length()
            if not 0 <= length <= 4096:
                self.close_connection = True
                self.error_json(413 if length > 4096 else 400, "invalid request size"); return
            try:
                payload = json.loads(self.rfile.read(length))
                supplied = payload["token"]
                if not isinstance(supplied, str):
                    raise ValueError("token must be a string")
            except (ValueError, KeyError, TypeError):
                self.error_json(400, "invalid token request"); return
            configured = config.api_token()
            if configured and not secrets.compare_digest(supplied, configured):
                self.error_json(401, "Access token rejected. Restart the server if its configured token changed."); return
            cookie = self.session_cookie(self.session_value(configured), 30 * 86400) if configured else self.session_cookie("", 0)
            self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return
        # Location senders may post native points with the ingest-only token too.
        sender = path == "/api/points" and self.ingest_authorized({key: value[0] for key, value in parse_qs(url.query).items()})
        if sender and not self.safe_origin():
            self.close_connection = True
            self.error_json(403, "cross-origin request denied"); return
        if not sender and not self.guarded():
            return
        if path == "/mcp" and not self.mcp_headers_valid():
            return
        length = self.request_length()
        if length < 0:
            self.close_connection = True
            if path == "/mcp":
                self.mcp_error(400, -32600, "Invalid Content-Length")
            else:
                self.error_json(400, "invalid Content-Length")
            return
        if length > 2_000_000:
            self.close_connection = True
            if path == "/mcp":
                self.mcp_error(413, -32600, "Request too large")
            else:
                self.error_json(413, "request too large")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            if path == "/mcp":
                self.mcp_error(400, -32700, "Parse error")
            else:
                self.error_json(400, "invalid JSON")
            return
        if path == "/mcp":
            if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                request_id = payload.get("id") if isinstance(payload, dict) else None
                self.mcp_error(400, -32600, "Invalid Request", request_id)
                return
            if "method" not in payload and ("result" in payload or "error" in payload):
                self.send_empty(202)
                return
        db = self.open_store()
        if db is None:
            return
        try:
            if path == "/mcp":
                result = dispatch(db, payload)
                if result is None:
                    self.send_empty(202)
                else:
                    self.send_json(200, result)
                return
            if path == "/api/points":
                items = payload if isinstance(payload, list) else [payload]
                refused = sender and self.token_refuses([str((item or {}).get("source") or "manual") for item in items])
                if refused:
                    self.error_json(403, refused); return
                last = payload[-1] if isinstance(payload, list) and payload else payload
                result = record_locations(db, items, "api", str((last or {}).get("source") or "manual")[:80],
                                          self.device_token["id"] if sender and self.device_token else None)
            elif path == "/api/ingest-tokens":
                result = create_ingest_token(db, payload)
            elif path == "/api/fuel":
                result = update_fuel(db, payload)
            elif path == "/api/ingest-tokens/delete":
                result = delete_ingest_token(db, payload["id"])
            elif path == "/api/sources/delete":
                result = delete_source(db, payload.get("source"), payload.get("confirm") is True)
            elif path == "/api/ingest-devices/delete":
                result = delete_ingest_device(db, payload["kind"], payload.get("device") or "")
            elif path == "/api/observations/delete":
                result = delete_samples(db, payload.get("ids") or [], payload.get("confirm") is True)
            elif path == "/api/settings": result = save_settings(db, payload)
            elif path == "/api/paths/learn": result = {**learn_path_history(db), **list_paths(db)}
            elif path == "/api/paths/exclude": result = exclude_path(db, payload["source"], payload["started_at"])
            elif path == "/api/paths/include": result = include_path(db, payload["source"], payload["started_at"])
            elif path == "/api/places": result = save_place(db, payload)
            elif path == "/api/places/delete": result = delete_place(db, payload["id"])
            elif path == "/api/place-labels": result = save_place_label(db, payload)
            elif path == "/api/mode-overrides": result = save_mode_override(db, payload)
            elif path == "/api/stay-edits": result = save_stay_edit(db, payload)
            elif path == "/api/stay-edits/delete": result = delete_stay_edit(db, payload["id"])
            elif path == "/api/place-labels/delete": result = delete_label(db, payload["id"])
            elif path == "/api/reminders": result = create_reminder(db, payload)
            elif path == "/api/reminders/cancel": result = cancel_reminder(db, payload["id"])
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, result)
        except INPUT_ERRORS as exc:
            self.error_json(400, input_message(exc))
        except Exception:
            self.error_json(500, internal_failure(self.command + " " + urlparse(self.path).path))
        finally:
            db.close()

    def do_DELETE(self):
        if urlparse(self.path).path == "/mcp":
            if self.guarded():
                self.send_empty(405, {"Allow": "POST"})
            return
        if urlparse(self.path).path != "/api/session":
            self.error_json(404, "not found"); return
        if not self.safe_origin():
            self.error_json(403, "cross-origin request denied"); return
        self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", 0)})


def _daily_summary_worker(stop):
    """Background upkeep, so that reading never writes: the path library, place details,
    stored daily summaries, and fuel prices. Each job fails on its own."""
    jobs = (("paths", learn_recent_paths), ("place details", enrich_saved_places),
            ("daily summary", save_scheduled_summaries), ("fuel prices", refresh_prices))  # prices at most every 12 hours
    while not stop.is_set():
        db = None
        try:
            db = open_database()
            for name, job in jobs:
                try:
                    job(db)
                except Exception as exc:
                    print(f"GeoLogs {name}: {exc}", flush=True)
        except Exception as exc:
            print("GeoLogs background work: " + str(exc), flush=True)
        finally:
            if db is not None:
                db.close()
        stop.wait(60)


def serve(host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost", "::1") and not config.api_token():
        raise ValueError("GEOLOGS_TOKEN is required when binding beyond loopback")
    server = ThreadingHTTPServer((host, int(port)), Handler)
    summary_stop = threading.Event()
    summary_thread = threading.Thread(target=_daily_summary_worker, args=(summary_stop,), daemon=True)
    summary_thread.start()
    print(f"GeoLogs browser: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        summary_stop.set()
        summary_thread.join(timeout=2)
        server.server_close()
