"""Versioned owner configuration shared by Buzz operations and Mission Control."""

import json
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

TOOLS = ["terminal", "file", "skills", "memory", "todo", "session_search"]
Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1, max_length=120)]
    instructions: Annotated[str, Field(max_length=24000)] = ""
    soul: Annotated[str, Field(max_length=24000)] = ""
    model: Annotated[str, Field(min_length=1, max_length=200)]
    tools: list[
        Literal["terminal", "file", "skills", "memory", "todo", "session_search"]
    ] = TOOLS
    skills: Annotated[list[Slug], Field(max_length=40)] = []
    mcp: Annotated[list[Slug], Field(max_length=20)] = []


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Slug
    kind: Literal["skill", "mcp"]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=2000)] = ""
    content: Annotated[str, Field(max_length=48000)] = ""
    url: Annotated[str, Field(max_length=2000)] = ""
    credential: Slug | None = None
    tools: Annotated[
        list[Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,120}$")]],
        Field(max_length=100),
    ] = []


def settings(manager, agent):
    return Settings.model_validate(
        {
            "name": agent["name"],
            "instructions": agent.get("instructions", ""),
            "soul": agent.get("soul", ""),
            "model": agent.get("model") or manager.config["model"],
            "tools": agent.get("tools", TOOLS),
            "skills": agent.get("skills", []),
            "mcp": agent.get("mcp", []),
        }
    ).model_dump()


def catalog(manager):
    return manager.registry.list("catalog")


def add_catalog(manager, value):
    entry = CatalogEntry.model_validate(value).model_dump()
    if entry["kind"] == "skill":
        if (
            not entry["content"].strip()
            or entry["url"]
            or entry["credential"]
            or entry["tools"]
        ):
            raise ValueError(
                "Skills require Markdown content, without connection settings"
            )
    else:
        url = urlsplit(entry["url"])
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) endpoint without embedded credentials, query, or fragment"
            )
        if entry["content"]:
            raise ValueError("MCP entries cannot contain skill content")
        if (
            entry["credential"]
            and not (
                manager.root / "credentials" / (entry["credential"] + ".json")
            ).is_file()
        ):
            raise ValueError(
                "Unknown named credential; provision it through the CLI first"
            )
    key = "catalog/" + entry["id"]
    old = manager.registry.get(key)
    if old and old != entry:
        raise ValueError("Catalog IDs are immutable; use a new ID for a new version")
    manager.registry.put(key, "catalog", entry)
    return entry


def inspect_config(manager, identifier):
    agent = manager.resource(identifier, "agent")
    applied = None
    from .dashboard_observe import read_local

    try:
        home = manager.root / "agents" / identifier / "home" / ".hermes"
        marker = json.loads(read_local(home / "team-builder-applied.json", home, 4096))
        gateway = json.loads(read_local(home / "gateway_state.json", home, 65536))
        if (
            marker["pid"] == gateway["pid"]
            and marker["start_time"] == gateway["start_time"]
            and type(marker["revision"]) is int
            and marker["revision"] >= 0
        ):
            applied = marker["revision"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    history = [
        h for h in manager.registry.list("agent_config") if h["agent"] == identifier
    ]
    from .runtime import render

    _, _, prompt = render(manager.config, manager.secrets, agent)
    return {
        "id": identifier,
        "revision": agent.get("config_revision", 0),
        "applied_revision": applied,
        "state": agent["state"],
        "settings": settings(manager, agent),
        "history": sorted(history, key=lambda h: h["revision"], reverse=True)[:50],
        "prompt_preview": prompt,
        "prompt_note": "Managed SOUL.md layer; Hermes also adds runtime, memory, and project context.",
        "catalog": catalog(manager),
        "available_tools": TOOLS,
        "impact": "Gateway restart for running agents; detached app servers keep running. Stopped agents apply on next start.",
    }


def configure(manager, identifier, expected_revision, value, source="coa"):
    agent = manager.resource(identifier, "agent")
    if agent["state"] in ("archived", "archiving"):
        raise ValueError("Archived agents cannot be configured")
    revision = agent.get("config_revision", 0)
    if revision != expected_revision:
        raise ValueError("Configuration changed; reload and review the latest version")
    value = Settings.model_validate(value).model_dump()
    for field, kind in [("skills", "skill"), ("mcp", "mcp")]:
        if len(set(value[field])) != len(value[field]):
            raise ValueError("Duplicate assignments are not allowed")
        for entry_id in value[field]:
            entry = manager.registry.get("catalog/" + entry_id)
            if not entry or entry["kind"] != kind:
                raise ValueError("Unknown catalog assignment")
    before = settings(manager, agent)
    if before == value:
        return {"revision": revision, "status": "unchanged"}
    if not revision:
        manager.registry.put(
            f"agent-config/{identifier}/0",
            "agent_config",
            {
                "agent": identifier,
                "revision": 0,
                "settings": before,
                "created_at": None,
                "actor": None,
                "source": "baseline",
            },
        )
    agent.update(value)
    agent["config_revision"] = revision + 1
    with manager.registry.db:
        # Store desired state and its immutable audit snapshot in one transaction.
        from .nostr import wire

        history = {
            "agent": identifier,
            "revision": revision + 1,
            "settings": value,
            "created_at": time.time(),
            "actor": manager.config["owner"],
            "source": source,
        }
        manager.registry.db.execute(
            "INSERT INTO resources VALUES(?,?,?)",
            (
                f"agent-config/{identifier}/{revision + 1}",
                "agent_config",
                wire(history).decode(),
            ),
        )
        manager.registry.db.execute(
            "UPDATE resources SET body=? WHERE id=?",
            (wire(agent).decode(), "agent/" + identifier),
        )
    return apply_config(manager, identifier)


def apply_config(manager, identifier):
    from .runtime import write_agent_files

    agent = manager.resource(identifier, "agent")
    if agent["state"] in ("archived", "archiving"):
        raise ValueError("Archived agents cannot be configured")
    stage = "Buzz registration"
    try:
        manager.register_agent(agent)
        stage = "managed configuration write"
        write_agent_files(manager.root, manager.config, manager.secrets, agent)
        if agent["state"] == "running":
            stage = "gateway restart"
            manager.docker.restart(manager.name(agent))
        return {
            "revision": agent.get("config_revision", 0),
            "status": "starting" if agent["state"] == "running" else "saved",
            "message": "Configuration saved. Check applied revision and gateway health for activation.",
        }
    except Exception:  # noqa: BLE001 -- configuration contains credentials; expose only safe diagnostics
        return {
            "revision": agent.get("config_revision", 0),
            "status": "pending",
            "message": f"Saved, but {stage} is incomplete. Check service health and retry Apply; no container was recreated.",
        }
