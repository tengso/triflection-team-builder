import json
from unittest.mock import Mock

import pytest
from conftest import message
from test_deployments import setup

from team_builder.deployment_api import handle
from team_builder.deployments import token


def spec(environment):
    return {
        "id": "ledger",
        "environment": environment,
        "repository": "acme/ledger",
        "services": [
            {"id": "api", "command": ["api"], "port": 8000, "health_path": "/health"}
        ],
    }


def setup_operations():
    return [
        {"action": "register_application", "spec": spec("staging")},
        {"action": "register_application", "spec": spec("production")},
        {
            "action": "generate_credential",
            "application": "ledger",
            "environment": "staging",
            "id": "api-token",
        },
        {
            "action": "register_profile",
            "application": "ledger",
            "environment": "staging",
            "profile": {
                "id": "standard-v1",
                "values": {"DB_HOST": "ledger-db"},
                "secrets": {"API_TOKEN": "api-token"},
                "required_env": ["API_TOKEN"],
            },
        },
        {
            "action": "configure_deployment_access",
            "agent": "engineer",
            "application": "ledger",
            "environment": "staging",
        },
    ]


def agent_call(manager, agent, action, **kw):
    return handle(
        manager,
        "/deployments",
        "Bearer " + token(manager.secrets, agent),
        {"agent": agent, "action": action, **kw},
    )


def engineer(manager, create_ops, monkeypatch, **fields):
    monkeypatch.setattr("os.chown", lambda *args: None)
    manager.execute(message(manager), create_ops)
    agent = manager.resource("engineer", "agent")
    agent.update(fields)
    manager.save_agent(agent)
    return agent, agent["channel_ids"][0]


def test_release_agent_proposal_executes_only_after_owner_approval(
    manager, create_ops, monkeypatch
):
    agent, channel = engineer(manager, create_ops, monkeypatch, release_agent=True)
    source = message(manager, "Set up the ledger application", channel=channel)
    proposal = agent_call(
        manager,
        "engineer",
        "propose-change",
        source_event_id=source,
        operations=setup_operations(),
    )
    event = manager.buzz.events[proposal["message_id"]]
    assert event["pubkey"] == agent["pubkey"]
    assert event["content"].startswith("Proposed release configuration changes")
    d = manager.deployments
    assert not d.db.get("app/ledger/staging")
    forged = message(
        manager,
        "approve",
        secret=agent["secret"],
        channel=channel,
        reply=proposal["message_id"],
    )
    with pytest.raises(ValueError, match="human owner"):
        agent_call(manager, "engineer", "approve-change", approval_event_id=forged)
    approval = message(
        manager, "approve", channel=channel, reply=proposal["message_id"]
    )
    result = agent_call(
        manager, "engineer", "approve-change", approval_event_id=approval
    )
    assert result["state"] == "complete"
    assert d.db.get("app/ledger/production")
    assert "ledger/staging" in manager.resource("engineer", "agent")["deployments"]
    assert d.profiles.binding("ledger", "staging", "standard-v1")["versions"][
        "api-token"
    ]
    assert "version" not in json.dumps(result["results"][2])
    assert (
        agent_call(manager, "engineer", "approve-change", approval_event_id=approval)
        == result
    )


def test_agent_proposals_are_scoped(manager, create_ops, monkeypatch):
    setup(manager)
    agent, channel = engineer(manager, create_ops, monkeypatch)
    source = message(manager, "Change things", channel=channel)

    def propose(operations):
        return agent_call(
            manager,
            "engineer",
            "propose-change",
            source_event_id=source,
            operations=operations,
        )

    with pytest.raises(ValueError, match="Only release agents"):
        propose(setup_operations())
    agent["deployments"] = ["portal/production"]
    manager.save_agent(agent)
    with pytest.raises(ValueError, match="outside your deployment scope"):
        propose(
            [
                {
                    "action": "generate_credential",
                    "application": "portal",
                    "environment": "staging",
                    "id": "token",
                }
            ]
        )
    with pytest.raises(ValueError, match="only release configuration"):
        propose([{"action": "create_channel", "id": "x", "name": "X"}])
    broken = setup_operations()[:1]
    broken[0]["spec"]["services"] = []
    with pytest.raises(ValueError):
        propose(broken)
    assert propose(
        [
            {
                "action": "generate_credential",
                "application": "portal",
                "environment": "production",
                "id": "token",
            }
        ]
    )["state"] == ("awaiting_owner_approval")


