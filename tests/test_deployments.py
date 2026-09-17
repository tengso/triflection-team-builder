import copy
import json
from unittest.mock import Mock

import pytest
from conftest import message

from team_builder.deployment_api import handle
from team_builder.deployments import Deployments, token
from team_builder.runtime import render

IMAGE = "sha256:" + "a" * 64


def setup(manager):
    d = manager.deployments
    d.register(
        {
            "id": "portal",
            "repository": "tengso/portal",
            "services": [
                {
                    "id": "ui",
                    "command": ["ui"],
                    "port": 8502,
                    "host_port": 38502,
                    "health_path": "/healthz",
                }
            ],
        },
        secrets={"API_KEY": "very-private-value"},
    )
    d.release(
        {
            "id": "r1",
            "application": "portal",
            "commit": "a" * 40,
            "images": {"ui": IMAGE},
        }
    )
    return d


def test_immutable_release_spec_and_plan(manager):
    d = setup(manager)
    with pytest.raises(ValueError):
        d.release(
            {
                "id": "r1",
                "application": "portal",
                "commit": "a" * 40,
                "images": {"ui": "repo:latest"},
            }
        )
    with pytest.raises(ValueError, match="immutable"):
        d.release(
            {
                "id": "r1",
                "application": "portal",
                "commit": "b" * 40,
                "images": {"ui": IMAGE},
            }
        )
    p = d.plan("portal", release="r1")
    assert p == d.plan("portal", release="r1")
    assert "very-private-value" not in json.dumps(p)
    app = d.get("app/portal/production")
    app["revision"] += 1
    d.put("app/portal/production", "application", app)
    with pytest.raises(ValueError, match="changed"):
        d.enqueue(p["plan_id"])


def test_durable_queue_recovery_duplicate_and_failure(manager):
    d = setup(manager)
    p = d.plan("portal", release="r1")
    first = d.enqueue(p["plan_id"])
    assert d.enqueue(p["plan_id"]) == first
    recovered = Deployments(manager)
    assert recovered.get("job/" + p["plan_id"])["state"] == "queued"
    recovered.install = Mock()
    recovered.wait_ready = Mock()
    recovered.run_job(recovered.get("job/" + p["plan_id"]))
    assert recovered.get("app/portal/production")["current"] == "r1"
    assert recovered.enqueue(p["plan_id"])["state"] == "succeeded"
    recovered.release(
        {
            "id": "r2",
            "application": "portal",
            "commit": "b" * 40,
            "images": {"ui": IMAGE},
        }
    )
    p2 = recovered.plan("portal", release="r2")
    recovered.enqueue(p2["plan_id"])
    recovered.wait_ready.side_effect = [RuntimeError("secret exception"), None]
    recovered.run_job(recovered.get("job/" + p2["plan_id"]))
    job = recovered.get("job/" + p2["plan_id"])
    assert job["state"] == "rolled_back"
    assert recovered.get("app/portal/production")["current"] == "r1"
    assert "secret exception" not in json.dumps(job)
    assert recovered.install.call_count == 3


def test_owner_authorization_and_frozen_proposal(manager):
    d = setup(manager)
    p = d.plan("portal", release="r1")
    op = {"action": "execute_deployment", "plan_id": p["plan_id"]}
    coa = message(manager, "deploy", secret=manager.secrets["coa"])
    with pytest.raises(ValueError):
        manager.execute(coa, [op])
    proposal = manager.propose(coa, [op])
    text = manager.buzz.events[proposal["message_id"]]["content"]
    assert "r1" in text and IMAGE in text and "very-private-value" not in text
    unrelated = message(manager, "approve")
    with pytest.raises(ValueError):
        manager.approve(approval_event_id=unrelated)
    approval = message(manager, "approve", reply=proposal["message_id"])
    result = manager.approve(approval_event_id=approval)
    assert result["results"][0]["state"] == "queued"
    assert manager.approve(approval_event_id=approval) == result
    with pytest.raises(ValueError):
        manager.execute(approval, [{"action": "stop_agent", "id": "coa"}])


