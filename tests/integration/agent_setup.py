"""Run inside the manager of a disposable Image Smoke Test installation only.

Exercises agent-led release setup without model calls: a release agent proposes
an application through the real relay, the owner approves, the manager executes,
a database-like container is attached, preflight reaches it, and exec output is
captured from a managed container.
"""

import json
import time
from pathlib import Path

from team_builder.cli import INFRA_IMAGES
from team_builder.deployment_api import handle
from team_builder.deployments import token
from team_builder.manager import Manager
from team_builder.nostr import reply_tags, sign


def exercise(owner_secret):
    m = Manager("/state")
    assert m.config["name"] == "Image Smoke Test"
    owner = m.actor(owner_secret)
    counter = 0

    def execute(ops):
        nonlocal counter
        counter += 1
        source = owner.publish(
            sign(
                owner_secret,
                9,
                [["h", m.config["office"]]],
                f"Setup {counter}: {json.dumps(ops)}",
            )
        )
        result = m.execute(source["id"], ops)
        assert result["state"] == "complete", result
        return result

    def ready(agent_id, timeout=600):
        name = m.name(m.resource(agent_id, "agent"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if m.docker.ready(name):
                return
            time.sleep(5)
        raise AssertionError(f"{agent_id} never became healthy")

    execute(
        [
            {"action": "create_channel", "id": "release-lab", "name": "Release Lab"},
            {
                "action": "create_agent",
                "id": "cody",
                "name": "Cody",
                "instructions": "Release agent for integration tests.",
                "channels": ["release-lab"],
            },
        ]
    )
    ready("cody")
    execute([{"action": "configure_release_agent", "agent": "cody"}])
    ready("cody")
    agent = m.resource("cody", "agent")
    managed = Path("/state/agents/cody/managed")
    assert (managed / "deployment_mcp.py").exists()
    assert "team-release-runbook" in json.loads((managed / "skills.json").read_text())
    channel = m.resource("release-lab", "channel")["uuid"]

    # A database stand-in the operator would normally attach by hand.
    database = "tb-agent-setup-db"
    docker = m.deployments.docker
    docker.call("DELETE", "/containers/" + database + "?force=true")
    docker.call(
        "POST",
        "/containers/create?name=" + database,
        # A real third-party image: Team Builder-built images are refused.
        json={"Image": INFRA_IMAGES["redis"]},
    )
    docker.call("POST", "/containers/" + database + "/start")
    try:
        spec = {
            "id": "ledger",
            "repository": "test/ledger",
            "services": [
                {
                    "id": "web",
                    "command": ["python", "-m", "http.server", "8000"],
                    "port": 8000,
                    "health_path": "/",
                }
            ],
        }
        operations = [
            {
                "action": "register_application",
                "spec": {**spec, "environment": "staging"},
            },
            {
                "action": "register_application",
                "spec": {**spec, "environment": "production"},
            },
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
                    "values": {"DATABASE_HOST": "ledger-db"},
                    "secrets": {"API_TOKEN": "api-token"},
                    "required_env": ["API_TOKEN"],
                    "connections": [
                        {"id": "database", "host": "ledger-db", "port": 6379}
                    ],
                },
            },
            {
                "action": "attach_dependency",
                "application": "ledger",
                "environment": "staging",
                "container": database,
                "alias": "ledger-db",
            },
            {
                "action": "configure_deployment_access",
                "agent": "cody",
                "application": "ledger",
                "environment": "staging",
            },
        ]

        def request(action, **body):
            return handle(
                m,
                "/deployments",
                "Bearer " + token(m.secrets, "cody"),
                {"agent": "cody", "action": action, **body},
            )

        source = owner.publish(
            sign(owner_secret, 9, [["h", channel]], "Set up the ledger application.")
        )
        proposal = request(
            "propose-change", source_event_id=source["id"], operations=operations
        )
        published = m.buzz.message(proposal["message_id"])
        assert published["pubkey"] == agent["pubkey"], "proposal must be agent-signed"
        assert published["content"].startswith("Proposed release configuration changes")
        assert not m.deployments.db.get("app/ledger/staging"), "nothing before approval"
        approval = owner.publish(
            sign(
                owner_secret,
                9,
                [["h", channel], *reply_tags(published), ["p", agent["pubkey"]]],
                "approve",
            )
        )
        result = request("approve-change", approval_event_id=approval["id"])
        assert result["state"] == "complete", result
        ready("cody")
        assert "ledger/staging" in m.resource("cody", "agent")["deployments"]
        preflight = m.deployments.profiles.preflight("ledger", "staging", "standard-v1")
        assert preflight["ready"], preflight
        # Recreating the dependency's network link is restored by preflight.
        network = m.deployments.name(m.deployments.get("app/ledger/staging"))
        docker.call(
            "POST",
            "/networks/" + network + "/disconnect",
            json={"Container": database, "Force": True},
        )
        preflight = m.deployments.profiles.preflight("ledger", "staging", "standard-v1")
        assert preflight["ready"], "attachment was not restored before preflight"
        code, output = docker.exec_output(
            m.name(m.resource("cody", "agent")),
            ["python3", "-c", "print('captured'); raise SystemExit(3)"],
        )
        assert (code, output.strip()) == (3, "captured"), (code, output)
        print(
            "PASS: release agent proposal through the relay, owner approval, "
            "generated credential, dependency attachment and restore, exec output capture",
            flush=True,
        )
    finally:
        docker.call("DELETE", "/containers/" + database + "?force=true")
