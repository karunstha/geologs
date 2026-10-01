"""Local dashboard/API and stateless MCP Streamable HTTP transport."""
import json
import hmac
import os
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import PLACES_PROVIDERS, Store, configured_places_providers, current_location, deliver_webhook, geocode, nearby_places, places_provider, reverse_geocode
from .analysis import daily_overview, day_summary, finalize_previous_day, insights, place_patterns, predict_next, resolve_place_patterns, search_visits, stays, transitions, visited_places
from .mcp import SUPPORTED_VERSIONS, dispatch
from .postgres import INTEGRITY_ERRORS

DB_INPUT_ERRORS = (ValueError, KeyError, TypeError, sqlite3.IntegrityError) + INTEGRITY_ERRORS


def active_token():
    """Read the mounted local token file so edits take effect without a rebuild."""
    path = os.environ.get("GEOTRACKER_TOKEN_FILE")
    if not path:
        return os.environ.get("GEOTRACKER_TOKEN", "")
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == "GEOTRACKER_TOKEN":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if not value:
                raise RuntimeError("GEOTRACKER_TOKEN_FILE has an empty token")
            return value
    raise RuntimeError("GEOTRACKER_TOKEN_FILE has no GEOTRACKER_TOKEN")


class Handler(BaseHTTPRequestHandler):
    server_version = "Geotracker/0.1"
    protocol_version = "HTTP/1.1"

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
        token = active_token()
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
        session = cookies.get("geotracker_session")
        return bool(session and secrets.compare_digest(session.value, self.session_value(token)))

    @staticmethod
    def session_value(token):
        return hmac.digest(token.encode(), b"geotracker-browser-session-v1", "sha256").hex()

    def session_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"geotracker_session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"

    def safe_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.netloc == self.headers.get("Host") and parsed.scheme in ("http", "https")

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
        if url.path in ("/", "/app.js", "/style.css"):
            filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[url.path]
            body = files("geotracker").joinpath("web", filename).read_bytes()
            content_type = "text/html" if filename.endswith("html") else "text/javascript" if filename.endswith("js") else "text/css"
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/mcp":
            if self.guarded():
                self.send_empty(405, {"Allow": "POST"})
            return
        if not self.guarded():
            return
        q = {key: value[0] for key, value in parse_qs(url.query).items()}
        store = Store()
        try:
            if url.path == "/api/status":
                active = places_provider(store)
                configured = configured_places_providers()
                value = {"ok": True, "geocoding": active is not None, "geocoder": active, "auth": bool(active_token()),
                         "places_providers": [{"id": name, "label": info["label"], "env": info["env"], "configured": configured[name]}
                                              for name, info in PLACES_PROVIDERS.items()]}
            elif url.path == "/api/current": value = current_location(store)
            elif url.path == "/api/points": value = store.points(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000))
            elif url.path == "/api/observations": value = store.observations(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000), q.get("offset", 0))
            elif url.path == "/api/timeline": value = store.timeline(q.get("start"), q.get("end"), q.get("source"), q.get("limit", 5000))
            elif url.path == "/api/settings": value = store.settings()
            elif url.path == "/api/stats": value = store.stats(q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/trips": value = store.trips(q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/coverage": value = store.coverage()
            elif url.path == "/api/near": value = store.near(q["lat"], q["lon"], q.get("radius_m", 250), q.get("start"), q.get("end"), q.get("limit", 100), q.get("source"))
            elif url.path == "/api/place-visits": value = store.place_visits(q["place_id"], q.get("start"), q.get("end"))
            elif url.path == "/api/stays": value = stays(store, q.get("start"), q.get("end"), q.get("source"), q.get("limit", 500))
            elif url.path == "/api/frequent-places": value = resolve_place_patterns(store, place_patterns(store, None))[0][:min(int(q.get("limit", 50)), 5000)]
            elif url.path == "/api/visited-places": value = visited_places(store, q.get("limit", 100))
            elif url.path == "/api/visits": value = search_visits(store, q.get("q", ""), q.get("start"), q.get("end"), q.get("limit", 50), q.get("timezone"))
            elif url.path == "/api/transitions": value = transitions(store, q.get("limit", 50))
            elif url.path == "/api/predict-next": value = predict_next(store)
            elif url.path == "/api/insights": value = insights(store, min(int(q.get("limit", 50)), 100))
            elif url.path == "/api/day": value = day_summary(store, q["date"], q.get("timezone") or store.settings()["timezone"], q.get("source"))
            elif url.path == "/api/daily-summary": value = daily_overview(store, q["date"], q.get("timezone"), q.get("source"))
            elif url.path == "/api/daily-summaries": value = store.daily_overviews(q.get("start"), q.get("end"), q.get("limit", 90))
            elif url.path == "/api/places": value = store.places(enrich=True)
            elif url.path == "/api/place-labels": value = store.place_labels()
            elif url.path == "/api/reminders": value = store.reminders()
            elif url.path == "/api/reminder-history": value = store.reminder_history(q.get("limit", 200))
            elif url.path == "/api/events": value = store.events(q.get("since_id", 0), q.get("limit", 100))
            elif url.path == "/api/geocode": value = geocode(store, q.get("q", ""), q.get("lat"), q.get("lon"))
            elif url.path == "/api/reverse": value = reverse_geocode(store, q["lat"], q["lon"], q.get("radius_m"))
            elif url.path == "/api/nearby-places": value = nearby_places(store, q["lat"], q["lon"], q.get("radius_m"), q.get("limit", 15))
            elif url.path == "/api/export.geojson": value = store.export_geojson(q.get("start"), q.get("end"))
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, value)
        except DB_INPUT_ERRORS as exc:
            self.error_json(400, str(exc))
        except Exception as exc:
            self.error_json(502, str(exc))
        finally:
            store.close()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            if not self.safe_origin():
                self.error_json(403, "cross-origin request denied"); return
            length = int(self.headers.get("Content-Length", 0))
            if length > 4096:
                self.error_json(413, "request too large"); return
            try:
                payload = json.loads(self.rfile.read(length))
                supplied = payload["token"]
                if not isinstance(supplied, str):
                    raise ValueError("token must be a string")
            except (ValueError, KeyError, TypeError):
                self.error_json(400, "invalid token request"); return
            configured = active_token()
            if configured and not secrets.compare_digest(supplied, configured):
                self.error_json(401, "Access token rejected. Restart the server if its configured token changed."); return
            cookie = self.session_cookie(self.session_value(configured), 30 * 86400) if configured else self.session_cookie("", 0)
            self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return
        if not self.guarded():
            return
        if path == "/mcp" and not self.mcp_headers_valid():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.close_connection = True
            if path == "/mcp":
                self.mcp_error(400, -32600, "Invalid Content-Length")
            else:
                self.error_json(400, "invalid Content-Length")
            return
        if length < 0 or length > 2_000_000:
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
        store = Store()
        try:
            if path == "/mcp":
                result = dispatch(store, payload)
                if result is None:
                    self.send_empty(202)
                else:
                    self.send_json(200, result)
                return
            if path == "/api/points":
                result = store.ingest(payload)
                deliver_webhook(store, result["events"])
            elif path == "/api/settings": result = store.save_settings(payload)
            elif path == "/api/places": result = store.save_place(payload)
            elif path == "/api/places/delete": result = store.delete_place(payload["id"])
            elif path == "/api/place-labels": result = store.save_place_label(payload)
            elif path == "/api/place-labels/delete": result = store.delete_place_label(payload["id"])
            elif path == "/api/reminders": result = store.create_reminder(payload)
            elif path == "/api/reminders/cancel": result = store.cancel_reminder(payload["id"])
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, result)
        except DB_INPUT_ERRORS as exc:
            self.error_json(400, str(exc))
        except Exception as exc:
            self.error_json(502, str(exc))
        finally:
            store.close()

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
    while not stop.is_set():
        store = None
        try:
            store = Store()
            finalize_previous_day(store)
        except Exception as exc:
            print("Geotracker daily summary: " + str(exc), flush=True)
        finally:
            if store is not None:
                store.close()
        stop.wait(60)


def serve(host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost", "::1") and not active_token():
        raise ValueError("GEOTRACKER_TOKEN is required when binding beyond loopback")
    server = ThreadingHTTPServer((host, int(port)), Handler)
    summary_stop = threading.Event()
    summary_thread = threading.Thread(target=_daily_summary_worker, args=(summary_stop,), daemon=True)
    summary_thread.start()
    print(f"Geotracker browser: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        summary_stop.set()
        summary_thread.join(timeout=2)
        server.server_close()
