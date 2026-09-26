import copy
import json
from unittest.mock import Mock

import pytest
from conftest import message
from test_deployments import setup

from team_builder.deployment_api import handle
from team_builder.deployments import token


def provision(manager, env="production"):
    d = setup(manager)
    p = d.profiles
    credential = p.credential("portal", env, "api-key", value="private-api-secret")
    p.credential("portal", env, "logins", value="private-login-file")
    p.register(
        "portal",
        env,
        {
            "id": "standard",
            "values": {"BACKEND": "rest"},
            "secrets": {"API_KEY": "api-key"},
            "files": {"/app/config/users.yaml": "logins"},
            "required_env": ["API_KEY", "BACKEND"],
        },
    )
    return d, p, credential


def test_profile_plan_and_job_never_expose_values(manager):
    d, p, _ = provision(manager)
    plan = p.plan("portal", "production", "standard", release="r1")
    public = json.dumps(
        [
            p.list("portal", "production"),
            plan,
            p.preflight("portal"),
            d.enqueue(plan["plan_id"]),
        ]
    )
    assert "private-api-secret" not in public and "private-login-file" not in public
    assert p.preflight("portal", profile="standard")["ready"]
    d.install = Mock()
    d.wait_ready = Mock()
    d.run_job(d.get("job/" + plan["plan_id"]))
    app = d.get("app/portal/production")
    assert app["current"] == "r1"
    assert d.secret_values(app)["API_KEY"] == "private-api-secret"
    assert p.mounts(app)[0]["ReadOnly"]
    assert d.get("job/" + plan["plan_id"])["state"] == "succeeded"


def test_owner_approval_required_for_configuration(manager):
    d, p, _ = provision(manager)
    plan = p.plan("portal", "production", "standard")
    source = message(manager, secret=manager.secrets["coa"])
    with pytest.raises(ValueError, match="human owner"):
        manager.execute(
            source, [{"action": "execute_deployment", "plan_id": plan["plan_id"]}]
        )
    proposal = manager.propose(
        source, [{"action": "execute_deployment", "plan_id": plan["plan_id"]}]
    )
    assert not d.get("app/portal/production").get("configuration")
    approval = message(manager, "approve", reply=proposal["message_id"])
    manager.approve(approval_event_id=approval)
    d.install = Mock()
    d.run_job(d.get("job/" + plan["plan_id"]))
    assert d.get("app/portal/production")["configuration"]["profile"] == "standard"
    d.install.assert_not_called()
    assert d.enqueue(plan["plan_id"])["state"] == "succeeded"


def test_scoped_references_and_immutable_profiles(manager):
    d, p, _ = provision(manager)
    app = copy.deepcopy(d.get("app/portal/production")["spec"])
    app["environment"] = "staging"
    d.register(app)
    p.register(
        "portal", "staging", {"id": "standard", "secrets": {"API_KEY": "api-key"}}
    )
    result = p.preflight("portal", "staging", profile="standard")
    assert not result["ready"]  # production's credential must never satisfy staging
    with pytest.raises(ValueError, match="missing"):
        p.plan("portal", "staging", "standard")
    with pytest.raises(ValueError, match="immutable"):
        p.register(
            "portal", "production", {"id": "standard", "values": {"BACKEND": "changed"}}
        )
    with pytest.raises(ValueError):
        p.credential("portal", "production", "../escape", value="bad")


def test_rotation_invalidates_pending_plan_and_blocks_active_job(manager):
    d, p, credential = provision(manager)
    assert (
        p.credential("portal", "production", "api-key", generate=True)["version"]
        == credential["version"]
    )
    plan = p.plan("portal", "production", "standard")
    p.credential(
        "portal", "production", "api-key", value="replacement-private", rotate=True
    )
    with pytest.raises(ValueError, match="credentials changed"):
        d.enqueue(plan["plan_id"])
    new = p.plan("portal", "production", "standard")
    d.enqueue(new["plan_id"])
    with pytest.raises(ValueError, match="active deployment"):
        p.credential("portal", "production", "api-key", generate=True, rotate=True)


