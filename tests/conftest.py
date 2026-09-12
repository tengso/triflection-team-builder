import time
import uuid

import pytest

from team_builder.manager import Manager
from team_builder.nostr import attestation, key, public, sign, tags, verify
from team_builder.storage import private_write


class FakeBuzz:
    def __init__(self, secret, relay, events=None, channels=None, roster=None):
        self.secret, self.pubkey, self.relay = secret, public(secret), relay
        self.events = events if events is not None else {}
        self.channels = channels if channels is not None else {}
        self.members = roster if roster is not None else {}
        self.origin = "http://test"

    def publish(self, event):
        assert verify(event) and event["pubkey"] == self.pubkey
        self.events[event["id"]] = event
        return event

    def message(self, identifier):
        return self.events[identifier]

    def head(self, kind, author, identifier=None):
        events = [
            e
            for e in self.events.values()
            if e["kind"] == kind
            and e["pubkey"] == author
            and (identifier is None or tags(e, "d") == [[identifier]])
        ]
        return max(events, key=lambda e: e["created_at"]) if events else None

    def replace(self, kind, tag_list, content):
        return self.event(kind, tag_list, content)

    def event(self, kind, tag_list, content="", head=None):
        event = sign(
            self.secret,
            kind,
            tag_list,
            content,
            max(int(time.time()), head["created_at"] + 1 if head else 0),
        )
        self.publish(event)
        h = tags(event, "h")
        if kind == 9007:
            identifier = h[0][0]
            self.channels[identifier] = {
                "id": identifier,
                "name": tags(event, "name"),
                "about": tags(event, "about"),
                "private": tags(event, "visibility") == [["private"]],
                "roles": {},
                "head": event,
            }
        if kind in (9000, 9001):
            channel = self.channels[h[0][0]]
            member = tags(event, "p")[0][0]
            if kind == 9000:
                channel["roles"][member] = tags(event, "role")[0][0]
            else:
                channel["roles"].pop(member, None)
            channel["head"] = event
        return event

    def channel(self, identifier):
        return self.channels.get(identifier)

    def roster(self):
        return self.members

    def archived(self, pubkey):
        return any(
            e["kind"] == 9035 and tags(e, "p") == [[pubkey]]
            for e in self.events.values()
        )


class FakeDocker:
    def __init__(self):
        self.running = set()
        self.stops = []

    def ready(self, name):
        return name in self.running

    def stop(self, name):
        self.stops.append(name)
        self.running.discard(name)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    owner, coa, admin, relay = key(), key(), key(), key()
    identifier = str(uuid.uuid4())
    config = {
        "id": identifier,
        "project": "tb-test",
        "owner": public(owner),
        "relay": public(relay),
        "office": str(uuid.uuid5(uuid.UUID(identifier), "channel/office")),
        "coa_auth": attestation(owner, public(coa)),
        "provider": "openrouter",
        "model": "test",
        "host_root": str(tmp_path),
        "runtime_image": "sha256:test",
        "advertised_url": "http://test:3100",
    }
    stored = {
        "admin": admin,
        "coa": coa,
        "token": "test-token",
        "provider_key": "model-secret",
    }
    private_write(tmp_path / "config.json", config)
    private_write(tmp_path / "secrets.json", stored)
    buzz = FakeBuzz(admin, public(relay))
    docker = FakeDocker()
    manager = Manager(tmp_path, buzz=buzz, docker=docker)
    manager.owner_secret = owner
    manager.actor = lambda secret, auth_tag=None: FakeBuzz(
        secret, public(relay), buzz.events, buzz.channels, buzz.members
    )
    manager.ensure_member = lambda pubkey, role="member": buzz.members.update(
        {pubkey: role}
    )

    def launch(agent):
        docker.running.add(manager.name(agent))
        agent["state"] = "running"
        manager.save_agent(agent)

    manager.launch = launch
    manager.bootstrap()
    yield manager
    manager.registry.db.close()


def message(
    manager,
    content="Create a channel",
    secret=None,
    reply=None,
    channel=None,
    timestamp=None,
):
    tag_list = [["h", channel or manager.config["office"]]]
    if reply:
        tag_list.append(["e", reply, "", "reply"])
    event = sign(secret or manager.owner_secret, 9, tag_list, content, timestamp)
    manager.buzz.events[event["id"]] = event
    return event["id"]


@pytest.fixture
def create_ops():
    return [
        {"action": "create_channel", "id": "dev", "name": "Development"},
        {
            "action": "create_agent",
            "id": "engineer",
            "name": "Software Engineer",
            "instructions": "Write and test software",
            "channels": ["dev"],
        },
    ]
