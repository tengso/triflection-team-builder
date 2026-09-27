import importlib.util
import inspect
import json
from pathlib import Path

import httpx
import pytest

from team_builder import agent_deployments
from team_builder.agent_deployments import REQUIRED, TOOLS, main, skill


@pytest.fixture
def mcp_module(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_TOKEN", "t")
    monkeypatch.setenv("DEPLOYMENT_AGENT", "a")
    path = Path(agent_deployments.__file__).parent / "resources/deployment_mcp.py"
    spec = importlib.util.spec_from_file_location("deployment_mcp_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_matches_mcp_tools(mcp_module):
    functions = {
        name
        for name, value in vars(mcp_module).items()
        if inspect.isfunction(value) and value.__module__ == mcp_module.__name__
    } - {"call"}
    assert functions == set(TOOLS)
    for tool, (_, params, _, _) in TOOLS.items():
        signature = inspect.signature(getattr(mcp_module, tool)).parameters
        expected = [
            (name, inspect.Parameter.empty if default is REQUIRED else default)
            for name, default in params
        ]
        assert [(p.name, p.default) for p in signature.values()] == expected, tool


def test_cli_mirrors_mcp_wire_requests(mcp_module, monkeypatch, tmp_path, capsys):
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    monkeypatch.setenv("DEPLOYMENT_TOKEN_FILE", str(token))
    seen = []

    def handler(request):
        seen.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"plan_id": "p1"})

    argv = [
        "plan",
        "--application",
        "portal",
        "--operation",
        "rollback",
        "--environment",
        "staging",
    ]
    assert main(argv, transport=httpx.MockTransport(handler)) == 0
    assert json.loads(capsys.readouterr().out) == {"plan_id": "p1"}
    assert seen[0][0] == "Bearer secret-token"
    mcp_bodies = []
    monkeypatch.setattr(
        mcp_module.client,
        "post",
        lambda path, json: (
            mcp_bodies.append(json)
            or httpx.Response(200, json={}, request=httpx.Request("POST", "http://x"))
        ),
    )
    mcp_module.plan_deployment("portal", operation="rollback", environment="staging")
    assert seen[0][1] == mcp_bodies[0]


def test_cli_reports_rejections_without_output(monkeypatch, tmp_path, capsys):
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setenv("DEPLOYMENT_TOKEN_FILE", str(token))
    monkeypatch.setenv("DEPLOYMENT_AGENT", "a")
    reject = httpx.MockTransport(
        lambda request: httpx.Response(
            400, json={"error": "Plan outside assigned scope"}
        )
    )
    argv = ["approve", "--application", "portal", "--approval-event-id", "e" * 64]
    assert main(argv, transport=reject) == 1
    out = capsys.readouterr()
    assert out.out == "" and "Plan outside assigned scope" in out.err

    def unreachable(request):
        raise httpx.ConnectError("down")

    assert main(argv, transport=httpx.MockTransport(unreachable)) == 2
    assert "unreachable" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["propose", "--application", "portal"])


def test_skill_lists_every_command_and_assignment():
    text = skill(["portal/staging"])
    assert text.startswith("---\nname: team-managed-team-deployments\n")
    assert "portal/staging" in text
    for command in agent_deployments.COMMANDS.values():
        assert f"`{command} " in text
    assert "--source-event-id" in text and "--approval-event-id" in text


def test_bundled_cli_is_self_contained():
    source = Path(agent_deployments.__file__).read_text()
    assert "team_builder" not in source.split('"""', 2)[2]
