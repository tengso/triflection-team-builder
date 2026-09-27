import copy
import json
from unittest.mock import Mock

import pytest
from test_deployments import setup

from team_builder.deployment_api import handle
from team_builder.deployments import Deployments, token


def prepare(manager):
    d = setup(manager)
    app = copy.deepcopy(d.get("app/portal/production")["spec"])
    app["environment"] = "staging"
    d.register(app)
    for env in ("staging", "production"):
        d.release(
            {
                "id": "ci-20-1",
                "application": "portal",
                "environment": env,
                "commit": "a" * 40,
                "images": {"ui": "sha256:" + "a" * 64},
            }
        )
        d.profiles.register(
            "portal", env, {"id": "standard", "values": {"BACKEND": "rest"}}
        )
    d.authorized = Mock()
    policy = {
        "application": "portal",
        "enabled": True,
        "staging_agent": "cody",
        "production_agent": "oppo",
        "staging_profile": "standard",
        "production_profile": "standard",
        "checks": [
            {
                "id": "functional",
                "service": "ui",
                "command": ["python", "-c", "assert True"],
            }
        ],
    }
    d.automation.configure(policy)
    d.install = Mock()
    d.wait_ready = Mock()
    d.inspect = Mock(
        return_value={
            "Id": "container",
            "Image": "sha256:" + "a" * 64,
            "State": {"Health": {"Status": "healthy"}},
        }
    )
    d.automation.check = Mock(return_value=(0, ""))
    return d, policy


def run_jobs(d):
    for job in d.db.list("job"):
        if job["state"] in ("queued", "running"):
            d.run_job(job)


def test_complete_automatic_handoff_no_owner_messages(manager):
    d, _ = prepare(manager)
    d.automation.tick()
    run_jobs(d)
    d.automation.tick()
    run = d.automation.status("portal")["runs"][0]
    assert run["stage"] == "production" and run["evidence"]["release"] == "ci-20-1"
    d.automation.tick()
    run_jobs(d)
    d.automation.tick()
    run = d.automation.status("portal")["runs"][0]
    assert run["state"] == "succeeded"
    assert {j["environment"]: j["actor"] for j in d.db.list("job")} == {
        "staging": "cody",
        "production": "oppo",
    }
    assert all(j["source"].startswith("release-policy/") for j in d.db.list("job"))
    d.automation.tick()
    assert len(d.db.list("job")) == 2
    assert "assert True" not in json.dumps(d.automation.status("portal"))


def test_failed_acceptance_never_promotes_and_retries_bounded(manager):
    d, _ = prepare(manager)
    d.automation.check.side_effect = RuntimeError("SECRET application output")
    d.automation.tick()
    run_jobs(d)
    d.automation.tick()
    run = d.automation.status("portal")["runs"][0]
    assert run["state"] == "blocked" and run["stage"] == "staging"
    assert len(d.db.list("job")) == 1
    assert "SECRET" not in json.dumps(run)
    with pytest.raises(ValueError):
        d.automation.retry("portal", "production", "oppo")
    for _ in range(2):
        d.automation.retry("portal", "staging", "cody")
        d.automation.tick()
        run_jobs(d)
        d.automation.tick()
    with pytest.raises(ValueError, match="limit"):
        d.automation.retry("portal", "staging", "cody")


def test_stale_evidence_stops_queued_production_before_install(manager):
    d, _ = prepare(manager)
    d.automation.tick()
    run_jobs(d)
    d.automation.tick()
    d.automation.tick()
    app = d.get("app/portal/staging")
    app["revision"] += 1
    d.put("app/portal/staging", "application", app)
    calls = d.install.call_count
    run_jobs(d)
    assert d.install.call_count == calls
    assert (
        next(j for j in d.db.list("job") if j["environment"] == "production")["state"]
        == "failed"
    )


def test_policy_disable_and_grant_revocation_invalidate_queue(manager):
    d, policy = prepare(manager)
    d.automation.tick()
    policy["enabled"] = False
    d.automation.configure(policy)
    run_jobs(d)
    d.install.assert_not_called()
    assert d.db.list("job")[0]["state"] == "failed"


def test_revoked_agent_cannot_execute_queued_policy_job(manager):
    d, _ = prepare(manager)
    d.automation.tick()
    d.authorized.side_effect = ValueError("revoked")
    run_jobs(d)
    d.install.assert_not_called()