def test_dependency_attachment_rejects_privileged_containers_and_reattaches(manager):
    d = setup(manager)
    app = d.get("app/portal/production")
    network = d.name(app)
    containers = {
        "db": {"Id": "db-id", "Config": {"Labels": {}}, "HostConfig": {}},
        "manager": {
            "Id": "m",
            "Config": {"Labels": {"io.team-builder.project": "tb-test"}},
        },
        "privileged": {
            "Id": "p",
            "Config": {"Labels": {}},
            "HostConfig": {"Privileged": True},
        },
        "socket": {
            "Id": "s",
            "Config": {"Labels": {}},
            "Mounts": [{"Source": "/var/run/docker.sock"}],
        },
    }
    calls = []

    def call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        if path.startswith("/containers/"):
            return containers.get(path.split("/")[2])
        if method == "GET" and path.startswith("/networks/"):
            return {"Labels": d.labels(app)}
        return {}

    d.docker.call = call
    for name in ("manager", "privileged", "socket"):
        with pytest.raises(ValueError):
            d.apply_change(
                {
                    "action": "attach_dependency",
                    "application": "portal",
                    "environment": "production",
                    "container": name,
                    "alias": "db",
                }
            )
    result = d.apply_change(
        {
            "action": "attach_dependency",
            "application": "portal",
            "environment": "production",
            "container": "db",
            "alias": "prod-db",
        }
    )
    assert result["changed"] is True
    connect = [c for c in calls if c[1].endswith("/connect")]
    assert connect[-1][2] == {
        "Container": "db-id",
        "EndpointConfig": {"Aliases": ["prod-db"]},
    }
    containers["db"]["NetworkSettings"] = {
        "Networks": {network: {"Aliases": ["prod-db"]}}
    }
    calls.clear()
    from team_builder.release_changes import reattach

    reattach(d, app)
    assert not [c for c in calls if c[1].endswith("/connect")]
    del containers["db"]["NetworkSettings"]
    reattach(d, app)
    assert [c for c in calls if c[1].endswith("/connect")]


def test_manager_runs_configured_ci_imports(manager, monkeypatch):
    d = setup(manager)
    op = {
        "action": "configure_release_sync",
        "application": "portal",
        "repository": "tengso/portal",
        "credential": "reader",
        "environments": ["production"],
        "services": ["ui"],
    }
    with pytest.raises(ValueError, match="GitHub credential"):
        d.apply_change(op)
    path = manager.root / "github" / "credentials" / "reader.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    with pytest.raises(ValueError, match="Repository does not match"):
        d.apply_change({**op, "repository": "other/repo"})
    assert d.apply_change(op)["enabled"] is True
    from team_builder import release_sync
    from team_builder.release_changes import import_releases

    seen = {}

    def sync(root, config, **kwargs):
        seen.update(config=config, **kwargs)

    monkeypatch.setattr(release_sync, "sync", sync)
    import_releases(d)
    assert seen["config"].repository == "tengso/portal"
    assert {"request", "load", "inspect"} <= set(seen)
    assert seen["request"](
        None,
        {"action": "profiles", "application": "portal", "environment": "production"},
    )
    monkeypatch.setattr(
        release_sync, "sync", Mock(side_effect=RuntimeError("signed-url-secret"))
    )
    import_releases(d)
    error = json.loads(
        (manager.root / "release-sync" / "portal" / "error.json").read_text()
    )
    assert error["error"] == "RuntimeError" and "signed" not in json.dumps(error)


def test_redacted_logs_follow_environment_diagnostics(manager):
    d = setup(manager)
    d.logs = Mock(return_value={"lines": []})
    d.grant = Mock()
    agent = manager.resource("coa", "agent")
    agent["deployments"] = ["portal/production"]
    manager.save_agent(agent)

    def logs(detail):
        return agent_call(
            manager,
            "coa",
            "logs",
            application="portal",
            environment="production",
            service="ui",
            detail=detail,
        )

    with pytest.raises(ValueError, match="summaries"):
        logs("redacted")
    logs("summary")
    d.db.put(
        "automation-policy/portal",
        "automation_policy",
        {"production_diagnostics": "redacted"},
    )
    logs("redacted")
    assert d.logs.call_args.kwargs["detail"] == "redacted"
    with pytest.raises(ValueError, match="summary or redacted"):
        logs("raw")


