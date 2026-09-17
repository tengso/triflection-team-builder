import json

import pytest
import yaml

from team_builder import cli
from team_builder.compose import render as compose
from team_builder.nostr import key
from team_builder.runtime import render


@pytest.mark.parametrize(
    "client",
    ["http://127.0.0.1:3400", "http://localhost:3400", "http://buzz.example:80"],
)
def test_tunnel_can_use_different_client_and_server_ports(client):
    cli.validate_network(client, "127.0.0.1", 3100, "http://relay:3000")


@pytest.mark.parametrize(
    "internal",
    [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://0.0.0.0:3000",
        "http://user:password@relay:3000",
        "http://relay:3000/path",
        "http://relay:3000?query",
        "http://relay:3000#fragment",
        "ws://relay:3000",
        "http://relay:0",
        "http://relay:65536",
    ],
)
def test_invalid_internal_origin_is_rejected(internal):
    with pytest.raises(ValueError):
        cli.validate_network("http://127.0.0.1:3400", "127.0.0.1", 3100, internal)


@pytest.mark.parametrize(
    "client",
    [
        "http://0.0.0.0:3400",
        "http://user:pass@localhost:3400",
        "http://localhost:3400/path",
        "http://localhost:0",
    ],
)
def test_invalid_client_origin_still_rejected(client):
    with pytest.raises(ValueError):
        cli.validate_network(client, "127.0.0.1", 3100, "http://relay:3000")


def test_internal_url_flag_and_environment(monkeypatch):
    monkeypatch.setenv("TEAM_BUILDER_INTERNAL_URL", "http://relay:3000")
    assert cli.parser().parse_args(["init"]).internal_url == "http://relay:3000"
    assert (
        cli.parser()
        .parse_args(["init", "--internal-url", "http://relay:3001"])
        .internal_url
        == "http://relay:3001"
    )


def test_separate_urls_preserve_public_metadata_and_agent_connectivity(manager):
    config = dict(
        manager.config,
        advertised_url="http://127.0.0.1:3400",
        internal_url="http://relay:3000",
        bind="127.0.0.1",
        port=3100,
        name="Tunnel test",
        images={
            k: "sha256:test" for k in ("relay", "postgres", "redis", "minio", "mc")
        },
    )
    secrets = dict(
        manager.secrets,
        relay=key(),
        **{k: "test" for k in ("database", "redis", "s3", "hmac")},
    )
    services = yaml.safe_load(compose(config, secrets))["services"]
    relay = services["relay"]
    assert relay["ports"] == ["127.0.0.1:3100:3000"]
    assert relay["environment"]["RELAY_URL"] == "ws://127.0.0.1:3400"
    assert relay["environment"]["BUZZ_MEDIA_BASE_URL"] == "http://127.0.0.1:3400/media"
    assert relay["environment"]["BUZZ_INTERNAL_RELAY_URL"] == "http://relay:3000"
    worker_config, env, _ = render(config, secrets, manager.resource("coa", "agent"))
    assert (
        worker_config["gateway"]["platforms"]["buzz"]["extra"]["relay_url"]
        == "http://relay:3000"
    )
    assert env["BUZZ_RELAY_URL"] == "http://relay:3000"
    del config["internal_url"]
    worker_config, env, _ = render(config, secrets, manager.resource("coa", "agent"))
    assert env["BUZZ_RELAY_URL"] == config["advertised_url"]
    assert (
        "BUZZ_INTERNAL_RELAY_URL"
        not in yaml.safe_load(compose(config, secrets))["services"]["relay"][
            "environment"
        ]
    )


def test_old_images_fail_before_bootstrap(monkeypatch):
    monkeypatch.setattr(cli, "run", lambda args: "<no value>")
    with pytest.raises(ValueError, match="updated Buzz"):
        cli.require_internal_relay_image("old-image")


def test_interrupted_setup_preserves_separate_urls_and_identities(
    tmp_path, monkeypatch
):
    owner_file = tmp_path / "owner.key"
    owner_file.write_text(key())
    monkeypatch.setenv("TEAM_BUILDER_PROVIDER_KEY", "test-not-real")
    monkeypatch.setattr(cli, "resolve_image", lambda image: "sha256:test")
    monkeypatch.setattr(cli, "prepare_runtime", lambda image, **kwargs: image)
    monkeypatch.setattr(cli, "require_internal_relay_image", lambda image: None)

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before services start")

    monkeypatch.setattr(cli, "run", interrupt)
    args = cli.parser().parse_args(
        [
            "init",
            "--non-interactive",
            "--name",
            "Tunnel",
            "--advertised-url",
            "http://127.0.0.1:3400",
            "--internal-url",
            "http://relay:3000",
            "--port",
            "3100",
            "--bind",
            "127.0.0.1",
            "--model",
            "test",
            "--owner-key-file",
            str(owner_file),
        ]
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        cli.initialize(tmp_path, args)
    before = (
        (tmp_path / "config.json").read_bytes(),
        (tmp_path / "secrets.json").read_bytes(),
    )
    assert json.loads(before[0])["internal_url"] == "http://relay:3000"
    owner_file.unlink()
    with pytest.raises(RuntimeError, match="interrupted"):
        cli.initialize(tmp_path, cli.parser().parse_args(["init", "--non-interactive"]))
    assert before == (
        (tmp_path / "config.json").read_bytes(),
        (tmp_path / "secrets.json").read_bytes(),
    )
    args.internal_url = "http://different:3000"
    with pytest.raises(ValueError, match="Existing installation settings differ"):
        cli.initialize(tmp_path, args)
