import hashlib
import json
import time
import uuid
from pathlib import Path
from threading import RLock

from .buzz import Buzz, policy
from .community import configure_provider, invite, project
from .docker import Docker
from .models import validate
from .nostr import attestation, key, public, reply_tags, sign, tags, wire
from .repositories import link_github_repository
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
        self.load_provider()
        from .deployments import Deployments

        self.deployments = Deployments(self)

    def load_provider(self):
        path = self.root / "provider.json"
        if path.exists():
            override = json.loads(path.read_text())
            self.config.update(
                {k: override[k] for k in ("provider", "model", "base_url")}
            )
            self.secrets["provider_key"] = override["provider_key"]

    def store_provider_credential(self, source_event_id, id, api_key):
        from .credentials import store_credential

        self.source(source_event_id, owner=True, cache=False)
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 16384:
            raise ValueError("Invalid credential")
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        self.registry.bind(
            source_event_id,
            [{"action": "store_provider_credential", "id": id, "sha256": digest}],
        )
        store_credential(self.root, id, api_key)
        return {"credential": id, "stored": True}

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
        existing = self.registry.get("channel/" + identifier)
        if existing and existing.get("state") == "deleted":
            raise ValueError("Channel was deleted; choose a new ID")
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
        channel["state"] = "provisioning"
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
        elif (
            observed["private"] != (channel["visibility"] == "private")
            or observed["name"] != [[channel["name"]]]
            or observed["about"]
            != ([[channel["description"]]] if channel["description"] else [])
        ):
            head = self.buzz.head(39000, self.config["relay"], channel["uuid"])
            self.buzz.event(
                9002,
                [
                    ["h", channel["uuid"]],
                    ["name", channel["name"]],
                    ["about", channel["description"]],
                    [
                        "visibility",
                        "private" if channel["visibility"] == "private" else "open",
                    ],
                ],
                head=head,
            )
        if operation["action"] == "create_channel":
            self.membership(channel["uuid"], self.config["owner"], "owner")
        actual = self.buzz.channel(channel["uuid"])
        if actual["name"] != [[channel["name"]]] or actual["private"] != (
            channel["visibility"] == "private"
        ):
            raise RuntimeError("Channel metadata failed readback")
        channel["state"] = "active"
        self.registry.put("channel/" + identifier, "channel", channel)
        return {"id": identifier, "uuid": channel["uuid"], "name": channel["name"]}

    def launch(self, agent):
        if not agent["channel_ids"]:
            raise ValueError("An agent needs at least one channel before it can start")
        start_agent(self.root, self.config, self.secrets, agent, self.docker)
        self.docker.wait_ready(self.name(agent))
        agent["state"] = "running"
        self.save_agent(agent)

    def dashboard_restart(self, identifier, request_id):
        """Called only by the dashboard's authenticated owner session boundary."""
        if not self.lock.acquire(blocking=False):
            return 409, {"error": "Another management operation is active; retry later"}
        try:
            agent = self.registry.get("agent/" + identifier)
            if not agent:
                return 404, {"error": "Unknown managed agent"}
            if agent["state"] != "running":
                return 409, {
                    "error": "Only agents with running desired state can be restarted"
                }
            operation_id = "dashboard-restart:" + request_id
            operation = {
                "action": "restart_agent",
                "id": identifier,
                "actor": self.config["owner"],
                "source": "dashboard",
            }
            try:
                previous = self.registry.operation(operation_id, operation)
            except ValueError:
                return 409, {"error": "Request ID already belongs to another operation"}
            if previous["state"] == "done":
                return 200, json.loads(previous["result"])
            if "result" in previous:
                return 409, {
                    "error": "Restart outcome is failed or uncertain; inspect health before submitting a new request"
                }
            try:
                self.docker.restart(self.name(agent))
            except Exception:  # noqa: BLE001 -- never expose Docker diagnostics to dashboard
                result = {
                    "error": "Gateway restart could not be confirmed. Check agent health and ensure its runtime supports gateway-only restart. The container was not restarted."
                }
                self.registry.outcome(operation_id, "failed", result)
                return 503, result
            result = {
                "id": identifier,
                "status": "restarted",
                "message": "Gateway restarted; the container and detached app processes were kept running. Buzz reconnection is still being observed.",
            }
            self.registry.outcome(operation_id, "done", result)
            return 200, result
        finally:
            self.lock.release()

    def apply(self, op):
        action = op["action"]
        if action == "execute_deployment":
            return self.deployments.enqueue(op["plan_id"])
        if action == "configure_deployment_access":
            return self.deployments.grant(
                **{k: v for k, v in op.items() if k != "action"}
            )
        if action in ("configure_agent", "apply_agent_config"):
            from .agent_config import apply_config, configure

            return (
                configure(self, op["id"], op["expected_revision"], op["settings"])
                if action == "configure_agent"
                else apply_config(self, op["id"])
            )
        if action == "configure_github_access":
            from .github_access import configure

            return configure(self, op)
        if action == "link_github_repository":
            return link_github_repository(self, op)
        if action in ("create_project", "update_project", "delete_project"):
            return project(self, op)
        if action == "create_invite":
            return invite(self, op)
        if action == "configure_provider":
            return configure_provider(self, op)
        if action in ("add_human_member", "remove_human_member"):
            channel = self.resource(op["channel"], "channel")
            if channel.get("state") == "deleted":
                raise ValueError("Channel is deleted")
            if any(a["pubkey"] == op["pubkey"] for a in self.registry.list("agent")):
                raise ValueError("Use agent membership operations for managed agents")
            remove = action == "remove_human_member"
            if not remove and op["pubkey"] not in self.buzz.roster():
                self.ensure_member(op["pubkey"])
            self.membership(channel["uuid"], op["pubkey"], op["role"], remove)
            return {"channel": op["channel"], "pubkey": op["pubkey"], "removed": remove}
        if action == "delete_channel":
            channel = self.resource(op["id"], "channel")
            if self.buzz.channel(channel["uuid"]):
                self.buzz.event(9008, [["h", channel["uuid"]]])
            if self.buzz.channel(channel["uuid"]):
                raise RuntimeError("Channel deletion failed authoritative readback")
            channel["state"] = "deleted"
            self.registry.put("channel/" + op["id"], "channel", channel)
            for agent in self.registry.list("agent"):
                if channel["uuid"] in agent["channel_ids"]:
                    agent["channel_ids"].remove(channel["uuid"])
                    if agent["state"] == "running":
                        if agent["channel_ids"]:
                            self.launch(agent)
                        else:
                            self.docker.stop(self.name(agent))
                            agent["state"] = "stopped"
                            self.save_agent(agent)
                    self.save_agent(agent)
            return {"id": op["id"], "state": "deleted"}
        if action in ("create_channel", "update_channel"):
            return self.channel(op)
        if action in ("add_member", "remove_member"):
            agent = self.resource(op["agent"], "agent")
            if agent["state"] == "archived":
                raise ValueError("Archived agents cannot join channels")
            channel_item = self.resource(op["channel"], "channel")
            if channel_item.get("state") == "deleted":
                raise ValueError("Channel is deleted")
            channel = channel_item["uuid"]
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
        agent = self.registry.get("agent/" + op["id"])
        if action == "create_agent":
            if op.get("github_credential"):
                from .github_access import token_for

                token_for(self.root, op["github_credential"])
            fingerprint = hashlib.sha256(wire(op)).hexdigest()
            if agent and agent.get("creation") != fingerprint:
                raise ValueError(
                    "Agent ID already exists with a different specification"
                )
            if not agent:
                if self.resource("coa", "agent")["state"] == "archived":
                    raise ValueError("Cannot provision agents owned by an archived COA")
                if any(
                    self.resource(c, "channel").get("state") == "deleted"
                    for c in op["channels"]
                ):
                    raise ValueError("Cannot use a deleted channel")
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
                from .agent_config import configure, settings

                value = settings(self, agent)
                value.update(
                    {
                        k: v
                        for k, v in op.items()
                        if k in ("name", "instructions", "model")
                    }
                )
                return configure(
                    self, agent["id"], agent.get("config_revision", 0), value
                )
            elif action == "start_agent":
                self.launch(agent)
            elif action == "stop_agent":
                agent["state"] = "stopped"
                self.save_agent(agent)
                self.docker.stop(self.name(agent))
            elif action == "archive_agent":
                if agent["state"] != "archived":
                    agent["state"] = "archiving"
                    self.save_agent(agent)
                self.docker.stop(self.name(agent))
                for channel in self.registry.list("channel"):
                    if channel.get("state") != "deleted":
                        self.membership(channel["uuid"], agent["pubkey"], remove=True)
                owner = (
                    self.buzz
                    if agent["id"] == "coa"
                    else self.actor(self.secrets["coa"], self.config["coa_auth"])
                )
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
                from .runtime import write_github_token

                write_github_token(self.root, agent)
        return {"id": agent["id"], "pubkey": agent["pubkey"], "state": agent["state"]}

    def source(self, identifier, owner=False, cache=True):
        known = self.registry.db.execute(
            "SELECT 1 FROM authorizations WHERE event=?", (identifier,)
        ).fetchone()
        cached = self.registry.get("source/" + identifier) if known else None
        event = cached or self.buzz.message(identifier)
        channel_tags = tags(event, "h")
        channels = {c["uuid"]: c for c in self.registry.list("channel")}
        if (
            event["kind"] != 9
            or len(channel_tags) != 1
            or len(channel_tags[0]) != 1
            or channel_tags[0][0] not in channels
        ):
            raise ValueError(
                "Authorization must be a message in Office Of COA or another managed channel"
            )
        if owner and event["pubkey"] != self.config["owner"]:
            raise ValueError("Only the human owner can authorize changes")
        if not known:
            if abs(time.time() - event["created_at"]) > 86400:
                raise ValueError(
                    "Request is older than 24 hours; ask the owner to repeat it"
                )
            channel = self.buzz.channel(channel_tags[0][0])
            if not channel or event["pubkey"] not in channel["roles"]:
                raise ValueError(
                    "Requester is no longer a member of the source channel"
                )
        if cache:
            self.registry.put("source/" + identifier, "source", event)
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
            for op in operations:
                if op["action"] == "execute_deployment":
                    plan = self.deployments.get("plan/" + op["plan_id"])
                    text += (
                        "\n\nFrozen deployment plan (no database migrations):\n```json\n"
                        + json.dumps(plan, indent=2)
                        + "\n```"
                    )
            event = sign(
                self.secrets["coa"],
                9,
                [["h", tags(source, "h")[0][0]], *reply_tags(source)],
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
                result = (
                    self.deployments.enqueue(
                        op["plan_id"], source=source_event_id, actor=event["pubkey"]
                    )
                    if op["action"] == "execute_deployment"
                    else self.apply(op)
                )
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
        if tags(approval, "h") != tags(proposal, "h"):
            raise ValueError("Approval must be in the proposal channel")
        if approval["created_at"] < proposal["created_at"]:
            raise ValueError("Approval predates proposal")
        return self.execute(approval_event_id, json.loads(row["body"]), proposal=True)

    def inspect(self):
        agents = []
        for agent in self.registry.list("agent"):
            agents.append(
                {
                    k: agent.get(k)
                    for k in (
                        "id",
                        "name",
                        "pubkey",
                        "state",
                        "channel_ids",
                        "model",
                        "github_credential",
                    )
                }
            )
            agents[-1]["config_revision"] = agent.get("config_revision", 0)
            agents[-1]["gateway_ready"] = self.docker.ready(self.name(agent))
        return {
            "agents": agents,
            "channels": self.registry.list("channel"),
            "projects": self.inspect_projects(),
            "provider": {
                k: self.config.get(k) for k in ("provider", "model", "base_url")
            },
            "credentials": sorted(
                p.stem for p in (self.root / "credentials").glob("*.json")
            ),
            "github_credentials": sorted(
                p.stem for p in (self.root / "github" / "credentials").glob("*.json")
            ),
        }

    def inspect_projects(self):
        return {
            "projects": self.registry.list("project"),
            "repositories": self.registry.list("repository"),
        }

    def bootstrap(self):
        """Run on manager startup; never starts explicitly stopped or archived workers."""
        with self.lock:
            self.ensure_member(self.buzz.pubkey, "admin")
            office = self.registry.get("channel/office")
            if office and office.get("state") == "provisioning":
                self.channel(
                    {k: v for k, v in office.items() if k not in ("uuid", "state")}
                )
            if not office:
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
            if agent["state"] in ("starting", "running"):
                self.register_agent(agent)
                for channel_id in agent["channel_ids"]:
                    self.membership(channel_id, agent["pubkey"])
                self.launch(agent)
            for worker in self.registry.list("agent"):
                if worker["id"] != "coa" and worker["state"] == "running":
                    self.launch(worker)
