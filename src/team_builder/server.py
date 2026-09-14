import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .manager import Manager
from .nostr import wire


def serve(manager, address=("0.0.0.0", 8088)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, code, value):
            body = wire(value)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self.answer(
                    200 if getattr(manager, "bootstrapped", False) else 503,
                    {"ready": getattr(manager, "bootstrapped", False)},
                )
            else:
                self.answer(404, {"error": "Not found"})

        def do_POST(self):
            local_github = self.path == "/operator/github-access"
            from .github_access import operator_apply, operator_token

            if not hmac.compare_digest(
                self.headers.get("Authorization", ""),
                "Bearer "
                + (
                    operator_token(manager.secrets)
                    if local_github
                    else manager.secrets["token"]
                ),
            ):
                self.answer(403, {"error": "COA authentication required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 262144:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                with manager.lock:
                    if local_github:
                        result = operator_apply(manager, **body)
                        self.answer(200, result)
                        return
                    methods = {
                        "/inspect": manager.inspect,
                        "/projects": manager.inspect_projects,
                        "/credential": manager.store_provider_credential,
                        "/propose": manager.propose,
                        "/execute": manager.execute,
                        "/approve": manager.approve,
                    }
                    if self.path not in methods:
                        self.answer(404, {"error": "Not found"})
                        return
                    if self.path == "/execute" and set(body) != {
                        "source_event_id",
                        "operations",
                    }:
                        raise ValueError("Invalid direct execution parameters")
                    result = methods[self.path](**body)
                self.answer(200, result)
            except (ValueError, TypeError) as exc:
                # Pydantic errors include raw input; do not return them.
                self.answer(
                    400,
                    {
                        "error": str(exc)
                        if type(exc) is ValueError
                        else "Invalid request"
                    },
                )
            except Exception:  # noqa: BLE001 -- contain failures at the HTTP boundary without leaking secrets
                self.answer(
                    503,
                    {
                        "error": "Management operation unavailable; retry the same request"
                    },
                )

    return ThreadingHTTPServer(address, Handler)


def main():
    manager = Manager(os.environ.get("TEAM_BUILDER_STATE", "/state"))

    from .dashboard import start

    start(manager)

    def bootstrap():
        while True:
            try:
                manager.bootstrap()
                manager.bootstrapped = True
                print("Community manager ready", flush=True)
                return
            except Exception as exc:  # noqa: BLE001 -- recover bootstrap after transient dependency failures
                print(
                    f"Bootstrap pending ({str(exc) if type(exc) in (RuntimeError, ValueError) else type(exc).__name__}); retrying in 10 seconds",
                    flush=True,
                )
                time.sleep(10)

    threading.Thread(target=bootstrap, daemon=True).start()
    serve(manager).serve_forever()
