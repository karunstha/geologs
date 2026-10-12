"""Local dashboard/API and stateless MCP Streamable HTTP transport."""
import base64
import binascii
import json
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .ingest import FORMATS as INGEST_FORMATS, claimed_source

from .core import PLACES_PROVIDERS, Store, token_allows_source, configured_places_providers, current_location, deliver_webhook, geocode, nearby_places, place_details, places_provider, reverse_geocode, save_place_label
from .analysis import daily_briefs, daily_overview, day_summary, delete_samples, delete_source, fuel_costs, insights, place_overview, stored_daily_record, place_patterns, predict_next, resolve_place_patterns, save_scheduled_summaries, search_visits, stays, transitions, visited_places
from .fuel import fuel_status, refresh_prices, set_manual_price
from .mcp import SUPPORTED_VERSIONS, dispatch
from .postgres import INTEGRITY_ERRORS

WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".yaml": "text/yaml; charset=utf-8"}
DB_INPUT_ERRORS = (ValueError, KeyError, TypeError, sqlite3.IntegrityError) + INTEGRITY_ERRORS


def _token_file_value(path, name):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            return value
    return None


def active_token():
    """Read the mounted local token file so edits take effect without a rebuild."""
    path = os.environ.get("GEOLOGS_TOKEN_FILE")
    if not path:
        return os.environ.get("GEOLOGS_TOKEN", "")
    value = _token_file_value(path, "GEOLOGS_TOKEN")
    if value is None:
        raise RuntimeError("GEOLOGS_TOKEN_FILE has no GEOLOGS_TOKEN")
    if not value:
        raise RuntimeError("GEOLOGS_TOKEN_FILE has an empty token")
    return value