def test_scoped_agent_and_operator_credentials(manager, monkeypatch):
    d = setup(manager)
    monkeypatch.setattr("team_builder.runtime.write_agent_files", Mock())
    manager.docker.restart = Mock()
    d.grant("coa", "portal")
    req = {"agent": "coa", "application": "portal", "action": "releases"}
    result = handle(
        manager,
        "/deployments",
        "Bearer " + token(manager.secrets, "coa"),
        copy.deepcopy(req),
    )
    assert result["releases"][0]["id"] == "r1"
    for bad in [
        manager.secrets["token"],
        token(manager.secrets),
        token(manager.secrets, "another"),
    ]:
        with pytest.raises(ValueError):
            handle(manager, "/deployments", "Bearer " + bad, copy.deepcopy(req))
    with pytest.raises(ValueError):
        handle(
            manager,
            "/operator/deployments",
            "Bearer " + token(manager.secrets, "coa"),
            {"action": "inspect"},
        )
    d.grant("coa", "portal", allowed=False)
    with pytest.raises(ValueError):
        handle(
            manager,
            "/deployments",
            "Bearer " + token(manager.secrets, "coa"),
            copy.deepcopy(req),
        )
    agent = manager.resource("coa", "agent")
    agent["deployments"] = ["portal/production"]
    config, _, _ = render(manager.config, manager.secrets, agent)
    assert config["mcp_servers"]["deployments"]["env"]["DEPLOYMENT_TOKEN"] == token(
        manager.secrets, "coa"
    )


def test_environment_serialization_and_scope(manager):
    d = setup(manager)
    p = d.plan("portal", release="r1")
    d.enqueue(p["plan_id"])
    d.release(
        {
            "id": "r2",
            "application": "portal",
            "commit": "b" * 40,
            "images": {"ui": IMAGE},
        }
    )
    p2 = d.plan("portal", release="r2")
    with pytest.raises(ValueError, match="already active"):
        d.enqueue(p2["plan_id"])
    d.docker.inspect = Mock(
        return_value={
            "Config": {
                "Labels": {
                    "io.team-builder.project": "tb-test",
                    "io.team-builder.application": "other",
                }
            }
        }
    )
    with pytest.raises(ValueError, match="belong"):
        d.inspect(d.get("app/portal/production"), "ui")


def test_operator_plan_and_read_are_non_mutating(manager):
    d = setup(manager)
    plan = handle(
        manager,
        "/operator/deployments",
        "Bearer " + token(manager.secrets),
        {"action": "plan", "application": "portal", "release": "r1"},
    )
    assert plan["plan_id"]
    assert d.db.list("job") == []
    assert d.read()["stale"] is True


def test_secret_rotation_invalidates_old_plan_and_blocks_active_job(manager):
    d = setup(manager)
    app = d.get("app/portal/production")["spec"]
    old = d.plan("portal", release="r1")
    d.register(app, secrets={"API_KEY": "replacement-secret"})
    with pytest.raises(ValueError, match="changed"):
        d.enqueue(old["plan_id"])
    new = d.plan("portal", release="r1")
    d.enqueue(new["plan_id"])
    with pytest.raises(ValueError, match="active deployment"):
        d.register(app, secrets={"API_KEY": "another-secret"})
    assert (
        d.secret_values(d.get("app/portal/production"))["API_KEY"]
        == "replacement-secret"
    )


def test_dashboard_deployment_routes_require_owner(manager):
    import hashlib
    import threading
    from types import SimpleNamespace

    import httpx

    from team_builder.dashboard import PREFIX, serve
    from team_builder.storage import private_write

    private_write(
        manager.root / "dashboard-auth.json",
        {"sha256": hashlib.sha256(b"test-key").hexdigest()},
    )
    server = serve(
        SimpleNamespace(root=manager.root, manager=manager), ("127.0.0.1", 0)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            assert client.get(PREFIX + "/deployments").status_code == 401
            assert (
                client.post(
                    PREFIX + "/login",
                    json={"key": "test-key"},
                    headers={"Origin": origin},
                ).status_code
                == 200
            )
            assert client.get(PREFIX + "/deployments").json()["stale"] is True
            assert (
                client.post(
                    PREFIX + "/deployments/execute", json={}, headers={"Origin": origin}
                ).status_code
                == 405
            )
            assert (
                client.get(
                    PREFIX
                    + "/deployments/logs?application=unknown&environment=production&service=ui"
                ).status_code
                == 503
            )
    finally:
        server.shutdown()
        server.server_close()
