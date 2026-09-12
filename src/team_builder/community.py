"""Community and NIP-MP project operations, executed after owner authorization."""

import hashlib
import json
from urllib.parse import urlsplit

from .nostr import wire
from .storage import private_write


def project(manager, op):
    identifier = op["id"]
    existing = manager.registry.get("project/" + identifier)
    action = op["action"]
    if action != "create_project" and not existing:
        raise ValueError("Unknown project")
    fingerprint = hashlib.sha256(wire(op)).hexdigest()
    if action == "create_project" and existing and existing["creation"] != fingerprint:
        raise ValueError("Project ID already exists with different configuration")
    if existing and existing.get("state") == "deleted" and action != "delete_project":
        raise ValueError("Project was deleted; choose a new ID")
    # Manager-owned records remain manageable if COA is stopped or archived.
    actor = manager.buzz
    coordinate = f"30621:{actor.pubkey}:{identifier}"
    head = actor.head(30621, actor.pubkey, identifier)
    if action == "delete_project":
        if head:
            actor.event(5, [["a", coordinate], ["k", "30621"]], head=head)
        if actor.head(30621, actor.pubkey, identifier):
            raise RuntimeError("Project deletion failed authoritative readback")
        item = {**existing, "state": "deleted"}
    else:
        item = {
            **(existing or {}),
            **op,
            "coordinate": coordinate,
            "creation": existing["creation"] if existing else fingerprint,
            "state": "active",
        }
        channel = manager.resource(item["channel"], "channel")
        if channel.get("state") == "deleted":
            raise ValueError("Cannot attach a project to a deleted channel")
        repositories = sorted(set(item["repositories"]))
        for repo in repositories:
            _, author, slug = repo.split(":", 2)
            if not actor.head(30617, author, slug):
                raise ValueError(
                    "Repository announcement was not found in this community"
                )
        if len(item["description"].encode()) > 2048:
            raise ValueError("Project description exceeds 2048 bytes")
        # Preserve relay hints and unknown tags on existing project records.
        managed = {
            "d",
            "name",
            "description",
            "buzz-channel",
            "buzz-visibility",
            "a",
            "auth",
        }
        tag_list = [t for t in (head or {}).get("tags", []) if t[0] not in managed]
        old_repos = {t[1]: t for t in (head or {}).get("tags", []) if t[0] == "a"}
        tag_list += [
            ["d", identifier],
            ["name", item["name"]],
            ["description", item["description"]],
            ["buzz-channel", channel["uuid"]],
            ["buzz-visibility", item["visibility"]],
        ]
        tag_list += [old_repos.get(r, ["a", r]) for r in repositories]
        actor.replace(30621, tag_list, "")
        item["repositories"] = repositories
    manager.registry.put("project/" + identifier, "project", item)
    return item


def invite(manager, op):
    existing = manager.registry.get("invite/" + op["id"])
    if existing:
        if existing["spec"] != op:
            raise ValueError("Invite ID already exists with different settings")
        if "result" not in existing:
            raise RuntimeError(
                "Invite creation outcome is unknown; inspect Buzz before authorizing a new invite ID"
            )
        return existing["result"]
    # The upstream mint API has no idempotency key. Never blindly mint again
    # after an interruption which may have created an invitation.
    manager.registry.put("invite/" + op["id"], "invite", {"spec": op})
    result = manager.buzz.call(
        "POST",
        "/api/invites",
        {
            "ttl_secs": op["ttl_secs"],
            "max_uses": op["max_uses"],
        },
    )
    if not result.get("code") or not result.get("url") or not result.get("expires_at"):
        raise RuntimeError("Buzz returned an incomplete invitation")
    manager.registry.put("invite/" + op["id"], "invite", {"spec": op, "result": result})
    return result


def configure_provider(manager, op):
    base_url = op.get("base_url")
    if op["provider"] == "custom":
        url = urlsplit(base_url or "")
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Custom provider requires an HTTP(S) base URL without credentials"
            )
    elif base_url:
        raise ValueError("base_url is only supported for custom providers")
    path = manager.root / "credentials" / (op["credential"] + ".json")
    if not path.is_file() or path.is_symlink():
        raise ValueError("Credential not found; store the named credential first")
    credential = json.loads(path.read_text())["api_key"]
    # A single atomic override prevents config/key mismatches after interruption.
    override = {k: op[k] for k in ("provider", "model", "credential")}
    override.update(base_url=base_url, provider_key=credential)
    private_write(manager.root / "provider.json", override)
    manager.load_provider()
    for agent in manager.registry.list("agent"):
        if agent["state"] == "running":
            manager.launch(agent)
    return {k: override[k] for k in ("provider", "model", "credential", "base_url")}
