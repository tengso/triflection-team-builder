import subprocess
import sys

import pytest

from team_builder import deployment_cli


def completed(code, stdout=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="noise")


def test_operator_request_reports_the_managers_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: completed(
            3, '{"error": "Credential exists; explicitly rotate it"}'
        ),
    )
    with pytest.raises(ValueError, match="explicitly rotate it"):
        deployment_cli.operator_request(tmp_path, {"action": "credential"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed(1))
    with pytest.raises(RuntimeError, match="Could not reach the manager"):
        deployment_cli.operator_request(tmp_path, {"action": "credential"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed(0, '{"id": "x"}'))
    assert deployment_cli.operator_request(tmp_path, {}) == {"id": "x"}


def test_deployment_command_prints_error_without_traceback(
    monkeypatch, tmp_path, capsys
):
    from team_builder import cli

    request = tmp_path / "request.json"
    request.write_text('{"action": "credential"}')
    secret = tmp_path / "secret"
    secret.write_text("private-value")

    def refuse(root, body):
        assert body["value"] == "private-value"
        raise ValueError("Credential exists; explicitly rotate it")

    monkeypatch.setattr(deployment_cli, "operator_request", refuse)
    monkeypatch.setattr(
        sys,
        "argv",
        ["team-builder", "deployment", str(request), "--secret-file", str(secret)],
    )
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == "Error: Credential exists; explicitly rotate it"
    assert "private-value" not in err and "Traceback" not in err
