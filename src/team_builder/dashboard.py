"""Mission Control observations, owner sessions, and scoped agent restart."""

import json
import re
import threading
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .dashboard_access import Access

PREFIX = "/dashboard/api/v1"
COOKIE = "tb_dashboard"
ASSETS = {
    "/dashboard/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}


def serve(observer, address=("0.0.0.0", 8089)):
    access = Access(observer.root)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def reply(self, code, value, content_type="application/json", cookie=None):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return cookie[COOKIE].value if COOKIE in cookie else None
            except Exception:  # noqa: BLE001 -- invalid cookies are unauthenticated
                return None

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path in ("/", "/dashboard"):
                self.send_response(302)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if url.path in ASSETS:
                name, mime = ASSETS[url.path]
                self.reply(
                    200,
                    files("team_builder").joinpath("resources", name).read_bytes(),
                    mime,
                )
                return
            if not access.valid(self.session()):
                self.reply(401, {"error": "Owner sign-in required"})
                return
            routes = {
                PREFIX + "/" + key: key
                for key in (
                    "overview",
                    "agents",
                    "services",
                    "channels",
                    "projects",
                    "activity",
                )
            }
            if url.path == PREFIX + "/logs":
                try:
                    query = parse_qs(url.query, strict_parsing=True)
                    if set(query) != {"component"} or len(query["component"]) != 1:
                        raise ValueError("Invalid component")
                    self.reply(200, observer.logs(query["component"][0]))
                except ValueError:
                    self.reply(400, {"error": "Unknown or unavailable component"})
                except Exception:  # noqa: BLE001 -- diagnostics never reflect raw errors
                    self.reply(503, {"error": "Logs unavailable; retry later"})
                return
            if url.path not in routes:
                self.reply(404, {"error": "Not found"})
                return
            snapshot = observer.read()
            key = routes[url.path]
            if key == "overview":
                self.reply(200, snapshot)
            else:
                result = {
                    "collected_at": snapshot["collected_at"],
                    "stale": snapshot["stale"],
                    "errors": snapshot["errors"],
                    key: snapshot[key],
                }
                if key == "projects":
                    result["repositories"] = snapshot["repositories"]
                self.reply(200, result)

        def do_POST(self):
            restart = re.fullmatch(
                re.escape(PREFIX) + r"/agents/([a-z0-9][a-z0-9_-]{0,63})/restart",
                self.path,
            )
            if not restart and self.path not in (PREFIX + "/login", PREFIX + "/logout"):
                self.reply(405, {"error": "Unsupported dashboard action"})
                return
            # Requiring a browser Origin blocks cross-origin login and logout.
            if (
                self.headers.get("Origin") != "http://" + self.headers.get("Host", "")
                or self.headers.get("Content-Type") != "application/json"
            ):
                self.reply(403, {"error": "Same-origin JSON request required"})
                return
            if restart and not access.valid(self.session()):
                self.reply(401, {"error": "Owner sign-in required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid body")
                body = json.loads(self.rfile.read(length))
                if restart:
                    if (
                        not isinstance(body, dict)
                        or set(body) != {"request_id"}
                        or not isinstance(body["request_id"], str)
                    ):
                        raise ValueError("Invalid body")
                    request_id = str(uuid.UUID(body["request_id"]))
                    status, result = observer.manager.dashboard_restart(
                        restart[1], request_id
                    )
                    self.reply(status, result)
                    return
                if self.path == PREFIX + "/logout":
                    if body != {}:
                        raise ValueError("Invalid body")
                    access.logout(self.session())
                    self.reply(
                        200,
                        {"ok": True},
                        cookie=f"{COOKIE}=; Path=/dashboard; HttpOnly; SameSite=Strict; Max-Age=0",
                    )
                    return
                if (
                    not isinstance(body, dict)
                    or set(body) != {"key"}
                    or not isinstance(body["key"], str)
                ):
                    raise ValueError("Invalid body")
                status, token = access.login(body["key"], self.client_address[0])
                self.reply(
                    status,
                    {"ok": True}
                    if token
                    else {
                        "error": "Too many attempts; wait one minute"
                        if status == 429
                        else "Invalid access key"
                    },
                    cookie=f"{COOKIE}={token}; Path=/dashboard; HttpOnly; SameSite=Strict; Max-Age=28800"
                    if token
                    else None,
                )
            except (ValueError, OSError):
                self.reply(400, {"error": "Invalid request"})
            except Exception:  # noqa: BLE001 -- contain private internal diagnostics
                self.reply(
                    503,
                    {
                        "error": "Dashboard operation unavailable; inspect health before retrying"
                    },
                )

        def do_PUT(self):
            self.reply(405, {"error": "Mission Control is read-only"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


def start(manager):
    if not manager.config.get("dashboard", {}).get("enabled"):
        return
    from .dashboard_observe import Observer

    observer = Observer(manager)
    server = serve(observer)
    threading.Thread(
        target=observer.run, daemon=True, name="dashboard-observer"
    ).start()
    threading.Thread(
        target=server.serve_forever, daemon=True, name="dashboard-http"
    ).start()
