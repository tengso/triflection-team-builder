"""Golden test: Hermes rendering, managed bundle and container spec.

The snapshot was deliberately regenerated for the buzz-acp migration: the
container spec and SOUL.md are byte-identical to the pre-migration render;
config.yaml drops only the gateway/display blocks and env drops
BUZZ_ALLOW_ALL_USERS in favour of the BUZZ_ACP_* / HERMES_ACP_SKIP_CONFIGURED_MCP
variables, and harness.json joins the managed bundle."""

import hashlib
import json
from unittest.mock import Mock

import yaml

from team_builder.runtime import _write_agent_files, render, start_agent

EXPECTED = json.loads(r"""
{"managed": {"SOUL.md": "8e847ce2a10ccac5156eeb62e8357986bdfc19c5947187a42ea6a7ce7c14a17b", "config.yaml": "c026058c42c2515ec880d6639d28e843a1ef7d4f60b1282d344bd39bf2901e61", "env.json": "a6c41baa5a25161d49f11480786eed5f13715d4c97d472b4fceec3a2e675d08b", "harness.json": "9fbfe5f1ae47412075f04453bfea5180c41635c22e20518a203fac283e68b510", "manifest.json": "22130079c0c2738db3b938318bce4f1391df9288a512f1e2490a195baf99d90f", "revision.json": "76555e7277c45c78047b8351081c3bdee9d6ca07043997134227c0df3d9d18d4", "skills.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"}, "managed_config": {"agent": {"max_turns": 30}, "mcp_servers": {}, "model": {"default": "test-model", "provider": "openrouter"}, "platform_toolsets": {"acp": ["terminal", "file", "skills", "memory", "todo", "session_search", "no_mcp"]}, "security": {"redact_secrets": true}, "terminal": {"backend": "local", "cwd": "/work"}}, "managed_env": {"TEAM_BUILDER_OWNER": "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo", "BUZZ_PRIVATE_KEY": "ssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss", "BUZZ_AUTH_TAG": "[\"auth\",\"oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo\",\"\",\"sig\"]", "BUZZ_RELAY_URL": "http://test:3100", "BUZZ_ACP_RESPOND_TO": "anyone", "BUZZ_ACP_PERMISSION_MODE": "bypass-permissions", "BUZZ_ACP_SESSION_POLICY": "channel", "OPENROUTER_API_KEY": "model-secret", "HERMES_ACP_SKIP_CONFIGURED_MCP": "0"}, "managed_manifest": {"config.yaml": "c026058c42c2515ec880d6639d28e843a1ef7d4f60b1282d344bd39bf2901e61", "env.json": "a6c41baa5a25161d49f11480786eed5f13715d4c97d472b4fceec3a2e675d08b", "SOUL.md": "8e847ce2a10ccac5156eeb62e8357986bdfc19c5947187a42ea6a7ce7c14a17b", "skills.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "revision.json": "76555e7277c45c78047b8351081c3bdee9d6ca07043997134227c0df3d9d18d4", "harness.json": "9fbfe5f1ae47412075f04453bfea5180c41635c22e20518a203fac283e68b510"}, "render": {"document": {"mcp_servers": {}, "model": {"provider": "openrouter", "default": "test-model"}, "agent": {"max_turns": 30}, "terminal": {"backend": "local", "cwd": "/work"}, "security": {"redact_secrets": true}, "platform_toolsets": {"acp": ["terminal", "file", "skills", "memory", "todo", "session_search", "no_mcp"]}}, "harness_document": {"harness": "hermes", "agent_id": "engineer", "agent_name": "Engineer", "model": "test-model", "provider": "openrouter", "base_url": null, "key_env": "OPENROUTER_API_KEY", "relay_url": "http://test:3100", "channels": ["chan-1"], "owner": "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo", "mcp_servers": {}}, "env": {"TEAM_BUILDER_OWNER": "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo", "BUZZ_PRIVATE_KEY": "ssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss", "BUZZ_AUTH_TAG": "[\"auth\",\"oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo\",\"\",\"sig\"]", "BUZZ_RELAY_URL": "http://test:3100", "BUZZ_ACP_RESPOND_TO": "anyone", "BUZZ_ACP_PERMISSION_MODE": "bypass-permissions", "BUZZ_ACP_SESSION_POLICY": "channel", "OPENROUTER_API_KEY": "model-secret", "HERMES_ACP_SKIP_CONFIGURED_MCP": "0"}, "soul": "You are Engineer, an agent in a Buzz community.\n\nDo work\n\nWork in your assigned channels and reply in the request thread. Team changes must be proposed to Chief of Agents and approved by the human owner.\n\nPersonality and communication:\nBe kind\n\nFor persistent app servers, launch detached from the terminal with stdin from /dev/null and stdout/stderr redirected to a log in /work (for example: nohup command > /work/app.log 2>&1 < /dev/null &). Gateway-only restart preserves detached processes, but attached pipes/PTYs and active turns may be interrupted. Check existing listeners before starting another instance. Container restart, stop, or upgrade still stops all processes.\n\nDelivery: your final assistant text is NOT shown to anyone. Every reply must be posted with the buzz CLI, replying to the triggering message: printf '%s\\n' \"<reply>\" | buzz messages send --channel <channel uuid> --reply-to <Event ID from the buzz-event block> --content -. Post exactly one reply per request unless asked for more; if a task fails, post the failure the same way. Tool calls are auto-approved inside your isolated container, so act carefully and never run destructive commands without an explicit owner instruction."}, "spec": {"Entrypoint": ["/opt/hermes/.venv/bin/team-builder-worker"], "Env": ["HOME=/home/hermes", "HERMES_HOME=/home/hermes/.hermes", "PYTHONDONTWRITEBYTECODE=1"], "Healthcheck": {"Interval": 5000000000, "Retries": 30, "Test": ["CMD", "/opt/hermes/.venv/bin/python", "-m", "team_builder.worker", "health"], "Timeout": 3000000000}, "HostConfig": {"Binds": ["{ROOT}/agents/engineer/managed:/run/team:ro", "{ROOT}/agents/engineer/home:/home/hermes", "{ROOT}/agents/engineer/work:/work"], "CapDrop": ["ALL"], "ExtraHosts": ["test:host-gateway"], "Init": true, "Memory": 2147483648, "NetworkMode": "tb-test_community", "PidsLimit": 256, "ReadonlyRootfs": true, "RestartPolicy": {"Name": "unless-stopped"}, "SecurityOpt": ["no-new-privileges:true"], "Tmpfs": {"/tmp": "rw,nosuid,size=512m"}}, "Image": "sha256:test", "User": "10000:10000", "WorkingDir": "/work"}}
""")


