"""Run inside the isolated manager container; never against a real community."""

import asyncio
import base64
import hashlib
import json
import struct
import time
import zlib
from pathlib import Path

from team_builder.buzz import Buzz
from team_builder.nostr import auth, public, sign, wire


def exercise(owner_secret):
    config = json.loads(Path("/state/config.json").read_text())
    assert config["name"] == "Image Smoke Test"
    assert public(owner_secret) == config["owner"]
    internal = Buzz(config["internal_url"], owner_secret, config["relay"])
    try:

        async def check_websocket(signed_relay, expected):
            import websockets

            async with websockets.connect(
                "ws://relay:3000", proxy=None, open_timeout=10
            ) as connection:
                challenge = json.loads(await asyncio.wait_for(connection.recv(), 10))
                assert challenge[0] == "AUTH"
                event = sign(
                    owner_secret,
                    22242,
                    [["relay", signed_relay], ["challenge", challenge[1]]],
                    "",
                )
                await connection.send(json.dumps(["AUTH", event]))
                result = json.loads(await asyncio.wait_for(connection.recv(), 10))
                assert result[0] == "OK" and result[1] == event["id"]
                assert result[2] is expected, (
                    "Unexpected WebSocket authorization outcome"
                )

        asyncio.run(check_websocket("ws://relay:3000", True))
        asyncio.run(
            check_websocket(
                config["advertised_url"].replace("http://", "ws://", 1), False
            )
        )
        assert config["owner"] in internal.channel(config["office"])["roles"]
        mismatch = internal.client.post(
            config["internal_url"] + "/query",
            content=b"[]",
            headers={
                "Authorization": auth(
                    owner_secret, "POST", config["advertised_url"] + "/query", b"[]"
                ),
                "Content-Type": "application/json",
            },
        )
        assert mismatch.status_code in (401, 403), (
            "Cross-origin HTTP signature accepted"
        )
        invite = internal.call("POST", "/api/invites", {"ttl_secs": 60, "max_uses": 1})
        assert invite["url"].startswith(config["advertised_url"] + "/invite/")

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff"))
            + chunk(b"IEND", b"")
        )
        digest = hashlib.sha256(png).hexdigest()
        event = sign(
            owner_secret,
            24242,
            [
                ["t", "upload"],
                ["x", digest],
                ["expiration", str(int(time.time()) + 300)],
                ["server", "relay:3000"],
            ],
            "Upload test image",
        )
        uploaded = internal.client.put(
            config["internal_url"] + "/upload",
            content=png,
            headers={
                "Authorization": "Nostr " + base64.b64encode(wire(event)).decode(),
                "X-SHA-256": digest,
                "Content-Type": "image/png",
            },
        )
        assert uploaded.status_code == 200, (
            f"Internal media upload failed: HTTP {uploaded.status_code}"
        )
        assert uploaded.json()["url"].startswith(config["advertised_url"] + "/media/")
        print(
            "PASS: internal membership, host-bound signatures, client-facing invites and media"
        )
    finally:
        internal.client.close()
