import json
import time

import pytest
from conftest import message

from team_builder.nostr import key, public, tags


def test_direct_creation_discovery_and_duplicate_request(manager, create_ops):
    source = message(manager)
    first = manager.execute(source, create_ops)
    assert first["state"] == "complete"
    agent = manager.resource("engineer", "agent")
    assert agent["auth_tag"][1] == public(manager.secrets["coa"])
    registration = manager.buzz.head(
        30177, public(manager.secrets["coa"]), agent["pubkey"]
    )
    assert json.loads(registration["content"])["parallelism"] == 1
    dev = manager.resource("dev", "channel")["uuid"]
    assert manager.buzz.channel(dev)["roles"][agent["pubkey"]] == "bot"
    assert (
        agent["pubkey"] not in manager.buzz.channel(manager.config["office"])["roles"]
    )
    assert manager.execute(source, create_ops) == first
    assert len(manager.registry.list("agent")) == 2


def test_agents_can_propose_but_cannot_execute(manager, create_ops):
    agent_key = key()
    manager.buzz.channels[manager.config["office"]]["roles"][public(agent_key)] = "bot"
    source = message(manager, secret=agent_key)
    with pytest.raises(ValueError, match="human owner"):
        manager.execute(source, create_ops)
    proposal = manager.propose(source, create_ops)
    assert len(manager.registry.list("agent")) == 1
    approval = message(manager, "approve", reply=proposal["message_id"])
    with pytest.raises(ValueError, match="frozen proposal"):
        manager.execute(approval, create_ops)
    assert manager.approve(proposal["proposal_id"], approval)["state"] == "complete"


def test_wrong_thread_denial_and_changed_proposal(manager, create_ops):
    source = message(manager, "Build a software team")
    first = manager.propose(source, create_ops)
    changed = manager.propose(
        source, [{"action": "create_channel", "id": "other", "name": "Other"}]
    )
    assert first["proposal_id"] != changed["proposal_id"]
    approval = message(manager, "approve", reply=first["message_id"])
    with pytest.raises(ValueError, match="directly"):
        manager.approve(changed["proposal_id"], approval)
    denial = message(manager, "Do not approve", reply=first["message_id"])
    with pytest.raises(ValueError, match="directly"):
        manager.approve(first["proposal_id"], denial)
    assert manager.approve(first["proposal_id"], approval)["state"] == "complete"


def test_approval_in_fresh_thread_needs_only_signed_reply(manager, create_ops):
    proposal = manager.propose(message(manager, "Build a software team"), create_ops)
    approval = message(manager, "approve", reply=proposal["message_id"])
    assert manager.approve(approval_event_id=approval)["state"] == "complete"


def test_root_only_marker_is_not_an_approval_reply(manager, create_ops):
    proposal = manager.propose(message(manager), create_ops)
    from team_builder.nostr import sign

    event = sign(
        manager.owner_secret,
        9,
        [["h", manager.config["office"]], ["e", proposal["message_id"], "", "root"]],
        "approve",
    )
    manager.buzz.events[event["id"]] = event
    with pytest.raises(ValueError, match="directly"):
        manager.approve(approval_event_id=event["id"])


def test_same_owner_message_cannot_authorize_new_changes(manager, create_ops):
    source = message(manager)
    manager.execute(source, create_ops)
    with pytest.raises(ValueError, match="different operations"):
        manager.execute(source, [{"action": "stop_agent", "id": "engineer"}])


def test_partial_failure_retry_preserves_identity_and_completed_steps(
    manager, create_ops
):
    source = message(manager)
    original = manager.launch
    manager.launch = lambda agent: (_ for _ in ()).throw(RuntimeError("Gateway failed"))
    first = manager.execute(source, create_ops)
    assert first["state"] == "partial_failure" and len(first["completed"]) == 1
    identity = manager.resource("engineer", "agent")["pubkey"]
    manager.launch = original
    assert manager.execute(source, create_ops)["state"] == "complete"
    assert manager.resource("engineer", "agent")["pubkey"] == identity