def inputs(root):
    config = {
        "id": "00000000-0000-0000-0000-000000000000",
        "project": "tb-test",
        "owner": "o" * 64,
        "relay": "r" * 64,
        "office": "office-uuid",
        "coa_auth": ["auth", "o" * 64, "", "sig"],
        "provider": "openrouter",
        "model": "test-model",
        "host_root": str(root),
        "runtime_image": "sha256:test",
        "advertised_url": "http://test:3100",
    }
    secrets = {
        "provider_key": "model-secret",
        "token": "tok",
        "coa": "c" * 64,
        "admin": "a" * 64,
    }
    agent = {
        "id": "engineer",
        "name": "Engineer",
        "instructions": "Do work",
        "model": None,
        "secret": "s" * 64,
        "pubkey": "p" * 64,
        "state": "running",
        "auth_tag": ["auth", "o" * 64, "", "sig"],
        "channel_ids": ["chan-1"],
        "soul": "Be kind",
    }
    return config, secrets, agent


def normalize(value, root):
    return json.loads(
        json.dumps(value, sort_keys=True, default=str).replace(str(root), "{ROOT}")
    )


def test_hermes_render_unchanged(tmp_path):
    config, secrets, agent = inputs(tmp_path)
    document, harness_document, env, soul = render(config, secrets, agent)
    assert {
        "document": document,
        "harness_document": harness_document,
        "env": env,
        "soul": soul,
    } == EXPECTED["render"]


def test_hermes_managed_bundle_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    config, secrets, agent = inputs(tmp_path)
    _write_agent_files(tmp_path, config, secrets, agent)
    managed = tmp_path / "agents/engineer/managed"
    digests = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(managed.iterdir())
        if p.is_file()
    }
    assert digests == EXPECTED["managed"]
    assert (
        yaml.safe_load((managed / "config.yaml").read_text())
        == EXPECTED["managed_config"]
    )
    assert json.loads((managed / "env.json").read_text()) == EXPECTED["managed_env"]
    assert (
        json.loads((managed / "manifest.json").read_text())
        == EXPECTED["managed_manifest"]
    )


def test_hermes_container_spec_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("os.chown", lambda *args: None)
    config, secrets, agent = inputs(tmp_path)
    docker = Mock()
    docker.inspect.return_value = None
    start_agent(tmp_path, config, secrets, agent, docker)
    spec = docker.ensure.call_args.args[1]
    assert normalize(spec, tmp_path) == EXPECTED["spec"]
    from team_builder.docker import generation

    assert docker.ensure.call_args.args[2] == generation([spec["Image"], spec])
