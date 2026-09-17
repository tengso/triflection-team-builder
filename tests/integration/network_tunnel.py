"""Exercise a client TCP tunnel after bootstrap completed without that tunnel."""

import json
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

import httpx

from team_builder.buzz import Buzz
from team_builder.nostr import auth, public


def exercise(state, owner_secret):
    config = json.loads((state / "config.json").read_text())
    assert config["internal_url"] == "http://relay:3000"
    client = urlsplit(config["advertised_url"])
    assert client.hostname == "127.0.0.1"
    assert client.port != config["port"]

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(
                ("127.0.0.1", config["port"]), timeout=10
            ) as upstream:
                peers = {self.request: upstream, upstream: self.request}
                while True:
                    readable, _, _ = select.select(list(peers), [], [], 10)
                    if not readable:
                        return
                    for connection in readable:
                        data = connection.recv(65536)
                        if not data:
                            return
                        peers[connection].sendall(data)

    class Tunnel(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Tunnel((client.hostname, client.port), Forward) as tunnel:
        thread = threading.Thread(target=tunnel.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(trust_env=False, timeout=10) as http:
                response = http.get(config["advertised_url"] + "/api/join-policy")
                response.raise_for_status()
                # Join policy is public deployment configuration. Test host
                # isolation on an authenticated community-data endpoint instead.
                unknown = http.post(
                    f"http://127.0.0.1:{config['port']}/query",
                    content=b"[]",
                    headers={
                        "Host": "unknown.invalid:3400",
                        "Content-Type": "application/json",
                        "Authorization": auth(
                            owner_secret,
                            "POST",
                            "http://unknown.invalid:3400/query",
                            b"[]",
                        ),
                    },
                )
                assert unknown.status_code >= 400, "Unknown host unexpectedly admitted"
            owner = Buzz(config["advertised_url"], owner_secret, config["relay"])
            try:
                office = owner.channel(config["office"])
                assert public(owner_secret) in office["roles"]
                assert owner.head(30177, public(owner_secret)) is not None
            finally:
                owner.client.close()
        finally:
            tunnel.shutdown()
            thread.join(timeout=5)
    print(
        "PASS: client tunnel, signed owner access, unknown-host rejection; agents bootstrapped without the tunnel",
        flush=True,
    )
