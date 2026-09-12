"""Nostr wire encoding, BIP-340 signatures, NIP-98 and NIP-OA."""

import base64
import hashlib
import json
import time
import uuid

from bech32 import bech32_decode, convertbits
from coincurve import PrivateKey, PublicKeyXOnly


def wire(value):
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def key():
    return PrivateKey().secret.hex()


def parse_key(value):
    try:
        value = value.strip()
        if value.startswith("nsec1"):
            hrp, data = bech32_decode(value)
            if hrp != "nsec" or data is None:
                raise ValueError()
            decoded = convertbits(data, 5, 8, False)
            if decoded is None:
                raise ValueError()
            raw = bytes(decoded)
        else:
            raw = bytes.fromhex(value)
        if len(raw) != 32:
            raise ValueError()
        return PrivateKey(raw).secret.hex()
    except (ValueError, TypeError):
        raise ValueError("Invalid private key; use hex or nsec") from None


def public(secret):
    return PrivateKey(bytes.fromhex(secret)).public_key_xonly.format().hex()


def sign(secret, kind, tags, content="", timestamp=None):
    event = {
        "pubkey": public(secret),
        "created_at": int(time.time()) if timestamp is None else timestamp,
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    digest = hashlib.sha256(
        wire([0, event["pubkey"], event["created_at"], kind, tags, content])
    ).digest()
    return dict(
        event,
        id=digest.hex(),
        sig=PrivateKey(bytes.fromhex(secret)).sign_schnorr(digest).hex(),
    )


def verify(event):
    try:
        if type(event["kind"]) is not int or type(event["created_at"]) is not int:
            return False
        if not isinstance(event["content"], str) or not isinstance(event["tags"], list):
            return False
        if any(
            not isinstance(t, list) or any(not isinstance(v, str) for v in t)
            for t in event["tags"]
        ):
            return False
        digest = hashlib.sha256(
            wire(
                [
                    0,
                    event["pubkey"],
                    event["created_at"],
                    event["kind"],
                    event["tags"],
                    event["content"],
                ]
            )
        ).digest()
        return event["id"] == digest.hex() and PublicKeyXOnly(
            bytes.fromhex(event["pubkey"])
        ).verify(bytes.fromhex(event["sig"]), digest)
    except (KeyError, ValueError, TypeError):
        return False


def auth(secret, method, url, body=None):
    tags = [["u", url], ["method", method], ["nonce", str(uuid.uuid4())]]
    if body is not None:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    return "Nostr " + base64.b64encode(wire(sign(secret, 27235, tags))).decode()


def attestation(owner_secret, agent):
    digest = hashlib.sha256(f"nostr:agent-auth:{agent}:".encode()).digest()
    return [
        "auth",
        public(owner_secret),
        "",
        PrivateKey(bytes.fromhex(owner_secret)).sign_schnorr(digest).hex(),
    ]


def tags(event, name):
    return [t[1:] for t in event["tags"] if t and t[0] == name]


def reply_tags(parent):
    """Buzz treats root-only tags as top-level; a reply marker is mandatory."""
    marked = {
        t[2]: t[0]
        for t in tags(parent, "e")
        if len(t) >= 3 and t[2] in ("root", "reply")
    }
    root = marked.get("root", marked["reply"]) if "reply" in marked else parent["id"]
    return ([["e", root, "", "root"]] if root != parent["id"] else []) + [
        ["e", parent["id"], "", "reply"]
    ]
