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
def test_protected_resources(manager, operation):
    result = manager.execute(message(manager), [operation])
    assert result["state"] == "partial_failure" and "protected" in result["error"]


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
