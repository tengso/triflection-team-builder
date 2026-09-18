import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from team_builder import cli
from team_builder.proxy import command, environment, validate_url
from team_builder.runtime import write_agent_files
from team_builder.storage import private_write
from team_builder.worker_config import verify_bundle


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@proxy:3128",
        "https://proxy:3128",
        "http://proxy:0",
        "http://proxy:65536",
        "http://proxy/path",
        "http://proxy?password=secret",
        "http://proxy#fragment",
        "http://127.0.0.1:3128",
        "http://localhost:3128",
        "http://[::1]:3128",
        "http://0.0.0.0:3128",
        "http://bad host:3128",
    ],
)
def test_reject_invalid_or_container_local_proxy(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_managed_proxy_bundle_and_removal(manager, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-shell:3128")
    agent = manager.resource("coa", "agent")
    config = dict(manager.config, internal_url="http://relay:3000")
    assert environment(manager.root, config) == {}
    private_write(manager.root / "agent-proxy.json", {"url": "http://192.0.2.10:3128"})
    _, env, _ = write_agent_files(manager.root, config, manager.secrets, agent)
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://192.0.2.10:3128"
    assert env["https_proxy"] == env["HTTPS_PROXY"]
    assert {"relay", "manager", "test", "localhost", "127.0.0.1"} <= set(
        env["NO_PROXY"].split(",")
    )
    managed = manager.root / "agents/coa/managed"
    verify_bundle(managed)
    assert (
        json.loads((managed / "env.json").read_text())["HTTPS_PROXY"]
        == env["HTTPS_PROXY"]
    )
    private_write(manager.root / "agent-proxy.json", {"url": None})
    _, env, _ = write_agent_files(manager.root, config, manager.secrets, agent)
    assert not any(k.lower().endswith("proxy") for k in env)
    verify_bundle(managed)


def test_cli_persistence_retry_and_old_runtime_guard(manager, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    run = Mock(side_effect=RuntimeError("old manager"))
    monkeypatch.setattr(cli, "run", run)
    args = SimpleNamespace(
        state_dir=str(manager.root), proxy_command="set", url="http://proxy:3128"
    )
    with pytest.raises(RuntimeError):
        command(args)
    assert not (manager.root / "agent-proxy.json").exists()
    run.side_effect = [None, RuntimeError("restart failed")]
    with pytest.raises(RuntimeError):
        command(args)
    assert environment(manager.root, manager.config)["HTTPS_PROXY"] == args.url
    run.side_effect = None
    command(args)
    assert run.call_args.args[0][-1] == "manager"
    args.proxy_command = "disable"
    command(args)
    assert environment(manager.root, manager.config) == {}


def test_cli_proxy_parser():
    args = cli.parser().parse_args(["proxy", "set", "--url", "http://proxy:3128"])
    assert args.proxy_command == "set"
    assert args.url == "http://proxy:3128"