def test_no_matching_production_artifact_no_deployment(manager):
    d, _ = prepare(manager)
    release = d.get("release/portal/production/ci-20-1")
    release["images"]["ui"] = "sha256:" + "b" * 64
    d.put("release/portal/production/ci-20-1", "release", release)
    d.automation.tick()
    assert not d.db.list("job")


def test_restart_uses_durable_stage_plan(manager):
    d, _ = prepare(manager)
    d.automation.tick()
    recovered = Deployments(manager)
    recovered.authorized = Mock()
    recovered.automation.tick()
    assert len(recovered.db.list("job")) == 1
    assert recovered.automation.status("portal")["runs"][0]["stage"] == "staging"


def test_agent_cannot_define_policy_or_forge_evidence(manager):
    prepare(manager)
    for action in ("automation-policy", "acceptance"):
        with pytest.raises(ValueError, match="Unknown deployment action"):
            handle(
                manager,
                "/deployments",
                "Bearer " + token(manager.secrets, "coa"),
                {
                    "agent": "coa",
                    "application": "portal",
                    "environment": "production",
                    "action": action,
                    "policy": {},
                    "evidence": {"passed": True},
                },
            )


def test_existing_healthy_profile_release_is_checked_without_redeploy(manager):
    d, _ = prepare(manager)
    for env in ("staging", "production"):
        app = d.get("app/portal/" + env)
        app.update(
            current="ci-20-1",
            configuration=d.profiles.binding("portal", env, "standard"),
        )
        d.put("app/portal/" + env, "application", app)
    d.automation.tick()
    d.automation.tick()
    run = d.automation.status("portal")["runs"][0]
    assert run["state"] == "succeeded" and run["production_retained"]
    assert d.automation.check.call_count == 2
    assert not d.db.list("job")


def test_check_wrapper_bounds_output_timeout_and_environment(manager):
    d, policy = prepare(manager)
    from team_builder.release_automation import Automation

    d.docker.exec_output = Mock(return_value=(0, ""))
    check = {
        **policy["checks"][0],
        "command": ["python", "-m", "acceptance", "--env", "{environment}"],
        "timeout": 3,
    }
    Automation(d).check(d.get("app/portal/production"), check, "staging")
    args = d.docker.exec_output.call_args.args[1]
    assert "killpg" in args[2] and "[-8000:]" in args[2] and args[-1] == "3"
    assert json.loads(args[3]) == ["python", "-m", "acceptance", "--env", "staging"]
    assert d.docker.exec_output.call_args.kwargs["timeout"] == 33


def test_notification_retries_same_signed_event_without_duplicate(manager):
    d, policy = prepare(manager)
    policy["notification_channel"] = "office"
    actual = manager.resource("coa", "agent")
    manager.resource = Mock(
        side_effect=lambda id, kind: (
            {"uuid": "channel"}
            if kind == "channel"
            else {**actual, "id": id, "name": id, "channel_ids": ["channel"]}
        )
    )
    manager.buzz.channel = Mock(return_value={"roles": {actual["pubkey"]: "member"}})
    actor = Mock()
    manager.actor = Mock(return_value=actor)
    run = {
        "id": "run",
        "application": "portal",
        "release": "ci-20-1",
        "state": "blocked",
        "stage": "production",
        "reason": "Technical check failed",
    }
    actor.publish.side_effect = [RuntimeError("transient"), None]
    d.automation.notify(run, policy)
    d.automation.notify(run, policy)
    d.automation.notify(run, policy)
    assert actor.publish.call_count == 2
    assert actor.publish.call_args_list[0] == actor.publish.call_args_list[1]
    event = actor.publish.call_args.args[0]
    # buzz-acp wakes an agent only for a p tag naming it.
    assert ["p", actual["pubkey"]] in event["tags"]


