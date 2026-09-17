import json
from unittest.mock import Mock

import pytest
from conftest import message

from team_builder.agent_config import (
    add_catalog,
    apply_config,
    configure,
    inspect_config,
)
from team_builder.credentials import store_credential
from team_builder.runtime import render, start_agent, write_agent_files
from team_builder.storage import private_write
from team_builder.worker_config import install_skills


def test_versions_conflicts_rollback_pending(manager, monkeypatch):
    monkeypatch.setattr("team_builder.runtime.write_agent_files", Mock())
    manager.docker.restart = Mock()
    original = inspect_config(manager, "coa")["settings"]
    changed = {**original, "soul": "Be concise"}
    assert configure(manager, "coa", 0, changed)["status"] == "starting"
    with pytest.raises(ValueError, match="Configuration changed"):
        configure(manager, "coa", 0, original)
    assert len(inspect_config(manager, "coa")["history"]) == 2
    assert configure(manager, "coa", 1, changed)["status"] == "unchanged"
    manager.docker.restart.side_effect = RuntimeError("private-token")
    result = configure(manager, "coa", 1, original)
    assert result["status"] == "pending" and "private-token" not in json.dumps(result)
    manager.docker.restart.side_effect = None
    assert apply_config(manager, "coa")["revision"] == 2
    assert inspect_config(manager, "coa")["settings"] == original
    assert manager.docker.stops == []


