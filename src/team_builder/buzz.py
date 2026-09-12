import json
import time
from urllib.parse import urlsplit

import httpx

from .nostr import auth, public, sign, tags, verify, wire


class Buzz:
    def __init__(self, origin, secret, relay, auth_tag=None, canonical_origin=None):
        self.origin, self.secret, self.relay = origin.rstrip("/"), secret, relay
        self.pubkey, self.auth_tag = public(secret), auth_tag
        self.canonical_origin = (canonical_origin or origin).rstrip("/")
        self.client = httpx.Client(timeout=30, trust_env=False)

    def call(self, method, path, data=None):
        url = self.origin + path
        body = wire(data) if data is not None else None
        headers = {
            "Authorization": auth(
                self.secret, method, self.canonical_origin + path, body
            ),
            "Host": urlsplit(self.canonical_origin).netloc,
            "Content-Type": "application/json",
        }
        if self.auth_tag:
            headers["x-auth-tag"] = wire(self.auth_tag).decode()
        response = self.client.request(method, url, content=body, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"Buzz {method} {path.split('?')[0]} failed (HTTP {response.status_code})"
            )
        return response.json()

    def query(self, filters):
        events = self.call("POST", "/query", filters)
        if not isinstance(events, list) or any(not verify(e) for e in events):
            raise RuntimeError("Buzz returned invalid signed events")
        return events

    def head(self, kind, author, identifier=None):
        f = {"kinds": [kind], "authors": [author], "limit": 1}
        if identifier is not None:
            f["#d"] = [identifier]
        events = self.query([f])
        if len(events) > 1 or any(
            e["kind"] != kind
            or e["pubkey"] != author
            or (identifier is not None and tags(e, "d") != [[identifier]])
            for e in events
        ):
            raise RuntimeError("Buzz returned a mismatched event coordinate")
        return events[0] if events else None

    def message(self, identifier):
        events = self.query([{"ids": [identifier], "limit": 1}])
        if len(events) != 1 or events[0]["id"] != identifier:
            raise ValueError("Source message was not found")
        return events[0]

    def publish(self, event):
        if not verify(event) or event["pubkey"] != self.pubkey:
            raise ValueError("Submission must be signed by the authenticated identity")
        result = self.call("POST", "/events", event)
        if result.get("accepted") is not True or result.get("event_id") != event["id"]:
            raise RuntimeError("Buzz rejected the signed event")
        return event

    def event(self, kind, tag_list, content="", head=None):
        now = int(time.time())
        timestamp = max(now, head["created_at"] + 1 if head else now)
        if timestamp > now + 120:
            raise RuntimeError(
                "Buzz timestamp too far in the future; retry after clock catches up"
            )
        return self.publish(sign(self.secret, kind, tag_list, content, timestamp))

    def replace(self, kind, tag_list, content):
        identifier = next((t[1] for t in tag_list if t[0] == "d"), None)
        head = self.head(kind, self.pubkey, identifier)
        if head and head["tags"] == tag_list and head["content"] == content:
            return head
        event = self.event(kind, tag_list, content, head)
        observed = self.head(kind, self.pubkey, identifier)
        if not observed or observed["id"] != event["id"]:
            raise RuntimeError("Buzz replaceable event failed readback")
        return event

    def roster(self):
        head = self.head(13534, self.relay)
        return {t[0]: t[1] for t in tags(head, "member") if len(t) == 2} if head else {}

    def archived(self, pubkey):
        head = self.head(13535, self.relay)
        return bool(head and [pubkey] in tags(head, "p"))

    def channel(self, identifier):
        meta = self.head(39000, self.relay, identifier)
        if not meta:
            return None
        members = self.head(39002, self.relay, identifier)
        admins = self.head(39001, self.relay, identifier)
        if not members or not admins:
            raise RuntimeError("Channel discovery records are incomplete")
        roles = {t[0]: t[-1] for t in tags(members, "p") if len(t) == 3}
        roles.update({t[0]: t[1] for t in tags(admins, "p") if len(t) == 2})
        return {
            "id": identifier,
            "name": tags(meta, "name"),
            "about": tags(meta, "about"),
            "private": any(t == ["private"] for t in meta["tags"]),
            "roles": roles,
            "head": members,
        }


def policy(name):
    return json.dumps(
        {
            "name": name,
            "parallelism": 1,
            "respond_to": "anyone",
            "respond_to_allowlist": [],
        },
        separators=(",", ":"),
    )