def test_release_agent_without_grants_gets_tools_and_runbook(
    manager, create_ops, monkeypatch
):
    engineer(manager, create_ops, monkeypatch)
    manager.apply({"action": "configure_release_agent", "agent": "engineer"})
    managed = manager.root / "agents/engineer/managed"
    skills = json.loads((managed / "skills.json").read_text())
    assert "no application yet" in skills["team-release-runbook"]
    assert "propose_configuration_change" in skills["team-release-runbook"]
    assert (managed / "deployment_mcp.py").exists()
    assert "Release operations" in (managed / "SOUL.md").read_text()
    manager.apply(
        {"action": "configure_release_agent", "agent": "engineer", "enabled": False}
    )
    skills = json.loads((managed / "skills.json").read_text())
    assert "team-release-runbook" not in skills
    assert not (managed / "deployment_mcp.py").exists()
    from team_builder.agent_config import add_catalog

    with pytest.raises(ValueError, match="reserved"):
        add_catalog(
            manager,
            {
                "id": "team-release-runbook",
                "kind": "skill",
                "name": "x",
                "content": "y",
            },
        )


def test_runbook_example_is_a_valid_proposal():
    from team_builder.agent_deployments import EXAMPLE_OPERATIONS, runbook
    from team_builder.release_changes import validate_operations

    assert len(validate_operations(EXAMPLE_OPERATIONS)) == len(EXAMPLE_OPERATIONS)
    text = runbook(["my-app/staging"])
    block = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(block) == EXAMPLE_OPERATIONS


def test_malformed_proposals_get_actionable_value_free_errors():
    from team_builder.release_changes import validate_operations

    # Shapes a real agent submitted before the runbook carried an example.
    with pytest.raises(ValueError, match=r"operations\[0\] needs an 'action' key"):
        validate_operations([{"op": "register_application", "spec": {}}])
    bad = spec("staging")
    bad["services"][0]["command"] = "api-secret-marker"
    with pytest.raises(ValueError) as caught:
        validate_operations([{"action": "register_application", "spec": bad}])
    message = str(caught.value)
    assert "operations[0] register_application" in message
    assert "services.0.command" in message and "runbook" in message
    assert "api-secret-marker" not in message
    with pytest.raises(ValueError, match="JSON list"):
        validate_operations({"action": "register_application"})


def test_agent_errors_are_actionable_but_unexpected_failures_stay_opaque():
    from pydantic import TypeAdapter, ValidationError

    from team_builder.deployment_api import GENERIC_ERROR, agent_error

    assert agent_error(ValueError("Agent has no access to this deployment")) == (
        "Agent has no access to this deployment"
    )
    assert agent_error(RuntimeError("docker socket /var/run/x")) == GENERIC_ERROR
    assert agent_error(KeyError("secret")) == GENERIC_ERROR
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(int).validate_python("hidden-value")
    summary = agent_error(caught.value)
    assert summary.startswith("Invalid request:") and "hidden-value" not in summary


def test_release_agents_wake_for_untagged_owner_approvals(
    manager, create_ops, monkeypatch
):
    engineer(manager, create_ops, monkeypatch)
    rules = manager.root / "agents/engineer/managed/rules.toml"
    assert not rules.exists()
    manager.apply({"action": "configure_release_agent", "agent": "engineer"})
    import tomllib

    parsed = tomllib.loads(rules.read_text())["rules"]
    assert [r["name"] for r in parsed] == ["mentions", "owner-approvals"]
    approvals = parsed[1]
    assert approvals["require_mention"] is False and approvals["kinds"] == [9]
    assert f'author == "{manager.config["owner"]}"' in approvals["filter"]
    assert 'content == "approve"' in approvals["filter"]
    manager.apply(
        {"action": "configure_release_agent", "agent": "engineer", "enabled": False}
    )
    assert not rules.exists()


def test_plan_based_calls_follow_the_plan_environment(manager, create_ops, monkeypatch):
    import copy

    from team_builder.deployment_api import inferred_environment

    d = setup(manager)
    staging = copy.deepcopy(d.get("app/portal/production")["spec"])
    staging["environment"] = "staging"
    d.register(staging)
    release = d.get("release/portal/production/r1")
    d.release({**release, "environment": "staging"})
    plan = d.plan("portal", "staging", release="r1")
    assert inferred_environment(manager, "propose", {"plan_id": plan["plan_id"]}) == (
        "staging"
    )
    assert inferred_environment(manager, "execute", {"plan_id": "unknown"}) == (
        "production"
    )
    agent, channel = engineer(manager, create_ops, monkeypatch)
    agent["deployments"] = ["portal/production", "portal/staging"]
    manager.save_agent(agent)
    # An explicit wrong environment names the right one instead of a bare refusal.
    with pytest.raises(ValueError, match='environment="staging"'):
        agent_call(
            manager,
            "engineer",
            "propose",
            application="portal",
            environment="production",
            plan_id=plan["plan_id"],
            source_event_id="unused",
        )
    # Omitted, the staging plan is authorized against the staging grant.
    agent["deployments"] = ["portal/staging"]
    manager.save_agent(agent)
    proposal = agent_call(
        manager,
        "engineer",
        "propose",
        application="portal",
        plan_id=plan["plan_id"],
        source_event_id=message(manager, "Deploy UAT", channel=channel),
    )
    assert proposal["state"] == "awaiting_owner_approval"


