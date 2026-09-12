import hashlib
import json
import threading

import httpx
import pytest
import yaml
from coincurve import PublicKeyXOnly
from conftest import message

from team_builder.compose import render as compose
from team_builder.models import validate
from team_builder.nostr import attestation, key, public, sign, verify
from team_builder.runtime import render
from team_builder.server import serve


def test_signatures_and_owner_attestation():
    owner, agent = key(), key()
    event = sign(agent, 0, [attestation(owner, public(agent))], "{}")
    assert verify(event)
    auth = event["tags"][0]
    digest = hashlib.sha256(f"nostr:agent-auth:{public(agent)}:".encode()).digest()
    assert PublicKeyXOnly(bytes.fromhex(public(owner))).verify(
        bytes.fromhex(auth[3]), digest
    )
    event["content"] = '{"name":"forged"}'
    assert not verify(event)


@pytest.mark.parametrize(
    "operation",
    [
        {"action": "create_channel", "id": "../../host", "name": "Attack"},
        {
            "action": "create_agent",
            "id": "agent",
            "name": "Agent",
            "instructions": "X",
            "channels": [],
            "image": "arbitrary",
        },
        {"action": "docker_exec", "command": "rm -rf /"},
    ],
)
def test_restricted_operation_schema(operation):
    with pytest.raises(ValueError):
        validate([operation])


def test_worker_has_no_management_access(manager, create_ops):
    manager.execute(message(manager), create_ops)
    worker = manager.resource("engineer", "agent")
    config, env, soul = render(manager.config, manager.secrets, worker)
    assert not config["mcp_servers"]
    assert manager.secrets["admin"] not in json.dumps([config, env, soul])
    assert manager.secrets["token"] not in json.dumps([config, env, soul])
    assert manager.owner_secret not in json.dumps([config, env, soul])
    coa = manager.resource("coa", "agent")
    config, env, _ = render(manager.config, manager.secrets, coa)
    assert (
        config["mcp_servers"]["team"]["env"]["TEAM_BUILDER_TOKEN"]
        == manager.secrets["token"]
    )
    assert env["TEAM_BUILDER_OWNER"] == manager.config["owner"]


def test_provider_aliases_are_native_hermes_configuration(manager):
    agent = manager.resource("coa", "agent")
    config, env, _ = render(
        dict(manager.config, provider="openai"), manager.secrets, agent
    )
    assert config["model"]["provider"] == "openai-api"
    assert env["OPENAI_API_KEY"] == manager.secrets["provider_key"]
    config, env, _ = render(
        dict(manager.config, provider="custom", base_url="http://models:8080/v1"),
        manager.secrets,
        agent,
    )
    assert config["model"]["provider"] == "custom:team"
    assert config["providers"]["team"]["api"] == "http://models:8080/v1"


@pytest.mark.parametrize(
    "endpoint,bind,port",
    [
        ("http://localhost:3100", "0.0.0.0", 3100),
        ("http://ubuntu.orb.local:3100", "127.0.0.1", 3100),
        ("http://ubuntu.orb.local:3100", "0.0.0.0", 3200),
        ("http://name:password@host:3100", "0.0.0.0", 3100),
    ],
)
def test_network_preflight_rejects_unreachable_or_ambiguous_setup(endpoint, bind, port):
    from team_builder.cli import validate_network

    with pytest.raises(ValueError):
        validate_network(endpoint, bind, port)


def test_network_preflight_accepts_private_hostname():
    from team_builder.cli import validate_network

    validate_network("http://ubuntu.orb.local:3310", "0.0.0.0", 3310)


def test_http_authentication_and_proposal_override_rejected(manager, create_ops):
    server = serve(manager, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            assert client.post("/inspect", json={}).status_code == 403
            client.headers["Authorization"] = "Bearer test-token"
            assert client.post("/inspect", json={}).status_code == 200
            result = client.post(
                "/execute",
                json={
                    "source_event_id": message(manager),
                    "operations": create_ops,
                    "proposal": True,
                },
            )
            assert result.status_code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_compose_keeps_database_private_and_only_manager_has_socket(manager):
    config = dict(
        manager.config,
        images={
            k: "sha256:test" for k in ("relay", "postgres", "redis", "minio", "mc")
        },
        advertised_url="http://ubuntu.orb.local:3100",
        bind="127.0.0.1",
        port=3100,
        name="Test",
    )
    secrets = dict(
        manager.secrets,
        **{k: "test" for k in ("database", "redis", "s3", "hmac")},
        relay=key(),
    )
    services = yaml.safe_load(compose(config, secrets))["services"]
    assert (
        services["relay"]["environment"]["BUZZ_MEDIA_BASE_URL"]
        == "http://ubuntu.orb.local:3100/media"
    )
    assert all(
        "ports" not in services[k] for k in ("postgres", "redis", "minio", "manager")
    )
    assert "/var/run/docker.sock:/var/run/docker.sock" in services["manager"]["volumes"]
    assert all(
        "docker.sock" not in json.dumps(v)
        for k, v in services.items()
        if k != "manager"
    )
