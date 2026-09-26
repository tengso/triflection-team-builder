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
    d.automation.check = Mock()
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


def test_check_wrapper_discards_output_and_enforces_timeout(manager):
    d, policy = prepare(manager)
    from team_builder.release_automation import Automation

    d.docker.exec = Mock()
    Automation(d).check(
        d.get("app/portal/production"), {**policy["checks"][0], "timeout": 3}
    )
    args = d.docker.exec.call_args.args[1]
    assert "DEVNULL" in args[2] and "killpg" in args[2] and args[-1] == "3"


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
