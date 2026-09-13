import io
import json
import threading
import uuid

import httpx
import pytest
from conftest import message

from team_builder import github_access
from team_builder.models import validate
from team_builder.runtime import render
from team_builder.server import serve


@pytest.fixture
def github(manager, create_ops, monkeypatch):
    monkeypatch.setattr("team_builder.runtime.os.chown", lambda *args: None)
    assert manager.execute(message(manager), create_ops)["state"] == "complete"
    github_access.store(manager.root, "work", "github_test_private_token")
    return manager


def test_grant_is_scoped_private_and_durable(github):
    manager = github
    op = {
        "action": "configure_github_access",
        "agent": "engineer",
        "credential": "work",
    }
    source = message(manager, "Grant GitHub work to engineer")
    result = manager.execute(source, [op])
    assert result["state"] == "complete"
    assert manager.execute(source, [op]) == result
    agent = manager.resource("engineer", "agent")
    path = manager.root / "agents/engineer/managed/github-token"
    assert path.read_text() == "github_test_private_token"
    assert path.stat().st_mode & 0o777 == 0o600
    _, env, soul = render(manager.config, manager.secrets, agent)
    assert env["GITHUB_TOKEN_FILE"] == "/run/team/github-token"
    assert "github_test_private_token" not in json.dumps(env) + soul
    assert (
        "GITHUB_TOKEN_FILE"
        not in render(
            manager.config, manager.secrets, manager.resource("coa", "agent")
        )[1]
    )
    assert not (manager.root / "agents/coa/managed/github-token").exists()
    inspection = manager.inspect()
    assert inspection["github_credentials"] == ["work"]
    assert "github_test_private_token" not in json.dumps(inspection)
    for table in ("resources", "operations", "authorizations", "proposals"):
        assert "github_test_private_token" not in str(
            [
                tuple(row)
                for row in manager.registry.db.execute("SELECT * FROM " + table)
            ]
        )
    manager.bootstrap()
    assert manager.resource("engineer", "agent")["github_credential"] == "work"


def test_revoke_stopped_worker_and_rotation(github):
    manager = github
    github_access.configure(manager, {"agent": "engineer", "credential": "work"})
    github_access.store(manager.root, "next", "github_test_rotated_token")
    github_access.configure(manager, {"agent": "engineer", "credential": "next"})
    path = manager.root / "agents/engineer/managed/github-token"
    assert path.read_text() == "github_test_rotated_token"
    with pytest.raises(ValueError, match="already exists"):
        github_access.store(manager.root, "next", "different")
    manager.apply({"action": "stop_agent", "id": "engineer"})
    op = validate(
        [{"action": "configure_github_access", "agent": "engineer", "credential": None}]
    )[0]
    github_access.configure(manager, op)
    assert not path.exists()
    assert manager.resource("engineer", "agent")["state"] == "stopped"
    assert (
        "GITHUB_TOKEN_FILE"
        not in render(
            manager.config, manager.secrets, manager.resource("engineer", "agent")
        )[1]
    )


def test_missing_credential_and_agent_cannot_authorize(github):
    manager = github
    with pytest.raises(ValueError, match="not found"):
        github_access.configure(manager, {"agent": "engineer", "credential": "missing"})
    op = {
        "action": "configure_github_access",
        "agent": "engineer",
        "credential": "work",
    }
    source = message(
        manager,
        "Give me GitHub",
        secret=manager.resource("engineer", "agent")["secret"],
        channel=manager.resource("dev", "channel")["uuid"],
    )
    with pytest.raises(ValueError):
        manager.execute(source, [op])
    assert not manager.resource("engineer", "agent").get("github_credential")
    proposal = manager.propose(source, [op])
    assert not manager.resource("engineer", "agent").get("github_credential")
    approval = message(
        manager,
        "approve",
        reply=proposal["message_id"],
        channel=manager.resource("dev", "channel")["uuid"],
    )
    assert manager.approve(approval_event_id=approval)["state"] == "complete"