def test_restart_retains_authorization_and_stopped_agents(manager, create_ops):
    source = message(manager)
    manager.execute(source, create_ops)
    manager.execute(
        message(manager, "Stop engineer"), [{"action": "stop_agent", "id": "engineer"}]
    )
    manager.bootstrap()
    assert manager.resource("engineer", "agent")["state"] == "stopped"
    from team_builder.storage import Registry

    reopened = Registry(manager.root / "registry.sqlite3")
    with pytest.raises(ValueError):
        reopened.bind(source, [])
    reopened.db.close()


def test_lifecycle_invitation_update_and_archive_retains_data(manager, create_ops):
    manager.execute(message(manager), create_ops)
    workspace = manager.root / "agents/engineer/work"
    workspace.mkdir(parents=True)
    (workspace / "work.txt").write_text("preserve me")
    operations = [
        {"action": "add_member", "agent": "engineer", "channel": "office"},
        {
            "action": "update_agent",
            "id": "engineer",
            "name": "Engineer II",
            "model": "another-model",
        },
        {"action": "stop_agent", "id": "engineer"},
        {"action": "start_agent", "id": "engineer"},
        {"action": "remove_member", "agent": "engineer", "channel": "office"},
        {"action": "archive_agent", "id": "engineer"},
    ]
    for i, op in enumerate(operations):
        assert manager.execute(message(manager, str(i)), [op])["state"] == "complete"
    agent = manager.resource("engineer", "agent")
    assert agent["state"] == "archived" and agent["name"] == "Engineer II"
    assert (workspace / "work.txt").read_text() == "preserve me"
    assert all(
        agent["pubkey"] not in ch["roles"] for ch in manager.buzz.channels.values()
    )
    archived = [e for e in manager.buzz.events.values() if e["kind"] == 9035]
    assert ["-"] in archived[0]["tags"] and tags(archived[0], "p") == [
        [agent["pubkey"]]
    ]


@pytest.mark.parametrize(
    "operation",
    [
        {"action": "archive_agent", "id": "coa"},
        {"action": "stop_agent", "id": "coa"},
        {"action": "update_channel", "id": "office", "name": "Gone"},
        {"action": "remove_member", "agent": "coa", "channel": "office"},
    ],
)
def test_owner_can_manage_coa_and_office(manager, operation):
    result = manager.execute(message(manager), [operation])
    assert result["state"] == "complete"
    manager.bootstrap()
    if operation["action"] in ("archive_agent", "stop_agent", "remove_member"):
        assert not manager.docker.ready(manager.name(manager.resource("coa", "agent")))


def test_reject_old_or_wrong_channel_request(manager, create_ops):
    with pytest.raises(ValueError, match="24 hours"):
        manager.execute(
            message(manager, timestamp=int(time.time()) - 90000), create_ops
        )
    with pytest.raises(ValueError, match="Office"):
        manager.execute(message(manager, channel="other"), create_ops)


def test_no_secrets_in_inspect(manager, create_ops):
    manager.execute(message(manager), create_ops)
    result = json.dumps(manager.inspect())
    for secret in manager.secrets.values():
        assert secret not in result
    assert manager.resource("engineer", "agent")["secret"] not in result