def ingest_token():
    """An optional token that can only send locations: safe to keep on a phone."""
    path = os.environ.get("GEOLOGS_TOKEN_FILE")
    value = _token_file_value(path, "GEOLOGS_INGEST_TOKEN") if path else None
    return value if value is not None else os.environ.get("GEOLOGS_INGEST_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoLogs/0.1"
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
        main, ingest = active_token(), ingest_token()
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
            store = Store()
        except Exception:
            return not main and not ingest
        try:
            for value in supplied:
                row = store.ingest_token_for(value)
                if row:
                    self.device_token = row
                    return True
            return not main and not ingest and not store.has_ingest_tokens()
        finally:
            store.close()

    def token_refuses(self, sources):
        """The message when a device token is locked to another tracker, else None."""
        locked = self.device_token["source"] if self.device_token else None
        stray = next((source for source in sources if not token_allows_source(locked, source)), None)
        if stray is None:
            return None
        return f"This token can only send as {locked}" + (" trackers" if locked.endswith(":") else "") + f", not {stray}"

    def note_token_use(self, store, source):
        if self.device_token:
            store.note_ingest_token_use(self.device_token["id"], source)

    def send_web_file(self, path):
        """The browser app: index.html, stylesheets and modules under /css/ and /js/, and the Home
        Assistant blueprint under /ha/ (public: Home Assistant fetches it, and it holds no secrets)."""
        relative = "index.html" if path == "/" else path.lstrip("/")
        parts = relative.split("/")
        kind = WEB_TYPES.get(Path(relative).suffix)
        if kind is None or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            self.error_json(404, "not found"); return
        resource = files("geologs").joinpath("web", *parts)
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
            return Store()
        except Exception as exc:
            print("GeoLogs database unavailable: " + str(exc), flush=True)
            self.error_json(503, "Database unavailable: " + str(exc).splitlines()[0][:300])
            return None

    def note_ingest(self, kind, status, message=None, device=None, accepted=0):
        try:
            store = Store()
        except Exception:
            return
        try:
            store.record_ingest(kind, status, message, device, accepted)
        except Exception:
            pass
        finally:
            store.close()

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
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = -1
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
        store = self.open_store()
        if store is None:
            return
        try:
            inserted = duplicates = 0
            for start in range(0, len(points), 1000):
                result = store.ingest(points[start:start + 1000])
                inserted += result["inserted"]
                duplicates += result["duplicates"]
                deliver_webhook(store, result["events"])
            # Retries and acknowledged non-location messages still prove the app gets through.
            store.record_ingest(kind, 200, f"{skipped} malformed fix{'' if skipped == 1 else 'es'} skipped" if skipped else None,
                                source, inserted)
            self.note_token_use(store, source)
        except DB_INPUT_ERRORS as exc:
            self.note_ingest(kind, 400, str(exc), source)
            self.error_json(400, str(exc)); return
        except Exception as exc:
            self.error_json(502, str(exc)); return
        finally:
            store.close()
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
        store = self.open_store()
        if store is None:
            return
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
            elif url.path == "/api/place-overview": value = place_overview(store, q.get("limit", 100))
            elif url.path == "/api/visits": value = search_visits(store, q.get("q", ""), q.get("start"), q.get("end"), q.get("limit", 50), q.get("timezone"))
            elif url.path == "/api/transitions": value = transitions(store, q.get("limit", 50))
            elif url.path == "/api/predict-next": value = predict_next(store)
            elif url.path == "/api/insights": value = insights(store, min(int(q.get("limit", 50)), 100))
            elif url.path == "/api/day": value = day_summary(store, q["date"], q.get("timezone") or store.settings()["timezone"], q.get("source"))
            elif url.path == "/api/daily-summary": value = daily_overview(store, q["date"], q.get("timezone"), q.get("source"))
            elif url.path == "/api/daily-summaries": value = store.daily_overviews(q.get("start"), q.get("end"), q.get("limit", 90))
            elif url.path == "/api/daily-summary/stored": value = stored_daily_record(store, q["date"])
            elif url.path == "/api/sources":
                value = {**store.source_activity(), "ingest_token": bool(ingest_token()), "formats": list(INGEST_FORMATS),
                         "now": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            elif url.path == "/api/daily-brief": value = daily_briefs(store, q["date"], q.get("end_date"))
            elif url.path == "/api/places": value = store.places(enrich=True)
            elif url.path == "/api/place-labels": value = store.place_labels()
            elif url.path == "/api/ingest-tokens":
                value = {"tokens": store.ingest_tokens(), "env_token": bool(ingest_token())}
            elif url.path == "/api/fuel":
                value = {**fuel_status(store), "last_7_days": fuel_costs(store, 7), "last_30_days": fuel_costs(store, 30)}
            elif url.path == "/api/stay-edits": value = store.stay_edits(q.get("start"), q.get("end"), q.get("source"))
            elif url.path == "/api/mode-overrides": value = store.mode_overrides()
            elif url.path == "/api/reminders": value = store.reminders()
            elif url.path == "/api/reminder-history": value = store.reminder_history(q.get("limit", 200))
            elif url.path == "/api/events": value = store.events(q.get("since_id", 0), q.get("limit", 100))
            elif url.path == "/api/geocode":
                # The page fetches coordinates only for the result you pick (/api/geocode/place).
                value = geocode(store, q.get("q", ""), q.get("lat"), q.get("lon"), resolve=0)
            elif url.path == "/api/geocode/place": value = place_details(store, q.get("id"), q.get("session"))
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
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/ingest/"):
            self.receive_locations(path[len("/api/ingest/"):], {key: value[0] for key, value in parse_qs(url.query).items()})
            return
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
        # Location senders may post native points with the ingest-only token too.
        sender = path == "/api/points" and self.ingest_authorized({key: value[0] for key, value in parse_qs(url.query).items()})
        if sender and not self.safe_origin():
            self.close_connection = True
            self.error_json(403, "cross-origin request denied"); return
        if not sender and not self.guarded():
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
        store = self.open_store()
        if store is None:
            return
        try:
            if path == "/mcp":
                result = dispatch(store, payload)
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
                result = store.ingest(payload)
                deliver_webhook(store, result["events"])
                last = payload[-1] if isinstance(payload, list) and payload else payload
                store.record_ingest("api", 200, None, str((last or {}).get("source") or "manual")[:80], result["inserted"])
                if sender:
                    self.note_token_use(store, str((last or {}).get("source") or "manual")[:80])
            elif path == "/api/ingest-tokens":
                result = store.create_ingest_token(payload)
            elif path == "/api/fuel":
                # Settings, then your own price (dated today) or a fresh EIA fetch for the new area or grade.
                fields = {key: payload[key] for key in ("currency", "fuel_economy_l100km", "fuel_grade", "fuel_price_source", "fuel_area")
                          if key in payload}
                store.save_settings(fields)
                if payload.get("price_per_l") not in (None, ""):
                    set_manual_price(store, payload["price_per_l"])
                refreshed = refresh_prices(store, force=True) if store.settings()["fuel_price_source"] == "auto" else None
                result = {**fuel_status(store), "refresh": refreshed, "last_7_days": fuel_costs(store, 7),
                          "last_30_days": fuel_costs(store, 30)}
            elif path == "/api/ingest-tokens/delete":
                result = store.delete_ingest_token(payload["id"])
            elif path == "/api/sources/delete":
                result = delete_source(store, payload.get("source"), payload.get("confirm") is True)
            elif path == "/api/ingest-devices/delete":
                result = store.delete_ingest_device(payload["kind"], payload.get("device") or "")
            elif path == "/api/observations/delete":
                result = delete_samples(store, payload.get("ids") or [], payload.get("confirm") is True)
            elif path == "/api/settings": result = store.save_settings(payload)
            elif path == "/api/places": result = store.save_place(payload)
            elif path == "/api/places/delete": result = store.delete_place(payload["id"])
            elif path == "/api/place-labels": result = save_place_label(store, payload)
            elif path == "/api/mode-overrides": result = store.save_mode_override(payload)
            elif path == "/api/stay-edits": result = store.save_stay_edit(payload)
            elif path == "/api/stay-edits/delete": result = store.delete_stay_edit(payload["id"])
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
            save_scheduled_summaries(store)
            try:
                refresh_prices(store)  # at most every 12 hours; weekly data
            except Exception as exc:
                print("GeoLogs fuel prices: " + str(exc), flush=True)
        except Exception as exc:
            print("GeoLogs daily summary: " + str(exc), flush=True)
        finally:
            if store is not None:
                store.close()
        stop.wait(60)


def serve(host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost", "::1") and not active_token():
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
