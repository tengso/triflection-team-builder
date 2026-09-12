import hashlib
import json
import time
import uuid
from pathlib import Path
from threading import RLock

from .buzz import Buzz, policy
from .docker import Docker
from .models import validate
from .nostr import attestation, key, public, reply_tags, sign, tags, wire
from .runtime import start_agent
from .storage import Registry


class Manager:
    def __init__(self, root, *, buzz=None, docker=None):
        self.root = Path(root)
        self.config = json.loads((self.root / "config.json").read_text())
        self.secrets = json.loads((self.root / "secrets.json").read_text())
        self.registry = Registry(self.root / "registry.sqlite3")
        self.buzz = buzz or Buzz(
            "http://relay:3000",
            self.secrets["admin"],
            self.config["relay"],
            canonical_origin=self.config["advertised_url"],
        )
        self.docker = docker or Docker(self.config["project"])
        self.lock = RLock()

    def actor(self, secret, auth_tag=None):
        return Buzz(
            self.buzz.origin,
            secret,
            self.config["relay"],
            auth_tag,
            canonical_origin=self.config["advertised_url"],
        )

    def resource(self, identifier, kind):
        item = self.registry.get(kind + "/" + identifier)
        if not item:
            raise ValueError(f"Unknown {kind}: {identifier}")
        return item

    def save_agent(self, agent):
        self.registry.put("agent/" + agent["id"], "agent", agent)

    def name(self, agent):
        return self.config["project"] + "-agent-" + agent["id"]

    def ensure_member(self, pubkey, role="member"):
        if self.buzz.roster().get(pubkey) == role:
            return
        # Relay roster events are second-granularity replaceables.
        time.sleep(1.05)
        self.docker.exec(
            self.config["project"] + "-relay-1",
            [
                "/usr/local/bin/buzz-admin",
                "add-member",
                "--pubkey",
                pubkey,
                "--role",
                role,
            ],
        )
        if self.buzz.roster().get(pubkey) != role:
            raise RuntimeError("Community membership failed readback")

    def membership(self, channel_id, pubkey, role="bot", remove=False):
        observed = self.buzz.channel(channel_id)
        if not observed:
            raise ValueError("Channel does not exist")
        if (remove and pubkey not in observed["roles"]) or (
            not remove and observed["roles"].get(pubkey) == role
        ):
            return
        tag_list = [["h", channel_id], ["p", pubkey]]
        if not remove:
            tag_list.append(["role", role])
        self.buzz.event(9001 if remove else 9000, tag_list, head=observed["head"])
        actual = self.buzz.channel(channel_id)["roles"]
        if (remove and pubkey in actual) or (not remove and actual.get(pubkey) != role):
            raise RuntimeError("Channel membership failed authoritative readback")

    def register_agent(self, agent):
        self.ensure_member(agent["pubkey"])
        actor = self.actor(agent["secret"], agent["auth_tag"])
        actor.replace(
            0,
            [agent["auth_tag"]],
            wire({"name": agent["id"], "display_name": agent["name"]}).decode(),
        )
        if agent["id"] != "coa":
            owner = self.actor(self.secrets["coa"], self.config["coa_auth"])
            owner.replace(30177, [["d", agent["pubkey"]]], policy(agent["name"]))

    def channel(self, operation):
        identifier = operation["id"]
        if identifier == "office" and operation["action"] == "update_channel":
            raise ValueError("Office Of COA is protected")
        existing = self.registry.get("channel/" + identifier)
        if operation["action"] == "update_channel" and not existing:
            raise ValueError("Channel does not exist")
        if (
            existing
            and operation["action"] == "create_channel"
            and any(
                existing.get(k) != operation.get(k)
                for k in ("name", "description", "visibility")
            )
        ):
            raise ValueError("Channel ID already exists with different configuration")
        channel = {
            **(existing or {}),
            **operation,
            "uuid": existing["uuid"]
            if existing
            else str(uuid.uuid5(uuid.UUID(self.config["id"]), "channel/" + identifier)),
        }
        self.registry.put("channel/" + identifier, "channel", channel)
        observed = self.buzz.channel(channel["uuid"])
        if not observed:
            self.buzz.event(
                9007,
                [
                    ["h", channel["uuid"]],
                    ["name", channel["name"]],
                    ["about", channel["description"]],
                    [
                        "visibility",
                        "private" if channel["visibility"] == "private" else "open",
                    ],
                    ["channel_type", "stream"],
                ],
            )
        elif observed["name"] != [[channel["name"]]] or observed["about"] != (
            [[channel["description"]]] if channel["description"] else []
        ):
            head = self.buzz.head(39000, self.config["relay"], channel["uuid"])
            self.buzz.event(
                9002,
                [
                    ["h", channel["uuid"]],
                    ["name", channel["name"]],
                    ["about", channel["description"]],
                ],
                head=head,
            )
        self.membership(channel["uuid"], self.config["owner"], "owner")
        actual = self.buzz.channel(channel["uuid"])
        if actual["name"] != [[channel["name"]]] or actual["private"] != (
            channel["visibility"] == "private"
        ):
            raise RuntimeError("Channel metadata failed readback")
        return {"id": identifier, "uuid": channel["uuid"], "name": channel["name"]}

    def launch(self, agent):
        if not agent["channel_ids"]:
            raise ValueError("An agent needs at least one channel before it can start")
        start_agent(self.root, self.config, self.secrets, agent, self.docker)
        self.docker.wait_ready(self.name(agent))
        agent["state"] = "running"
        self.save_agent(agent)

    def apply(self, op):
        action = op["action"]
        if action in ("create_channel", "update_channel"):
            return self.channel(op)
        if action in ("add_member", "remove_member"):
            if op["agent"] == "coa":
                raise ValueError("COA's office membership is protected")
            agent = self.resource(op["agent"], "agent")
            if agent["state"] == "archived":
                raise ValueError("Archived agents cannot join channels")
            channel = self.resource(op["channel"], "channel")["uuid"]
            remove = action == "remove_member"
            self.membership(channel, agent["pubkey"], remove=remove)
            ids = set(agent["channel_ids"])
            ids.discard(channel) if remove else ids.add(channel)
            agent["channel_ids"] = sorted(ids)
            self.save_agent(agent)
            if not ids:
                self.docker.stop(self.name(agent))
                agent["state"] = "stopped"
                self.save_agent(agent)
            elif agent["state"] == "running":
                self.launch(agent)
            return {"id": agent["id"], "channels": agent["channel_ids"]}
        if op["id"] == "coa":
            raise ValueError("COA is protected; use host-level maintenance")
        agent = self.registry.get("agent/" + op["id"])
        if action == "create_agent":
            fingerprint = hashlib.sha256(wire(op)).hexdigest()
            if agent and agent.get("creation") != fingerprint:
                raise ValueError(
                    "Agent ID already exists with a different specification"
                )
            if not agent:
                secret = key()
                agent = {
                    **op,
                    "secret": secret,
                    "pubkey": public(secret),
                    "state": "starting",
                    "creation": fingerprint,
                    "auth_tag": attestation(self.secrets["coa"], public(secret)),
                    "channel_ids": sorted(
                        {self.resource(c, "channel")["uuid"] for c in op["channels"]}
                    ),
                }
                self.save_agent(agent)
            if agent["state"] == "archived":
                raise ValueError("Agent is archived; choose a new ID")
            self.register_agent(agent)
            for channel_id in agent["channel_ids"]:
                self.membership(channel_id, agent["pubkey"])
            self.launch(agent)
        else:
            if not agent:
                raise ValueError("Agent does not exist")
            if agent["state"] == "archived" and action != "archive_agent":
                raise ValueError(
                    "Archived agent data is retained; automatic restoration is outside v1"
                )
            if action == "update_agent":
                agent.update(
                    {
                        k: v
                        for k, v in op.items()
                        if k in ("name", "instructions", "model")
                    }
                )
                self.save_agent(agent)
                self.register_agent(agent)
                if agent["state"] == "running":
                    self.launch(agent)
            elif action == "start_agent":
                self.launch(agent)
            elif action == "stop_agent":
                self.docker.stop(self.name(agent))
                agent["state"] = "stopped"
                self.save_agent(agent)
            elif action == "archive_agent":
                self.docker.stop(self.name(agent))
                for channel in self.registry.list("channel"):
                    self.membership(channel["uuid"], agent["pubkey"], remove=True)
                owner = self.actor(self.secrets["coa"], self.config["coa_auth"])
                if agent["state"] != "archived":
                    owner.event(
                        9035,
                        [
                            ["-"],
                            ["p", agent["pubkey"]],
                            ["reason", "retired"],
                            agent["auth_tag"],
                        ],
                    )
                if not self.buzz.archived(agent["pubkey"]):
                    raise RuntimeError("Agent archive failed authoritative readback")
                agent["state"] = "archived"
                self.save_agent(agent)
        return {"id": agent["id"], "pubkey": agent["pubkey"], "state": agent["state"]}

    def source(self, identifier, owner=False):
        event = self.buzz.message(identifier)
        if event["kind"] != 9 or tags(event, "h") != [[self.config["office"]]]:
            raise ValueError("Authorization must be a message in Office Of COA")
        if owner and event["pubkey"] != self.config["owner"]:
            raise ValueError("Only the human owner can authorize changes")
        if abs(time.time() - event["created_at"]) > 86400:
            # Bound requests remain retryable across outages; unseen old messages cannot authorize new work.
            known = self.registry.db.execute(
                "SELECT 1 FROM authorizations WHERE event=?", (identifier,)
            ).fetchone()
            if not known:
                raise ValueError(
                    "Request is older than 24 hours; ask the owner to repeat it"
                )
        members = self.buzz.channel(self.config["office"])["roles"]
        if event["pubkey"] not in members:
            raise ValueError("Requester is no longer a member of Office Of COA")
        return event

    def propose(self, source_event_id, operations):
        operations = validate(operations)
        source = self.source(source_event_id)
        digest = hashlib.sha256(wire([source_event_id, operations])).hexdigest()
        row = self.registry.db.execute(
            "SELECT event FROM proposals WHERE id=?", (digest,)
        ).fetchone()
        if row:
            event = json.loads(row[0])
        else:
            text = (
                "Proposed team changes:\n```json\n"
                + json.dumps(operations, indent=2)
                + "\n```\nReply `approve` to this message to authorize exactly these changes."
            )
            event = sign(
                self.secrets["coa"],
                9,
                [["h", self.config["office"]], *reply_tags(source)],
                text,
            )
            with self.registry.db:
                self.registry.db.execute(
                    "INSERT INTO proposals VALUES(?,?,?,?)",
                    (
                        digest,
                        source_event_id,
                        wire(operations).decode(),
                        wire(event).decode(),
                    ),
                )
        self.actor(self.secrets["coa"], self.config["coa_auth"]).publish(event)
        return {
            "proposal_id": digest,
            "message_id": event["id"],
            "state": "awaiting_owner_approval",
        }

    def execute(self, source_event_id, operations, *, proposal=False):
        event = self.source(source_event_id, owner=True)
        if not proposal and event["content"].strip().lower() == "approve":
            raise ValueError("Approval replies must execute their frozen proposal")
        operations = validate(operations)
        self.registry.bind(source_event_id, operations)
        results = []
        for index, op in enumerate(operations):
            identifier = source_event_id + "/" + str(index)
            prior = self.registry.operation(identifier, op)
            if prior["state"] == "done":
                results.append(json.loads(prior["result"]))
                continue
            try:
                result = self.apply(op)
                self.registry.outcome(identifier, "done", result)
                results.append(result)
            except Exception as exc:  # noqa: BLE001 -- persist partial outcomes even for unexpected provider failures
                # Exception values may contain config/credentials; only controlled messages leave the service.
                message = (
                    str(exc)
                    if type(exc) in (ValueError, RuntimeError)
                    else "Operation failed; inspect service health"
                )
                failure = {"operation": index, "error": message}
                self.registry.outcome(identifier, "failed", failure)
                return {
                    "state": "partial_failure",
                    "completed": results,
                    **failure,
                    "retry_source_event_id": source_event_id,
                }
        return {"state": "complete", "results": results}

    def approve(self, proposal_id=None, approval_event_id=None):
        approval = self.source(approval_event_id, owner=True)
        replies = tags(approval, "e")
        # Resolve from the signed reply itself, so approval also works in a new
        # Hermes thread which has never seen the proposal tool's return value.
        parents = [t[0] for t in replies if len(t) >= 3 and t[2] == "reply"]
        if len(parents) != 1 or approval["content"].strip().lower() != "approve":
            raise ValueError("Owner must reply 'approve' directly to this proposal")
        row = self.registry.db.execute(
            "SELECT * FROM proposals WHERE json_extract(event, '$.id')=?", (parents[0],)
        ).fetchone()
        if not row or (proposal_id is not None and row["id"] != proposal_id):
            raise ValueError("Owner must reply 'approve' directly to this proposal")
        proposal = json.loads(row["event"])
        if approval["created_at"] < proposal["created_at"]:
            raise ValueError("Approval predates proposal")
        return self.execute(approval_event_id, json.loads(row["body"]), proposal=True)

    def inspect(self):
        agents = []
        for agent in self.registry.list("agent"):
            agents.append(
                {
                    k: agent.get(k)
                    for k in ("id", "name", "pubkey", "state", "channel_ids", "model")
                }
            )
            agents[-1]["gateway_ready"] = self.docker.ready(self.name(agent))
        return {"agents": agents, "channels": self.registry.list("channel")}

    def bootstrap(self):
        """Run on manager startup; never starts explicitly stopped or archived workers."""
        with self.lock:
            self.ensure_member(self.buzz.pubkey, "admin")
            self.channel(
                {
                    "action": "create_channel",
                    "id": "office",
                    "name": "Office Of COA",
                    "description": "Team building with Chief of Agents",
                    "visibility": "private",
                }
            )
            agent = self.registry.get("agent/coa") or {
                "id": "coa",
                "name": "Chief of Agents",
                "instructions": "",
                "secret": self.secrets["coa"],
                "pubkey": public(self.secrets["coa"]),
                "auth_tag": self.config["coa_auth"],
                "channel_ids": [self.config["office"]],
                "state": "starting",
            }
            self.save_agent(agent)
            self.register_agent(agent)
            self.membership(self.config["office"], agent["pubkey"])
            self.launch(agent)
            for worker in self.registry.list("agent"):
                if worker["id"] != "coa" and worker["state"] == "running":
                    self.launch(worker)