def real_launch(manager, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    monkeypatch.setattr(manager, "launch", manager.__class__.launch.__get__(manager))


def test_codex_agent_uses_harness_image_and_bundle(manager, monkeypatch):
    real_launch(manager, monkeypatch)
    manager.config["images"] = {"codex": "codex-image"}
    ops = [
        {"action": "create_channel", "id": "dev", "name": "Development"},
        {
            "action": "create_agent",
            "id": "coder",
            "name": "Coder",
            "instructions": "Write code",
            "channels": ["dev"],
            "harness": "codex",
        },
    ]
    assert manager.execute(message(manager, "harness-1"), ops)["state"] == "complete"
    name = manager.name(manager.resource("coder", "agent"))
    spec = manager.docker.specs[name]
    assert spec["Image"] == "codex-image"
    assert spec["Entrypoint"] == ["/opt/team-builder/.venv/bin/team-builder-worker"]
    assert "TEAM_BUILDER_HARNESS=codex" in spec["Env"]
    assert spec["HostConfig"]["Binds"][1].endswith(":/home/agent")
    managed = manager.root / "agents/coder/managed"
    assert json.loads((managed / "harness.json").read_text())["harness"] == "codex"
    manifest = json.loads((managed / "manifest.json").read_text())
    assert "harness.json" in manifest
    env = json.loads((managed / "env.json").read_text())
    assert env["OPENROUTER_API_KEY"] == "model-secret"
    assert "WINDSURF_API_KEY" not in env
    assert json.loads((managed / "config.yaml").read_text()) == {}


def test_devin_agent_uses_stored_credential(manager, monkeypatch):
    from team_builder.credentials import store_credential

    real_launch(manager, monkeypatch)
    manager.config["images"] = {"devin": "devin-image"}
    store_credential(manager.root, "devin-acct", "devin-key-123")
    ops = [
        {"action": "create_channel", "id": "dev", "name": "Development"},
        {
            "action": "create_agent",
            "id": "dev",
            "name": "Dev",
            "instructions": "Work",
            "channels": ["dev"],
            "harness": "devin",
            "harness_credential": "devin-acct",
        },
    ]
    with pytest.raises(ValueError, match="COA must run on Hermes"):
        manager.apply(ops[1] | {"id": "coa"})
    missing = manager.execute(
        message(manager, "harness-2"),
        [ops[0], {**ops[1], "id": "nodev", "harness_credential": "absent"}],
    )
    assert missing["state"] == "partial_failure"
    assert "Unknown named credential" in missing["error"]
    assert manager.execute(message(manager, "harness-3"), ops)["state"] == "complete"
    env = json.loads((manager.root / "agents/dev/managed/env.json").read_text())
    assert env["WINDSURF_API_KEY"] == "devin-key-123"
    assert "OPENROUTER_API_KEY" not in env and "OPENAI_API_KEY" not in env
    shown = manager.inspect()
    entry = next(a for a in shown["agents"] if a["id"] == "dev")
    assert entry["harness"] == "devin"
    assert "devin-key-123" not in json.dumps(shown)


def test_coa_and_pi_restrictions(manager, monkeypatch):
    real_launch(manager, monkeypatch)
    manager.config["images"] = {"pi": "pi-image"}
    channel = {"action": "create_channel", "id": "dev", "name": "Development"}
    manager.execute(message(manager, "harness-4"), [channel])
    with pytest.raises(ValueError, match="COA must run on Hermes"):
        manager.apply(
            {
                "action": "create_agent",
                "id": "coa",
                "name": "COA",
                "instructions": "x",
                "channels": ["dev"],
                "harness": "pi",
            }
        )
    assert (
        manager.execute(
            message(manager, "harness-5"),
            [
                {
                    "action": "create_agent",
                    "id": "helper",
                    "name": "Helper",
                    "instructions": "x",
                    "channels": ["dev"],
                    "harness": "pi",
                }
            ],
        )["state"]
        == "complete"
    )
    from team_builder.agent_config import add_catalog, configure, inspect_config

    add_catalog(
        manager,
        {
            "id": "search",
            "kind": "mcp",
            "name": "Search",
            "url": "https://example.com/mcp",
        },
    )
    value = inspect_config(manager, "helper")["settings"]
    with pytest.raises(ValueError, match="pi agents do not support MCP"):
        configure(manager, "helper", 0, {**value, "mcp": ["search"]})
    info = inspect_config(manager, "helper")
    assert info["harness"] == "pi" and info["available_tools"] == []
    assert "{harness}" not in info["prompt_note"]


def test_missing_harness_image_is_actionable(manager, monkeypatch):
    real_launch(manager, monkeypatch)
    manager.execute(
        message(manager, "harness-6"),
        [{"action": "create_channel", "id": "dev", "name": "Development"}],
    )
    result = manager.execute(
        message(manager, "harness-7"),
        [
            {
                "action": "create_agent",
                "id": "helper",
                "name": "Helper",
                "instructions": "x",
                "channels": ["dev"],
                "harness": "pi",
            }
        ],
    )
    assert result["state"] == "partial_failure"
    assert "upgrade --harness pi" in result["error"]


def test_hermes_create_fingerprint_ignores_default_harness(manager, create_ops):
    source = message(manager, "harness-8")
    first = manager.execute(source, create_ops)
    assert first["state"] == "complete"
    op = {**create_ops[1], "harness": "hermes"}
    again = manager.apply(op)
    assert again["id"] == "engineer"
