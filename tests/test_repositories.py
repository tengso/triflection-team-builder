import json

import pytest
from conftest import message

from team_builder.models import validate
from team_builder.nostr import key, public, tags
from team_builder.repositories import github_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Owner/Repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://github.com/OWNER/REPO.git/",
    ],
)
def test_normalize_github_url(url):
    assert github_url(url) == "https://github.com/owner/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://token@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "https://github.com/owner/repo#readme",
        "https://github.com.evil/owner/repo",
        "http://github.com/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/../repo",
        "https://github.com/owner/.git",
        "https://github.com/owner/repo\n",
        "https://github.com/owner/%72epo",
        "https://github.com/owner/İrepo",
    ],
)
def test_reject_invalid_or_credential_bearing_url(url):
    with pytest.raises(ValueError):
        validate([{"action": "link_github_repository", "project": "app", "url": url}])


def create_project(manager, id="app"):
    ops = [{"action": "create_project", "id": id, "name": id, "channel": "office"}]
    assert manager.execute(message(manager, "Create " + id), ops)["state"] == "complete"


def link(manager, url="https://github.com/example/repo", project="app"):
    ops = [{"action": "link_github_repository", "project": project, "url": url}]
    source = message(manager, json.dumps(ops))
    result = manager.execute(source, ops)
    assert result["state"] == "complete", result
    assert manager.execute(source, ops) == result
    return result["results"][0]


def test_link_announces_github_and_preserves_existing_repositories(manager):
    create_project(manager)
    first = link(manager)
    second = link(manager, "https://github.com/example/second")
    repo = first["repository"]
    head = manager.buzz.head(30617, manager.buzz.pubkey, repo["id"])
    assert tags(head, "buzz-hosting") == [["external"]]
    assert tags(head, "web") == [["https://github.com/example/repo"]]
    assert tags(head, "clone") == [["https://github.com/example/repo.git"]]
    assert set(manager.resource("app", "project")["repositories"]) == {
        repo["coordinate"],
        second["repository"]["coordinate"],
    }
    repeated = link(manager, "https://github.com/EXAMPLE/REPO.git/")
    assert repeated["repository"]["coordinate"] == repo["coordinate"]
    assert len(manager.registry.list("repository")) == 2
    create_project(manager, "other")
    assert (
        link(manager, project="other")["repository"]["coordinate"] == repo["coordinate"]
    )
    assert manager.inspect_projects()["repositories"]


def test_link_retry_after_announcement_preserves_identity(manager, monkeypatch):
    create_project(manager)
    original = manager.buzz.replace

    def fail_project(kind, *args):
        if kind == 30621:
            raise RuntimeError("Interrupted project update")
        return original(kind, *args)

    monkeypatch.setattr(manager.buzz, "replace", fail_project)
    ops = [
        {
            "action": "link_github_repository",
            "project": "app",
            "url": "https://github.com/example/repo",
        }
    ]
    source = message(manager, "Link repository")
    assert manager.execute(source, ops)["state"] == "partial_failure"
    repo = manager.registry.list("repository")[0]
    monkeypatch.setattr(manager.buzz, "replace", original)
    assert manager.execute(source, ops)["state"] == "complete"
    assert manager.resource("app", "project")["repositories"] == [repo["coordinate"]]
    assert len([e for e in manager.buzz.events.values() if e["kind"] == 30617]) == 1


def test_link_requires_owner_and_rejects_deleted_project(manager):
    create_project(manager)
    agent = key()
    manager.buzz.channels[manager.config["office"]]["roles"][public(agent)] = "bot"
    ops = [
        {
            "action": "link_github_repository",
            "project": "app",
            "url": "https://github.com/example/repo",
        }
    ]
    with pytest.raises(ValueError, match="human owner"):
        manager.execute(message(manager, secret=agent), ops)
    assert not manager.registry.list("repository")
    manager.apply({"action": "delete_project", "id": "app"})
    result = manager.execute(message(manager, "Link to deleted project"), ops)
    assert result["state"] == "partial_failure" and "deleted" in result["error"]
    assert not manager.registry.list("repository")