def test_catalog_credentials_and_runtime_assignment(manager, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    store_credential(manager.root, "research", "super-secret-value")
    add_catalog(
        manager,
        {
            "id": "search-v1",
            "kind": "mcp",
            "name": "Search",
            "url": "https://example.com/mcp",
            "credential": "research",
            "tools": ["search"],
        },
    )
    add_catalog(
        manager,
        {
            "id": "review-v1",
            "kind": "skill",
            "name": "Review",
            "content": "Review changes carefully.",
        },
    )
    with pytest.raises(ValueError, match="immutable"):
        add_catalog(
            manager,
            {
                "id": "review-v1",
                "kind": "skill",
                "name": "Review",
                "content": "Changed",
            },
        )
    with pytest.raises(ValueError, match="embedded credentials"):
        add_catalog(
            manager,
            {
                "id": "bad",
                "kind": "mcp",
                "name": "Bad",
                "url": "https://user:secret@example.com/mcp",
            },
        )
    agent = manager.resource("coa", "agent")
    agent.update(mcp=["search-v1"], skills=["review-v1"], tools=["file"])
    document, env, _ = write_agent_files(
        manager.root, manager.config, manager.secrets, agent
    )
    assert "team" in document["mcp_servers"]
    assigned = document["mcp_servers"]["assigned-search-v1"]
    assert assigned["headers"]["Authorization"] == "Bearer super-secret-value"
    assert assigned["tools"] == {"include": ["search"]}
    assert document["platform_toolsets"]["buzz"] == ["file", "mcp"]
    assert "super-secret-value" not in json.dumps(inspect_config(manager, "coa"))
    assert "TEAM_BUILDER_TOKEN" not in env
    ordinary = {**agent, "id": "engineer"}
    doc, _, _ = render(manager.config, manager.secrets, ordinary)
    assert "team" not in doc["mcp_servers"]


def test_managed_skills_preserve_unmanaged(tmp_path):
    home, managed = tmp_path / "home", tmp_path / "managed"
    private_write(home / "skills" / "personal" / "SKILL.md", b"Keep me")
    private_write(managed / "skills.json", {"review": "Review code"})
    install_skills(home, managed)
    assert (home / "skills/team-managed-review/SKILL.md").read_text() == "Review code"
    private_write(managed / "skills.json", {})
    install_skills(home, managed)
    assert not (home / "skills/team-managed-review/SKILL.md").exists()
    assert (home / "skills/personal/SKILL.md").read_text() == "Keep me"


def test_config_updates_preserve_container_generation(manager, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    docker = Mock()
    docker.inspect.return_value = {"Id": "stable", "State": {"Running": True}}
    agent = manager.resource("coa", "agent")
    start_agent(manager.root, manager.config, manager.secrets, agent, docker)
    first = docker.ensure.call_args.args[2]
    docker.restart.reset_mock()
    start_agent(manager.root, manager.config, manager.secrets, agent, docker)
    docker.restart.assert_not_called()
    agent["instructions"] = "Updated instructions"
    start_agent(manager.root, manager.config, manager.secrets, agent, docker)
    assert docker.ensure.call_args.args[2] == first
    docker.restart.assert_called_once_with(manager.name(agent))


def test_coa_config_operation_requires_owner(manager):
    value = inspect_config(manager, "coa")["settings"]
    op = {
        "action": "configure_agent",
        "id": "coa",
        "expected_revision": 0,
        "settings": value,
    }
    with pytest.raises(ValueError, match="human owner"):
        manager.execute(message(manager, secret=manager.secrets["coa"]), [op])
    assert manager.execute(message(manager), [op])["state"] == "complete"


def test_archived_unknown_and_applied_state(manager):
    value = inspect_config(manager, "coa")["settings"]
    with pytest.raises(ValueError, match="Unknown catalog"):
        configure(manager, "coa", 0, {**value, "skills": ["missing"]})
    home = manager.root / "agents/coa/home/.hermes"
    private_write(
        home / "team-builder-applied.json",
        {"pid": 12, "start_time": 123, "revision": 0},
    )
    private_write(home / "gateway_state.json", {"pid": 13, "start_time": 123})
    assert inspect_config(manager, "coa")["applied_revision"] is None
    private_write(home / "gateway_state.json", {"pid": 12, "start_time": 123})
    assert inspect_config(manager, "coa")["applied_revision"] == 0
    agent = manager.resource("coa", "agent")
    agent["state"] = "archived"
    manager.save_agent(agent)
    with pytest.raises(ValueError, match="Archived"):
        configure(manager, "coa", 0, value)


def test_dashboard_configuration_boundary(manager, monkeypatch):
    import hashlib
    import threading
    from types import SimpleNamespace

    import httpx

    from team_builder.dashboard import PREFIX, serve

    monkeypatch.setattr("team_builder.runtime.write_agent_files", Mock())
    manager.docker.restart = Mock()
    private_write(
        manager.root / "dashboard-auth.json",
        {"sha256": hashlib.sha256(b"test-key").hexdigest()},
    )
    server = serve(
        SimpleNamespace(root=manager.root, manager=manager), ("127.0.0.1", 0)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        with httpx.Client(
            base_url=origin, trust_env=False, headers={"Origin": origin}
        ) as client:
            route = PREFIX + "/agents/coa/configuration"
            assert client.get(route).status_code == 401
            assert client.post(route, json={}).status_code == 401
            assert (
                client.post(PREFIX + "/login", json={"key": "test-key"}).status_code
                == 200
            )
            value = client.get(route).json()["settings"]
            value["soul"] = "Be clear"
            body = {"expected_revision": 0, "settings": value}
            assert (
                client.post(
                    route, json=body, headers={"Origin": "http://evil"}
                ).status_code
                == 403
            )
            assert client.post(route, json=body).json()["revision"] == 1
            assert client.post(route, json=body).status_code == 409
            assert (
                client.post(route, json={**body, "token": "private-token"}).status_code
                == 400
            )
            assert client.post(PREFIX + "/execute", json={}).status_code == 405
            assert client.get(PREFIX + "/agents/other/configuration").status_code == 404
            result = client.get(route).json()
            assert result["history"][0]["source"] == "dashboard"
            assert manager.secrets["token"] not in json.dumps(result)
            assert (
                client.post(
                    PREFIX + "/agents/coa/apply-configuration", json={}
                ).status_code
                == 200
            )
    finally:
        server.shutdown()
        server.server_close()


def test_incomplete_bundle_cannot_load(manager, monkeypatch):
    from team_builder.worker_config import verify_bundle

    monkeypatch.setattr("os.chown", lambda *args: None)
    agent = manager.resource("coa", "agent")
    write_agent_files(manager.root, manager.config, manager.secrets, agent)
    managed = manager.root / "agents/coa/managed"
    verify_bundle(managed)
    private_write(managed / "SOUL.md", b"Interrupted write")
    with pytest.raises(ValueError, match="Incomplete configuration write"):
        verify_bundle(managed)
    write_agent_files(manager.root, manager.config, manager.secrets, agent)
    verify_bundle(managed)


def test_stopped_configuration_is_saved_without_restart(manager, monkeypatch):
    writer = Mock()
    monkeypatch.setattr("team_builder.runtime.write_agent_files", writer)
    manager.docker.restart = Mock()
    agent = manager.resource("coa", "agent")
    agent["state"] = "stopped"
    manager.save_agent(agent)
    value = inspect_config(manager, "coa")["settings"]
    result = configure(manager, "coa", 0, {**value, "name": "Team Chief"})
    assert result["status"] == "saved"
    assert manager.resource("coa", "agent")["state"] == "stopped"
    manager.docker.restart.assert_not_called()
    writer.assert_called_once()
    assert manager.buzz.head(0, agent["pubkey"]) is not None
