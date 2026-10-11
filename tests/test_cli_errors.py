import json
import subprocess
import sys

import pytest
from pydantic import BaseModel

from team_builder import cli
from team_builder.cli import describe, secret_file


def run_init(monkeypatch, tmp_path, *extra):
    def no_prompt(*args, **kwargs):
        raise AssertionError("prompted before the input files were checked")

    monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
    monkeypatch.setattr("builtins.input", no_prompt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "team-builder",
            "init",
            "--state-dir",
            str(tmp_path / "state"),
            "--name",
            "HTI Research",
            "--advertised-url",
            "http://127.0.0.1:3100",
            "--internal-url",
            "http://relay:3000",
            "--bind",
            "127.0.0.1",
            "--port",
            "3100",
            "--provider",
            "custom",
            "--base-url",
            "https://dashscope.example/v1",
            "--model",
            "kimi-k3",
            *extra,
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return exited.value.code


def test_missing_provider_key_file_is_named_before_any_prompt(
    monkeypatch, tmp_path, capsys
):
    missing = tmp_path / "secrets" / "dashscope.key"
    assert run_init(monkeypatch, tmp_path, "--provider-key-file", str(missing)) == 1
    err = capsys.readouterr().err
    assert f"Error: Provider key file not found: {missing}" in err
    assert "rerun the same init command" in err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda p: p.write_text("  \n"), "file is empty"),
        (lambda p: p.mkdir(), "file is a directory"),
        (lambda p: p.write_bytes(b"\xff\xfe\x00"), "not a text file"),
    ],
)
def test_unusable_key_files(tmp_path, setup, expected):
    path = tmp_path / "key"
    setup(path)
    with pytest.raises(ValueError, match=expected):
        secret_file(path, "Provider key")


def test_key_file_expands_home_and_strips(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "k").write_text("sk-123\n")
    assert secret_file("~/k", "Provider key") == "sk-123"
    assert secret_file(None, "Provider key") is None


class Settings(BaseModel):
    port: int


def test_describe_is_specific_without_echoing_values():
    assert describe(ValueError("plain")) == "plain"
    assert describe(FileNotFoundError(2, "No such file", "/x/y")) == (
        "File not found: /x/y"
    )
    assert "install Docker" in describe(FileNotFoundError(2, "No such file", "docker"))
    assert "docker group" in describe(
        PermissionError(13, "Permission denied", "/var/run/docker.sock")
    )
    assert describe(IsADirectoryError(21, "Is a directory", "/tmp")) == (
        "Is a directory: /tmp"
    )
    timeout = describe(
        subprocess.TimeoutExpired(["docker", "compose", "up", "--secret"], 420)
    )
    assert "`docker compose up` did not finish within 420s" in timeout
    assert "--secret" not in timeout
    try:
        json.loads('{"a": ')
    except json.JSONDecodeError as exc:
        assert describe(exc).startswith("Invalid JSON: Expecting value (line 1")
    try:
        Settings(port="sk-secret-value")
    except ValueError as exc:
        message = describe(exc)
    assert message.startswith("Invalid settings: port:")
    assert "sk-secret-value" not in message

    class Odd(ValueError):
        pass

    hidden = describe(Odd("sk-secret-value"))
    assert "sk-secret-value" not in hidden and "TEAM_BUILDER_DEBUG=1" in hidden


def test_debug_prints_the_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TEAM_BUILDER_DEBUG", "1")
    run_init(monkeypatch, tmp_path, "--provider-key-file", str(tmp_path / "none"))
    assert "Traceback" in capsys.readouterr().err
