"""Run inside the manager of a disposable Image Smoke Test installation only."""

import json
import time

from team_builder.manager import Manager
from team_builder.nostr import key, public, sign, tags


def exercise(owner_secret):
    m = Manager("/state")
    assert m.config["name"] == "Image Smoke Test"
    owner = m.actor(owner_secret)
    counter = 0

    def execute(ops):
        nonlocal counter
        counter += 1
        source = sign(
            owner_secret,
            9,
            [["h", m.config["office"]]],
            f"Test {counter}: {json.dumps(ops)}",
        )
        owner.publish(source)
        result = m.execute(source["id"], ops)
        assert result["state"] == "complete", result
        assert m.execute(source["id"], ops) == result
        time.sleep(1.1)
        return result

    execute([{"action": "create_channel", "id": "research", "name": "Research"}])
    execute([{"action": "update_channel", "id": "research", "visibility": "public"}])
    assert not m.buzz.channel(m.resource("research", "channel")["uuid"])["private"]
    execute([{"action": "update_channel", "id": "research", "visibility": "private"}])
    execute(
        [
            {
                "action": "create_project",
                "id": "study",
                "name": "Study",
                "channel": "research",
            }
        ]
    )
    head = m.buzz.head(30621, m.buzz.pubkey, "study")
    assert tags(head, "name") == [["Study"]]
    execute(
        [
            {
                "action": "update_project",
                "id": "study",
                "name": "Study II",
                "visibility": "unlisted",
            }
        ]
    )
    linked = execute(
        [
            {
                "action": "link_github_repository",
                "project": "study",
                "url": "https://github.com/example/existing-repository",
            }
        ]
    )["results"][0]["repository"]
    repo_head = m.buzz.head(30617, m.buzz.pubkey, linked["id"])
    assert tags(repo_head, "buzz-hosting") == [["external"]]
    assert tags(repo_head, "clone") == [
        ["https://github.com/example/existing-repository.git"]
    ]
    project_head = m.buzz.head(30621, m.buzz.pubkey, "study")
    assert tags(project_head, "a") == [[linked["coordinate"]]]
    execute(
        [
            {
                "action": "link_github_repository",
                "project": "study",
                "url": "https://github.com/EXAMPLE/existing-repository.git/",
            }
        ]
    )
    assert len(m.registry.list("repository")) == 1
    execute([{"action": "delete_project", "id": "study"}])
    person = public(key())
    execute([{"action": "create_invite", "id": "visitor", "max_uses": 1}])
    execute([{"action": "add_human_member", "channel": "research", "pubkey": person}])
    execute(
        [{"action": "remove_human_member", "channel": "research", "pubkey": person}]
    )
    execute([{"action": "delete_channel", "id": "research"}])
    execute([{"action": "stop_agent", "id": "coa"}])
    from team_builder.github_access import configure, store

    store(m.root, "github-test", "github_dummy_integration_token")
    configure(m, {"agent": "coa", "credential": "github-test"})
    token_path = m.root / "agents/coa/managed/github-token"
    assert token_path.read_text() == "github_dummy_integration_token"
    assert m.resource("coa", "agent")["state"] == "stopped"
    m.bootstrap()
    assert m.resource("coa", "agent")["state"] == "stopped"
    execute([{"action": "start_agent", "id": "coa"}])
    assert token_path.read_text() == "github_dummy_integration_token"
    assert m.inspect()["github_credentials"] == ["github-test"]
    execute([{"action": "configure_github_access", "agent": "coa"}])
    assert not token_path.exists()
    execute(
        [
            {"action": "archive_agent", "id": "coa"},
            {"action": "delete_channel", "id": "office"},
        ]
    )
    m.bootstrap()
    assert m.buzz.archived(public(m.secrets["coa"]))
    assert m.buzz.channel(m.config["office"]) is None
    print(
        "PASS: real relay projects, GitHub repository linking and credential lifecycle, invitations, memberships, visibility, deletion, COA lifecycle, and retries"
    )