def test_owner_is_asked_for_missing_credentials_with_host_command(manager):
    d, policy = prepare(manager)
    for env in ("staging", "production"):
        d.profiles.register(
            "portal",
            env,
            {"id": "secret-v1", "secrets": {"DB_PASSWORD": "db-password"}},
        )
    policy.update(staging_profile="secret-v1", production_profile="secret-v1")
    d.automation.configure(policy)
    d.automation.tick()
    run = d.automation.status("portal")["runs"][0]
    assert run["state"] == "blocked" and "db-password" in run["reason"]
    assert (
        "team-builder deployment db-password.json --secret-file"
        in (run["owner_action"])
    )
    policy["notification_channel"] = "office"
    owner = manager.config["owner"]
    actual = manager.resource("coa", "agent")
    manager.resource = Mock(
        side_effect=lambda id, kind: (
            {"uuid": "channel"}
            if kind == "channel"
            else {**actual, "id": id, "name": id, "channel_ids": ["channel"]}
        )
    )
    manager.buzz.channel = Mock(
        return_value={"roles": {actual["pubkey"]: "member", owner: "owner"}}
    )
    actor = Mock()
    manager.actor = Mock(return_value=actor)
    d.automation.notify(run, policy)
    events = [c.args[0] for c in actor.publish.call_args_list]
    assert any(
        ["p", owner] in e["tags"] and "db-password" in e["content"] for e in events
    )


def test_failed_check_output_is_masked_and_summary_mode_hides_it(manager):
    d, policy = prepare(manager)
    secret = manager.secrets["admin"]
    d.automation.check = Mock(
        return_value=(1, "Traceback: token=abc123def456 " + secret + " db refused")
    )
    results = d.automation.run_checks(
        "portal", policy | {"staging_diagnostics": "redacted"}, "staging"
    )
    assert results[0]["passed"] is False and results[0]["exit_code"] == 1
    assert "db refused" in results[0]["output"]
    assert (
        secret not in results[0]["output"]
        and "abc123def456" not in results[0]["output"]
    )
    hidden = d.automation.run_checks(
        "portal", policy | {"production_diagnostics": "summary"}, "production"
    )
    assert "output" not in hidden[0]
    d.automation.check = Mock(return_value=(0, "all good"))
    assert "output" not in d.automation.run_checks("portal", policy, "staging")[0]


def test_agents_verify_on_demand_without_spending_retries(manager):
    d, _ = prepare(manager)
    with pytest.raises(ValueError, match="assigned release agent"):
        d.automation.verify("portal", "staging", "oppo")
    result = d.automation.verify("portal", "staging", "cody")
    assert result["passed"] and result["checks"][0]["id"] == "functional"
    with pytest.raises(ValueError, match="less than a minute"):
        d.automation.verify("portal", "staging", "cody")
    assert not d.db.list("job")


def failing_production(manager, rollback="agent"):
    d, policy = prepare(manager)
    policy["production_rollback"] = rollback
    d.automation.configure(policy)
    app = d.get("app/portal/production")
    app["current"] = "r1"
    d.put("app/portal/production", "application", app)
    d.automation.check = Mock(
        side_effect=lambda app, check, env: (0, "") if env == "staging" else (1, "x")
    )
    for _ in range(3):
        d.automation.tick()
        run_jobs(d)
    d.automation.tick()
    return d, d.automation.status("portal")["runs"][0]


def test_production_agent_rolls_back_when_policy_allows(manager):
    d, run = failing_production(manager)
    assert run["state"] == "blocked" and run["stage"] == "production"
    with pytest.raises(ValueError, match="production agent"):
        d.automation.rollback("portal", "cody")
    job = d.automation.rollback("portal", "oppo")
    assert job["action"] == "rollback" and job["release"] == "r1"
    assert job["actor"] == "oppo" and job["source"].startswith(
        "release-policy-rollback/"
    )
    assert "r1" in d.automation.status("portal")["runs"][0]["reason"]


def test_automatic_rollback_and_disabled_rollback(manager):
    d, run = failing_production(manager, "automatic")
    assert run["rollback"]["release"] == "r1"
    assert any(j["action"] == "rollback" for j in d.db.list("job"))


def test_rollback_disabled_requires_owner(manager):
    d, _ = failing_production(manager, "off")
    with pytest.raises(ValueError, match="owner approval"):
        d.automation.rollback("portal", "oppo")


def test_detached_checks_do_not_attach_unused_output_streams():
    from team_builder.docker import Docker

    docker = object.__new__(Docker)
    docker.inspect = Mock(return_value={"Id": "managed"})
    docker.call = Mock(
        side_effect=[{"Id": "check"}, None, {"Running": False, "ExitCode": 0}]
    )
    docker.exec("managed", ["true"])
    assert docker.call.call_args_list[0].kwargs["json"] == {
        "Cmd": ["true"],
        "AttachStdout": False,
        "AttachStderr": False,
    }
    assert docker.call.call_args_list[1].kwargs["json"] == {"Detach": True}