def test_preflight_failure_does_not_replace_or_rollback_containers(manager):
    d = setup(manager)
    app = d.get("app/portal/production")
    app["spec"]["services"][0]["health_token_env"] = "MISSING_TOKEN"
    d.put("app/portal/production", "application", app)
    plan = d.plan("portal", release="r1")
    d.enqueue(plan["plan_id"])
    d.install = Mock()
    d.wait_ready = Mock()
    d.run_job(d.get("job/" + plan["plan_id"]))
    d.install.assert_not_called()
    d.wait_ready.assert_not_called()
    job = d.get("job/" + plan["plan_id"])
    assert job["state"] == "failed"
    assert "MISSING_TOKEN" in job["events"][-1]["message"]


def test_connectivity_preflight_is_bounded_and_sanitized(manager):
    _d, p, _ = provision(manager)
    p.register(
        "portal",
        "production",
        {
            "id": "with-db",
            "connections": [{"id": "database", "host": "db", "port": 3306}],
        },
    )
    p.probe = Mock(return_value=False)
    result = p.preflight("portal", profile="with-db")
    assert not result["ready"] and result["checks"][0]["id"] == "database"
    assert "password" not in json.dumps(result)


def test_agents_cannot_register_profiles_or_credential_values(manager):
    _d, _p, _ = provision(manager)
    agent = manager.resource("coa", "agent")
    agent["deployments"] = ["portal/production"]
    manager.save_agent(agent)
    for action in ("credential", "profile"):
        with pytest.raises(ValueError, match="Unknown deployment action"):
            handle(
                manager,
                "/deployments",
                "Bearer " + token(manager.secrets, "coa"),
                {"agent": "coa", "application": "portal", "action": action},
            )
    result = handle(
        manager,
        "/deployments",
        "Bearer " + token(manager.secrets, "coa"),
        {"agent": "coa", "application": "portal", "action": "profiles"},
    )
    assert result["profiles"][0]["secret_refs"] == {"API_KEY": "api-key"}


def test_failed_combined_rollout_preserves_previous_configuration(manager):
    d, p, _ = provision(manager)
    plan = p.plan("portal", "production", "standard", release="r1")
    d.enqueue(plan["plan_id"])
    d.install = Mock(side_effect=RuntimeError("private-error"))
    d.wait_ready = Mock()
    d.run_job(d.get("job/" + plan["plan_id"]))
    assert not d.get("app/portal/production").get("configuration")
    assert "private-error" not in json.dumps(
        d.public_job(d.get("job/" + plan["plan_id"]))
    )


def test_probe_is_scoped_bounded_and_has_no_credentials(manager):
    d, p, _ = provision(manager)
    app = d.get("app/portal/production")
    d.manager.config["manager_image"] = "manager-image"
    calls = []

    def call(method, path, **kw):
        calls.append((method, path, kw))
        assert kw["timeout"] <= 8
        if path.startswith("/networks/"):
            return {"Labels": d.labels(app)}
        if path == "/containers/create":
            spec = kw["json"]
            assert "Env" not in spec
            assert "Mounts" not in spec["HostConfig"]
            assert spec["Labels"]["io.team-builder.component"] == "preflight"
            return {"Id": "probe"}
        if path.endswith("/wait"):
            return {"StatusCode": 0}
        return {}

    d.docker.call = Mock(side_effect=call)
    assert p.probe(app, {"host": "database", "port": 3306})
    assert calls[-1][:2] == ("DELETE", "/containers/probe?force=true")
    d.docker.call = Mock(return_value={"Labels": {"io.team-builder.project": "other"}})
    assert not p.probe(app, {"host": "database", "port": 3306})
    assert d.docker.call.call_count == 1


def test_health_reasons_never_return_raw_probe_output(manager):
    d = setup(manager)
    state = {
        "Health": {
            "Status": "unhealthy",
            "Log": [{"Output": "private credential\nKeyError: 'API_TOKEN'"}],
        }
    }
    assert (
        d.health_reason(state, {"health_token_env": "API_TOKEN"})
        == "Missing health-check credential: API_TOKEN"
    )
    assert "private" not in d.health_reason(state, {})
    state["Health"]["Log"][0]["Output"] = "private value HTTP Error 401"
    assert d.health_reason(state, {}) == "Health endpoint rejected authentication (401)"