def test_ci_import_progress_is_visible_to_agents_and_dashboard(manager):
    import time

    from team_builder.storage import private_write

    d = setup(manager)
    agent = manager.resource("coa", "agent")
    agent["deployments"] = ["portal/production"]
    manager.save_agent(agent)
    directory = manager.root / "release-sync" / "portal"
    directory.mkdir(parents=True)
    now = time.time()
    importing = {"release": "ci-9-1", "commit": "a" * 40, "started_at": now - 60}
    private_write(
        directory / "status.json", {"checked_at": now - 200, "importing": importing}
    )
    status = d.release_sync_status("portal")
    assert status["state"] == "importing" and status["importing"]["release"] == "ci-9-1"
    result = agent_call(
        manager, "coa", "releases", application="portal", environment="production"
    )
    assert result["ci_import"]["state"] == "importing"
    assert "few minutes" in result["note"] and result["releases"]
    # A crashed import stops being reported as running.
    importing["started_at"] = now - 7200
    private_write(
        directory / "status.json", {"checked_at": now - 7200, "importing": importing}
    )
    assert d.release_sync_status("portal")["state"] == "stale"
    assert d.release_sync_status("portal")["importing"] is None
    private_write(directory / "status.json", {"checked_at": now - 30})
    assert d.release_sync_status("portal")["state"] == "current"


def test_stale_policy_proposals_are_rejected_without_side_effects(
    manager, create_ops, monkeypatch
):
    _, channel = engineer(manager, create_ops, monkeypatch, release_agent=True)
    d = manager.deployments
    profile = {
        "action": "register_profile",
        "application": "ledger",
        "environment": "production",
        "profile": {"id": "standard-v1", "values": {"DB_HOST": "ledger-db"}},
    }
    policy = {
        "application": "ledger",
        "staging_agent": "engineer",
        "production_agent": "engineer",
        "staging_profile": "standard-v1",
        "production_profile": "standard-v1",
        "checks": [{"id": "acceptance", "service": "api", "command": ["true"]}],
    }

    def propose(operations):
        source = message(manager, "Change the release policy", channel=channel)
        return agent_call(
            manager,
            "engineer",
            "propose-change",
            source_event_id=source,
            operations=operations,
        )["message_id"]

    def approve(proposal):
        reply = message(manager, "approve", channel=channel, reply=proposal)
        return agent_call(
            manager, "engineer", "approve-change", approval_event_id=reply
        )

    # First policy: agents must say they expect none to exist yet.
    first = {"action": "configure_release_policy", "policy": policy}
    with pytest.raises(ValueError, match="expected_version"):
        propose([*setup_operations(), profile, first])  # no expected_version
    production_access = {
        "action": "configure_deployment_access",
        "agent": "engineer",
        "application": "ledger",
        "environment": "production",
    }
    setup = [*setup_operations(), production_access, profile]
    assert approve(propose([*setup, {**first, "expected_version": ""}]))["state"] == (
        "complete"
    )
    v1 = d.db.get("automation-policy/ledger")["version"]

    # Two proposals written against the same live version.
    a = propose(
        [
            {
                "action": "configure_release_policy",
                "expected_version": v1,
                "policy": {**policy, "staging_diagnostics": "summary"},
            }
        ]
    )
    b = propose(
        [
            {**profile, "profile": {"id": "standard-v2", "values": {"DB_HOST": "x"}}},
            {
                "action": "configure_release_policy",
                "expected_version": v1,
                "policy": {**policy, "production_diagnostics": "summary"},
            },
        ]
    )
    assert approve(a)["state"] == "complete"
    live = d.db.get("automation-policy/ledger")
    assert live["version"] != v1 and live["staging_diagnostics"] == "summary"

    # B would silently undo A's change: refused, and its profile is not registered.
    with pytest.raises(ValueError, match="Nothing was changed") as caught:
        approve(b)
    assert live["version"] in str(caught.value)
    assert d.db.get("automation-policy/ledger") == live
    assert not d.db.get("profile/ledger/production/standard-v2")

    # Proposing against an outdated version fails fast, too.
    with pytest.raises(ValueError, match="Re-read the policy"):
        propose(
            [
                {
                    "action": "configure_release_policy",
                    "expected_version": v1,
                    "policy": policy,
                }
            ]
        )
