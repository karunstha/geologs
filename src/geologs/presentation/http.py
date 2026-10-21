"""Local dashboard/API and stateless MCP Streamable HTTP transport."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config
from ..domain.access import token_allows_source
from ..domain.accounts import SESSION_PREFIX
from ..services import open_pool
from ..services.accounts import (SESSION_DAYS, CodeNeeded, TooManyAttempts, accept_invite, account, api_token_user,
                                 begin_two_factor, change_email, change_password, confirm_two_factor, create_api_token,
                                 end_session, end_sessions, forget_expired_sessions, invite_details, owner, public_user,
                                 rename, request_password_reset, reset_password, revoke_api_token, send_verification,
                                 session_user, sign_in, sign_up, start_session, stop_two_factor, verify_email)
from ..services.admin import (audit as admin_audit, clear_failures as admin_clear_failures, delete_user as admin_delete_user,
                             health as admin_health, invite as admin_invite, note_failed_attempt, refresh_user_stats,
                             require_admin, reset_link as admin_reset_link, revoke_invite as admin_revoke_invite,
                             set_disabled as admin_set_disabled, set_role as admin_set_role,
                             set_signup_mode as admin_set_signup, signup_mode, users as admin_users)
from ..services import jobs
from ..services.mail import can_send, deliver_due, waiting as mail_waiting
from ..services.ambient import ambient
from ..services.corrections import delete_stay_edit, save_mode_override, save_stay_edit
from ..services.day import day_summary
from ..services.devices import create_ingest_token, delete_ingest_device, delete_ingest_token, ingest_token_for, record_ingest, source_activity
from ..services.fuel import fuel_cost_windows, fuel_status, refresh_prices, update_fuel
from ..services.ingest import record_locations
from ..services.location import current_location
from ..services.overview import daily_briefs, daily_overview, daily_overviews, save_scheduled_summaries, stored_daily_record
from ..services.paths import exclude_path, include_path, learn_path_history, learn_recent_paths, list_paths
from ..services.periods import period_summary
from ..services.place_routes import route_map
from ..services.patterns import insights, place_overview, place_patterns, predict_next, resolve_place_patterns, search_visits, transitions, visited_places
from ..services.places import geocode, nearby_places, place_details, places_provider, reverse_geocode
from ..services.reminders import cancel_reminder, create_reminder, delete_reminder
from ..services.imports import import_file
from ..services.samples import delete_samples, delete_source, export_csv, export_geojson, export_gpx, near, stats
from ..services.saved_places import delete_label, delete_place, enrich_saved_places, place_visits, save_place, save_place_label
from ..services.settings import place_providers, save_settings
from ..services.stays import anchored_trips, stays
from ..services.tracker_alerts import check_quiet_trackers, quiet_alerts, set_quiet_alert, tracker_notices
from .errors import INPUT_ERRORS, input_message, internal_failure
from .ingest_formats import claimed_source, FORMATS as INGEST_FORMATS
from .mcp import dispatch, SUPPORTED_VERSIONS

# Posted from links in emails, before anyone is signed in (see Handler.public_post).
PUBLIC_POSTS = ("/api/password/forgot", "/api/password/reset", "/api/invites/accept", "/api/email/verify",
                "/api/signup")

WEB_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".yaml": "text/yaml; charset=utf-8", ".webmanifest": "application/manifest+json", ".png": "image/png"}
# What installs the app on a phone: its manifest and icons, and the service worker that opens it offline
# (served from the root, so it may look after every page).
APP_FILES = ("/manifest.webmanifest", "/sw.js")


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

    def send_download(self, text, content_type, filename):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

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

    def cookie(self, name):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookies.get(name)
        return morsel.value if morsel else None

    session_token = None  # the session cookie a request signed in with, if it did

    def request_user(self, db):
        """Who sent the request, or None: a personal API token or GEOLOGS_TOKEN (the owner) as a
        bearer, or a signed-in browser's session cookie. With no GEOLOGS_TOKEN configured (a
        loopback-only server), every request is the owner's."""
        main = config.api_token()
        if not main:
            return owner(db)
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            supplied = bearer[7:]
            if secrets.compare_digest(supplied, main):
                return owner(db)
            user = api_token_user(db, supplied)
            if user:
                return user
        session = self.cookie("geologs_session")
        if session and session.startswith(SESSION_PREFIX):
            user = session_user(db, session)
            self.session_token = session if user else None
            return user
        # A browser signed in before accounts existed holds a cookie made from GEOLOGS_TOKEN: the owner's.
        if session and secrets.compare_digest(session, self.session_value(main)):
            return owner(db)
        return None

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
        GPSLogger). A device token from Settings sends as the user who made it, and is
        kept on self.device_token so its tracker lock can be checked once the points are
        read; GEOLOGS_TOKEN and the .env ingest token send as the owner, and a signed-in
        browser or personal API token as its user. With no token configured anywhere,
        anyone may send, as the owner (a loopback-only server). Sets self.user."""
        self.device_token, self.user = None, None
        main, ingest = config.api_token(), config.ingest_token()
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
        try:
            db = self.server.pool.open()
        except Exception:
            return False
        try:
            if main:
                self.user = self.request_user(db)
                if self.user:
                    return True
            if any(secrets.compare_digest(value, token) for value in supplied for token in (main, ingest) if token):
                self.user = owner(db)
                return True
            for value in supplied:
                row = ingest_token_for(db, value)
                if row:
                    self.device_token, self.user = row, {"id": row["user_id"]}
                    return True
            if not main and not ingest and not db.devices.has_tokens():
                self.user = owner(db)
                return True
            return False
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
        """The browser app: index.html, stylesheets and modules under /css/ and /js/, what installs it
        (APP_FILES, /icons/), and the Home Assistant blueprint under /ha/ (public: Home Assistant
        fetches it, and it holds no secrets)."""
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
        """The store, working for the request's user (see guarded and ingest_authorized), or a 503
        the browser can show (instead of a dropped connection) when the database is down."""
        try:
            return self.server.pool.open(self.user["id"] if self.user else None)
        except Exception as exc:
            print("GeoLogs database unavailable: " + str(exc), flush=True)
            self.error_json(503, "Database unavailable: " + str(exc).splitlines()[0][:300])
            return None

    def note_ingest(self, kind, status, message=None, device=None, accepted=0):
        """Note a tracker app's request for its user's Settings → Devices. A request with a wrong
        token is no one's: it is noted for admins instead (Admin → Server)."""
        if not self.user:
            try:
                db = self.open_store()
                if db is not None:
                    try:
                        note_failed_attempt(db, kind, device, status, message, self.client_address[0])
                    finally:
                        db.close()
            except Exception:
                pass
            return
        try:
            db = self.open_store()
            if db is None:
                return
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

    def start_session(self):
        """POST /api/session: sign a browser in with an email and password, or with GEOLOGS_TOKEN (the owner)."""
        if not self.safe_origin():
            self.error_json(403, "cross-origin request denied"); return
        length = self.request_length()
        if not 0 <= length <= 4096:
            self.close_connection = True
            self.error_json(413 if length > 4096 else 400, "invalid request size"); return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not all(isinstance(payload.get(key, ""), str)
                                                        for key in ("token", "email", "password", "code")):
                raise ValueError("sign-in fields must be strings")
        except (ValueError, TypeError):
            self.error_json(400, "invalid sign-in request"); return
        configured = config.api_token()
        if "email" not in payload and not configured:
            # Nothing to sign in to: a loopback server with no token lets everyone in as the owner.
            self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", 0)}); return
        self.user = None
        db = self.open_store()
        if db is None:
            return
        try:
            if "email" in payload:
                try:
                    user = sign_in(db, payload.get("email"), payload.get("password"), self.client_address[0],
                                   payload.get("code"))
                except TooManyAttempts as exc:
                    self.error_json(429, str(exc)); return
                except CodeNeeded as exc:
                    self.send_json(401, {"error": str(exc), "needs_code": True}); return
                except ValueError as exc:
                    self.error_json(401, str(exc)); return
            elif secrets.compare_digest(payload.get("token", ""), configured):
                user = owner(db)
            else:
                self.error_json(401, "Access token rejected. Restart the server if its configured token changed."); return
            token = start_session(db, user["id"], self.headers.get("User-Agent"))
        except Exception:
            self.error_json(500, internal_failure("POST /api/session")); return
        finally:
            db.close()
        self.send_json(200, {"ok": True, "user": public_user(user)},
                       {"Set-Cookie": self.session_cookie(token, SESSION_DAYS * 86400)})

    def public_post(self, path):
        """The account links people open before they can sign in: asking for a password reset,
        setting the new password, accepting an invite (which signs the new user in), and
        confirming an address."""
        if not self.safe_origin():
            self.error_json(403, "cross-origin request denied"); return
        length = self.request_length()
        if not 0 <= length <= 4096:
            self.close_connection = True
            self.error_json(413 if length > 4096 else 400, "invalid request size"); return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self.error_json(400, "invalid JSON"); return
        self.user = None
        db = self.open_store()
        if db is None:
            return
        cookie = None
        try:
            if path == "/api/password/forgot":
                result = request_password_reset(db, payload.get("email"), self.client_address[0])
            elif path == "/api/password/reset":
                result = {"user": reset_password(db, payload.get("token"), payload.get("password"))}
            elif path in ("/api/invites/accept", "/api/signup"):
                user = (accept_invite(db, payload.get("token"), payload.get("password"), payload.get("name"))
                        if path == "/api/invites/accept" else
                        public_user(sign_up(db, payload.get("email"), payload.get("password"), payload.get("name"),
                                            self.client_address[0])))
                cookie = self.session_cookie(start_session(db, user["id"], self.headers.get("User-Agent")), SESSION_DAYS * 86400)
                result = {"user": user}
            else:  # /api/email/verify
                result = {"user": verify_email(db, payload.get("token"))}
        except TooManyAttempts as exc:
            self.error_json(429, str(exc)); return
        except PermissionError as exc:
            self.error_json(403, str(exc)); return
        except INPUT_ERRORS as exc:
            self.error_json(400, input_message(exc)); return
        except Exception:
            self.error_json(500, internal_failure("POST " + path)); return
        finally:
            db.close()
        self.send_json(200, result, {"Set-Cookie": cookie} if cookie else None)

    def guarded(self):
        """Whether the request may go on, as its user (self.user, for open_store); else a 401,
        403 or 503 is sent."""
        self.user, self.session_token = None, None
        db = self.open_store()
        if db is None:
            return False
        try:
            self.user = self.request_user(db)
        finally:
            db.close()
        if not self.user:
            if self.command == "POST":
                self.close_connection = True
            self.error_json(401, "Sign in, or send a bearer token")
            return False
        if not self.safe_origin():
            if self.command == "POST":
                self.close_connection = True
            self.error_json(403, "cross-origin request denied")
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path in APP_FILES or url.path.startswith(("/css/", "/js/", "/ha/", "/icons/")):
            self.send_web_file(url.path)
            return
        if url.path == "/mcp":
            if self.guarded():
                self.send_empty(405, {"Allow": "POST"})
            return
        if url.path.startswith("/api/ingest/"):
            self.receive_locations(url.path[len("/api/ingest/"):], {key: value[0] for key, value in parse_qs(url.query).items()})
            return
        if url.path == "/api/sign-in-options":
            # Public: what the sign-in screen can offer here.
            self.user = None
            db = self.open_store()
            if db is None:
                return
            try:
                mode = signup_mode(db)
            finally:
                db.close()
            self.send_json(200, {"email": can_send(), "token": bool(config.api_token()), "signup": mode == "open"})
            return
        if url.path == "/api/invite":
            self.user = None
            db = self.open_store()
            if db is None:
                return
            try:
                details = invite_details(db, (parse_qs(url.query).get("token") or [""])[0])
            finally:
                db.close()
            if details is None:
                self.error_json(404, "this invite has expired or was already used")
            else:
                self.send_json(200, details)
            return
        if not self.guarded():
            return
        q = {key: value[0] for key, value in parse_qs(url.query).items()}
        db = self.open_store()
        if db is None:
            return
        try:
            if url.path == "/api/me": value = {"user": public_user(self.user), "can_email": can_send()}
            elif url.path == "/api/account": value = account(db, self.user["id"], self.session_token)
            elif url.path == "/api/invites":
                require_admin(self.user)
                value = db.users.invites(datetime.now(timezone.utc).isoformat(timespec="seconds"))
            elif url.path.startswith("/api/admin/"):
                require_admin(self.user)
                if url.path == "/api/admin/users": value = admin_users(db)
                elif url.path == "/api/admin/audit": value = admin_audit(db, q.get("limit", 200))
                elif url.path == "/api/admin/health": value = admin_health(db)
                else:
                    self.error_json(404, "not found"); return
            elif url.path == "/api/status":
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
            elif url.path == "/api/coverage": value = db.points.coverage(gaps=q.get("gaps") != "false")
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
            elif url.path == "/api/period-summary": value = period_summary(db, q["period"])
            elif url.path == "/api/daily-summary/stored": value = stored_daily_record(db, q["date"])
            elif url.path == "/api/sources":
                value = {**source_activity(db), "ingest_token": bool(config.ingest_token()), "formats": list(INGEST_FORMATS),
                         "quiet_alerts": quiet_alerts(db), "now": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            elif url.path == "/api/tracker-notices": value = tracker_notices(db, q.get("since_id", 0))
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
            elif url.path in EXPORTS:
                export, content_type = EXPORTS[url.path]
                self.send_download(export(db, q.get("start"), q.get("end")), content_type, "geologs" + url.path[len("/api/export"):])
                return
            elif url.path == "/api/import": value = import_status(self.user["id"])
            elif url.path == "/api/paths": value = list_paths(db, q.get("limit", 200))
            elif url.path == "/api/route-map": value = route_map(db, q["south"], q["west"], q["north"], q["east"], q.get("limit", 300))
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, value)
        except PermissionError as exc:
            self.error_json(403, str(exc))
        except INPUT_ERRORS as exc:
            self.error_json(400, input_message(exc))
        except Exception:
            self.error_json(500, internal_failure(self.command + " " + urlparse(self.path).path))
        finally:
            db.close()

    def receive_import(self, source):
        """Save an uploaded history file, then import it in the background; GET /api/import says how
        it is going. One import at a time for each person."""
        length = self.request_length()
        if length <= 0 or length > IMPORT_MAX_BYTES:
            self.close_connection = True
            self.error_json(413 if length > IMPORT_MAX_BYTES else 400, f"Choose a file of at most "
                            f"{IMPORT_MAX_BYTES // 1_000_000} MB" if length > IMPORT_MAX_BYTES else "Choose a file to import")
            return
        user_id = self.user["id"]
        with _imports_lock:
            if (_imports.get(user_id) or {}).get("state") == "running":
                self.close_connection = True
                self.error_json(409, "An import is already running")
                return
            _imports[user_id] = {"state": "running", "phase": "uploading", "inserted": 0, "duplicates": 0,
                                 "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        handle, path = tempfile.mkstemp(prefix="geologs-import-")
        try:
            with os.fdopen(handle, "wb") as out:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("the upload stopped")
                    out.write(chunk)
                    remaining -= len(chunk)
        except (OSError, ConnectionError):
            os.unlink(path)
            _import_progress(user_id, state="failed", phase=None, error="The upload was interrupted. Try again.")
            self.close_connection = True
            return
        threading.Thread(target=_run_import, args=(self.server.pool, user_id, path, source), daemon=True).start()
        self.send_json(202, import_status(user_id))

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        if path.startswith("/api/ingest/"):
            self.receive_locations(path[len("/api/ingest/"):], {key: value[0] for key, value in parse_qs(url.query).items()})
            return
        if path == "/api/session":
            self.start_session()
            return
        if path in PUBLIC_POSTS:
            self.public_post(path)
            return
        # Location senders may post native points with the ingest-only token too.
        sender = path == "/api/points" and self.ingest_authorized({key: value[0] for key, value in parse_qs(url.query).items()})
        if sender and not self.safe_origin():
            self.close_connection = True
            self.error_json(403, "cross-origin request denied"); return
        if not sender and not self.guarded():
            return
        if path == "/api/import":
            self.receive_import((parse_qs(url.query).get("source") or [""])[0])
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
            elif path == "/api/invites":
                require_admin(self.user)
                result = admin_invite(db, self.user, payload.get("email"), payload.get("role") or "user", self.client_address[0])
            elif path.startswith("/api/admin/"):
                require_admin(self.user)
                actor, client = self.user, self.client_address[0]
                if path == "/api/admin/users/role": result = admin_set_role(db, actor, payload["id"], payload.get("role"), client)
                elif path == "/api/admin/users/disable":
                    result = admin_set_disabled(db, actor, payload["id"], payload.get("disabled") is not False, client)
                elif path == "/api/admin/users/reset": result = admin_reset_link(db, actor, payload["id"], client)
                elif path == "/api/admin/users/delete": result = admin_delete_user(db, actor, payload["id"], payload.get("email"), client)
                elif path == "/api/admin/invites/revoke": result = admin_revoke_invite(db, actor, payload.get("email"), client)
                elif path == "/api/admin/signup": result = admin_set_signup(db, actor, payload.get("mode"), client)
                elif path == "/api/admin/failures/clear": result = admin_clear_failures(db, actor, client)
                else:
                    self.error_json(404, "not found"); return
            elif path == "/api/account/2fa/begin": result = begin_two_factor(db, self.user["id"])
            elif path == "/api/account/2fa/confirm": result = confirm_two_factor(db, self.user["id"], payload.get("code"))
            elif path == "/api/account/2fa/stop": result = stop_two_factor(db, self.user["id"], payload.get("current"))
            elif path == "/api/account": result = rename(db, self.user["id"], payload.get("name"))
            elif path == "/api/account/password":
                result = change_password(db, self.user["id"], payload.get("current"), payload.get("password"), self.session_token)
            elif path == "/api/account/email":
                result = change_email(db, self.user["id"], payload.get("email"), payload.get("current"))
            elif path == "/api/account/sessions/end":
                result = end_sessions(db, self.user["id"], payload.get("id"), self.session_token)
            elif path == "/api/account/tokens": result = create_api_token(db, self.user["id"], payload.get("name"))
            elif path == "/api/account/tokens/delete": result = revoke_api_token(db, self.user["id"], payload["id"])
            elif path == "/api/email/verify/send":
                result = {"sent": send_verification(db, self.user["id"])}
            elif path == "/api/fuel":
                result = update_fuel(db, payload)
            elif path == "/api/ingest-tokens/delete":
                result = delete_ingest_token(db, payload["id"])
            elif path == "/api/sources/delete":
                result = delete_source(db, payload.get("source"), payload.get("confirm") is True)
            elif path == "/api/tracker-alerts":
                result = {"quiet_alerts": set_quiet_alert(db, payload)}
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
            elif path == "/api/reminders/delete": result = delete_reminder(db, payload["id"])
            elif path == "/api/reminders/clear-history": result = delete_reminder(db)
            else:
                self.error_json(404, "not found"); return
            self.send_json(200, result)
        except PermissionError as exc:
            self.error_json(403, str(exc))
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
        session = self.cookie("geologs_session")
        if session:
            self.user = None
            db = self.open_store()
            if db is None:
                return
            try:
                end_session(db, session)
            finally:
                db.close()
        self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", 0)})


# Downloads of every fix in a range, beside /api/export.geojson.
EXPORTS = {"/api/export.csv": (export_csv, "text/csv; charset=utf-8"),
           "/api/export.gpx": (export_gpx, "application/gpx+xml; charset=utf-8")}
IMPORT_MAX_BYTES = 1_000_000_000
# How each person's latest import went (kept in memory: an import outlives its request).
_imports = {}
_imports_lock = threading.Lock()


def import_status(user_id):
    with _imports_lock:
        return dict(_imports.get(user_id) or {"state": "idle"})


def _import_progress(user_id, **changes):
    with _imports_lock:
        _imports[user_id] = {**_imports.get(user_id, {}), **changes}


def _run_import(pool, user_id, path, source):
    db = None
    try:
        db = pool.open(user_id)
        totals = import_file(db, path, source, progress=lambda progress: _import_progress(user_id, **progress))
        _import_progress(user_id, **totals, state="done", phase=None)
    except INPUT_ERRORS as exc:
        _import_progress(user_id, state="failed", phase=None, error=input_message(exc))
    except Exception:
        internal_failure("import")
        _import_progress(user_id, state="failed", phase=None, error="The import failed on the server; its log says why.")
    finally:
        _import_progress(user_id, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if db is not None:
            db.close()
        os.unlink(path)


def _daily_summary_worker(pool, stop):
    """Background upkeep, so that reading never writes: for each user, the path library, place
    details, stored daily summaries, and fuel prices; then expired sessions. Each job fails on its own."""
    work = (("paths", learn_recent_paths), ("place details", enrich_saved_places),
            ("daily summary", save_scheduled_summaries), ("fuel prices", refresh_prices),  # prices at most every 12 hours
            ("user counts", refresh_user_stats), ("quiet trackers", check_quiet_trackers))
    while not stop.is_set():
        db = None
        try:
            db = pool.open()
            for user_id in db.users.active_ids():
                db.as_user(user_id)
                for name, job in work:
                    started = time.perf_counter()
                    try:
                        job(db)
                        jobs.record(name, started)
                    except Exception as exc:
                        print(f"GeoLogs {name} (user {user_id}): {exc}", flush=True)
                        db.rollback()
                        jobs.record(name, started, exc)
            db.as_user(None)
            forget_expired_sessions(db)
        except Exception as exc:
            print("GeoLogs background work: " + str(exc), flush=True)
        finally:
            if db is not None:
                db.close()
        stop.wait(60)


def _mail_worker(pool, stop):
    """Send queued email: woken when a message is queued, and at least every 30 seconds for retries."""
    while not stop.is_set():
        mail_waiting.wait(30)
        mail_waiting.clear()
        if stop.is_set():
            break
        stop.wait(0.5)  # let the request that queued the message finish saving it
        db, started = None, time.perf_counter()
        try:
            db = pool.open()
            deliver_due(db)
            jobs.record("email", started)
        except Exception as exc:
            print("GeoLogs mail: " + str(exc), flush=True)
            jobs.record("email", started, exc)
        finally:
            if db is not None:
                db.close()


class Server(ThreadingHTTPServer):
    """The HTTP server, with the database pool its requests borrow connections from."""
    # Python's default backlog of 5 refuses connections when a few dozen clients arrive at once.
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, address, pool):
        super().__init__(address, Handler)
        self.pool = pool


def serve(host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost", "::1") and not config.api_token():
        raise ValueError("GEOLOGS_TOKEN is required when binding beyond loopback")
    pool = open_pool()
    server = Server((host, int(port)), pool)
    summary_stop = threading.Event()
    summary_thread = threading.Thread(target=_daily_summary_worker, args=(pool, summary_stop), daemon=True)
    summary_thread.start()
    mail_thread = threading.Thread(target=_mail_worker, args=(pool, summary_stop), daemon=True)
    mail_thread.start()
    print(f"GeoLogs browser: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        summary_stop.set()
        mail_waiting.set()
        summary_thread.join(timeout=2)
        mail_thread.join(timeout=2)
        server.server_close()
        pool.close()