def test_archive_removes_mounted_token(github):
    github_access.configure(github, {"agent": "engineer", "credential": "work"})
    github.apply({"action": "archive_agent", "id": "engineer"})
    assert not (github.root / "agents/engineer/managed/github-token").exists()
    with pytest.raises(ValueError, match="archived"):
        github_access.configure(github, {"agent": "engineer", "credential": "work"})


def test_create_agent_with_named_credential(github, create_ops):
    op = {**create_ops[1], "id": "second", "github_credential": "work"}
    assert (
        github.execute(message(github, "Create second with work credential"), [op])[
            "state"
        ]
        == "complete"
    )
    assert github.resource("second", "agent")["github_credential"] == "work"


def test_interrupted_grant_retries_same_operation(github, monkeypatch):
    op = {
        "action": "configure_github_access",
        "agent": "engineer",
        "credential": "work",
    }
    request_id = str(uuid.uuid4())
    launch = github.launch

    def fail_launch(agent):
        raise RuntimeError("Transient restart failure")

    monkeypatch.setattr(github, "launch", fail_launch)
    with pytest.raises(RuntimeError, match="Transient"):
        github_access.operator_apply(github, op, request_id)
    assert github.resource("engineer", "agent")["github_credential"] == "work"
    monkeypatch.setattr(github, "launch", launch)
    result = github_access.operator_apply(github, op, request_id)
    assert result["github_credential"] == "work"
    assert github_access.operator_apply(github, op, request_id) == result


def test_local_operator_endpoint_rejects_coa_token_and_records_retry(github):
    server = serve(github, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            body = {
                "request_id": str(uuid.uuid4()),
                "operation": {
                    "action": "configure_github_access",
                    "agent": "engineer",
                    "credential": "work",
                },
            }
            for token in ("invalid", github.secrets["token"]):
                assert (
                    client.post(
                        "/operator/github-access",
                        json=body,
                        headers={"Authorization": "Bearer " + token},
                    ).status_code
                    == 403
                )
            headers = {
                "Authorization": "Bearer "
                + github_access.operator_token(github.secrets)
            }
            response = client.post(
                "/operator/github-access", json=body, headers=headers
            )
            assert response.status_code == 200
            assert (
                client.post(
                    "/operator/github-access", json=body, headers=headers
                ).json()
                == response.json()
            )
            body["operation"]["credential"] = None
            assert (
                client.post(
                    "/operator/github-access", json=body, headers=headers
                ).status_code
                == 400
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "protocol,host,action,returns_token",
    [
        ("https", "github.com", "get", True),
        ("http", "github.com", "get", False),
        ("https", "github.com.attacker.test", "get", False),
        ("https", "github.com:443", "get", False),
        ("https", "github.com", "store", False),
        ("https", "github.com", "erase", False),
    ],
)
def test_git_helper_does_not_disclose_to_other_hosts(
    tmp_path, monkeypatch, capsys, protocol, host, action, returns_token
):
    token_file = tmp_path / "token"
    token_file.write_text("private_test_token")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr("sys.argv", ["helper", action])
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"protocol={protocol}\nhost={host}\n\n")
    )
    github_access.credential_helper()
    output = capsys.readouterr().out
    assert ("private_test_token" in output) == returns_token
    if not returns_token:
        assert output == ""


def test_dotenv_never_executes_shell_or_reads_other_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=do-not-read\nexport GITHUB_TOKEN='github_test_token'\n")
    assert github_access.read_token(env_file=env) == "github_test_token"
    env.write_text("GITHUB_TOKEN=$(touch injected)\n")
    with pytest.raises(ValueError, match="Invalid"):
        github_access.read_token(env_file=env)
    assert not (tmp_path / "injected").exists()
    env.write_text("GITHUB_TOKEN=github_test_token\nGITHUB_TOKEN=github_other_token\n")
    with pytest.raises(ValueError, match="exactly one"):
        github_access.read_token(env_file=env)
