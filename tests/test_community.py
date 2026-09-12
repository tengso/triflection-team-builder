import json

import pytest
from conftest import message

from team_builder.credentials import store_credential
from team_builder.manager import Manager
from team_builder.nostr import key, public, sign, tags


def execute(manager, operations):
    result = manager.execute(message(manager, json.dumps(operations)), operations)
    assert result["state"] == "complete", result
    return result


def test_channel_visibility_and_delete_are_durable(manager, create_ops):
    execute(manager, create_ops)
    execute(
        manager, [{"action": "update_channel", "id": "dev", "visibility": "public"}]
    )
    channel = manager.resource("dev", "channel")
    assert not manager.buzz.channel(channel["uuid"])["private"]
    source = message(manager, "Delete this office")
    ops = [{"action": "delete_channel", "id": "office"}]
    result = manager.execute(source, ops)
    assert result["state"] == "complete"
    assert manager.execute(source, ops) == result
    manager.bootstrap()
    assert manager.buzz.channel(manager.config["office"]) is None
    assert manager.resource("coa", "agent")["state"] == "stopped"
    assert manager.resource("engineer", "agent")["state"] == "running"


def test_project_records_repository_membership_and_delete(manager, create_ops):
    execute(manager, create_ops)
    repository = sign(manager.buzz.secret, 30617, [["d", "repo"]], "")
    manager.buzz.publish(repository)
    coordinate = f"30617:{manager.buzz.pubkey}:repo"
    execute(
        manager,
        [
            {
                "action": "create_project",
                "id": "app",
                "name": "App",
                "channel": "dev",
                "repositories": [coordinate],
            }
        ],
    )
    event = manager.buzz.head(30621, manager.buzz.pubkey, "app")
    assert tags(event, "a") == [[coordinate]]
    assert tags(event, "buzz-channel") == [[manager.resource("dev", "channel")["uuid"]]]
    execute(
        manager,
        [
            {
                "action": "update_project",
                "id": "app",
                "name": "App II",
                "repositories": [],
                "visibility": "unlisted",
            }
        ],
    )
    assert manager.resource("app", "project")["repositories"] == []
    execute(manager, [{"action": "delete_project", "id": "app"}])
    assert manager.buzz.head(30621, manager.buzz.pubkey, "app") is None
    assert manager.buzz.head(30617, manager.buzz.pubkey, "repo") == repository
    assert manager.buzz.channel(manager.resource("dev", "channel")["uuid"])


def test_human_invitation_and_memberships(manager):
    calls = []

    def mint(method, path, data):
        calls.append(data)
        return {
            "code": "invite-code",
            "url": "http://test/invite/code",
            "expires_at": 123456789,
        }

    manager.buzz.call = mint
    op = {"action": "create_invite", "id": "alice", "max_uses": 1}
    execute(manager, [op])
    # Different owner messages requesting the same invite ID reuse the result.
    execute(manager, [op])
    assert len(calls) == 1
    person = public(key())
    execute(
        manager, [{"action": "add_human_member", "channel": "office", "pubkey": person}]
    )
    assert manager.buzz.channel(manager.config["office"])["roles"][person] == "member"
    execute(
        manager,
        [{"action": "remove_human_member", "channel": "office", "pubkey": person}],
    )
    assert person not in manager.buzz.channel(manager.config["office"])["roles"]


def test_provider_credentials_never_enter_registry_or_inspection(manager):
    secret = "new-provider-secret"
    manager.store_provider_credential(
        message(manager, "Save credential"), "second", secret
    )
    op = {
        "action": "configure_provider",
        "provider": "custom",
        "model": "new-model",
        "credential": "second",
        "base_url": "https://models.example/v1",
    }
    execute(manager, [op])
    assert manager.secrets["provider_key"] == secret
    assert manager.config["base_url"] == op["base_url"]
    assert secret not in json.dumps(manager.inspect())
    assert secret not in "\n".join(manager.registry.db.iterdump())
    restarted = Manager(manager.root, buzz=manager.buzz, docker=manager.docker)
    assert restarted.secrets["provider_key"] == secret
    assert restarted.config["model"] == "new-model"
    restarted.registry.db.close()
    with pytest.raises(ValueError, match="already exists"):
        store_credential(manager.root, "second", "different-key")
    with pytest.raises(ValueError, match="slug"):
        store_credential(manager.root, "../escape", secret)


def test_new_operations_require_owner_approval(manager):
    agent_key = key()
    manager.buzz.channels[manager.config["office"]]["roles"][public(agent_key)] = "bot"
    source = message(manager, secret=agent_key)
    ops = [
        {"action": "create_project", "id": "new", "name": "New", "channel": "office"}
    ]
    with pytest.raises(ValueError, match="human owner"):
        manager.execute(source, ops)
    with pytest.raises(ValueError, match="human owner"):
        manager.store_provider_credential(source, "stolen", "secret")
    proposal = manager.propose(source, ops)
    assert not manager.registry.list("project")
    approval = message(manager, "approve", reply=proposal["message_id"])
    assert manager.approve(approval_event_id=approval)["state"] == "complete"


def test_owner_can_move_management_conversation(manager, create_ops):
    execute(manager, create_ops)
    channel = manager.resource("dev", "channel")["uuid"]
    execute(manager, [{"action": "add_member", "channel": "dev", "agent": "coa"}])
    source = message(manager, "Create a project here", channel=channel)
    proposal = manager.propose(
        source,
        [{"action": "create_project", "id": "new", "name": "New", "channel": "dev"}],
    )
    event = manager.buzz.events[proposal["message_id"]]
    assert tags(event, "h") == [[channel]]
    approval = message(
        manager, "approve", reply=proposal["message_id"], channel=channel
    )
    assert manager.approve(approval_event_id=approval)["state"] == "complete"


def test_invite_unknown_outcome_does_not_mint_twice(manager):
    calls = []

    def interrupted(*args):
        calls.append(args)
        raise RuntimeError("Interrupted")

    manager.buzz.call = interrupted
    source = message(manager, "Invite a visitor")
    ops = [{"action": "create_invite", "id": "visitor"}]
    assert manager.execute(source, ops)["state"] == "partial_failure"
    retry = manager.execute(source, ops)
    assert "unknown" in retry["error"]
    assert len(calls) == 1


def test_approval_cannot_move_to_another_channel(manager, create_ops):
    execute(manager, create_ops)
    proposal = manager.propose(
        message(manager), [{"action": "delete_channel", "id": "dev"}]
    )
    reply = message(
        manager,
        "approve",
        reply=proposal["message_id"],
        channel=manager.resource("dev", "channel")["uuid"],
    )
    with pytest.raises(ValueError, match="proposal channel"):
        manager.approve(approval_event_id=reply)


def test_credential_chat_is_not_copied_into_registry(manager):
    secret = "key-sent-in-chat"
    source = message(manager, "Store this credential: " + secret)
    manager.store_provider_credential(source, "chat-key", secret)
    assert secret not in "\n".join(manager.registry.db.iterdump())
