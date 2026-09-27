import json

import yaml

from team_builder.storage import private_write
from team_builder.upgrade import upgrade


def test_upgrade_preserves_state_and_limits_compose_to_manager(tmp_path, monkeypatch):
    from team_builder import cli

    monkeypatch.setattr("team_builder.upgrade.platform.system", lambda: "Linux")
    config = {
        "host_root": str(tmp_path),
        "runtime_image": "old",
        "images": {"hermes": "old", "buzz": "relay"},
        "owner": "keep-owner",
    }
    compose = {
        "services": {
            "manager": {"image": "old", "volumes": ["state:/state"]},
            "relay": {"image": "relay"},
        },
        "volumes": {"data": {}},
    }
    private_write(tmp_path / "config.json", config)
    private_write(tmp_path / "compose.yaml", yaml.safe_dump(compose).encode())
    (tmp_path / "registry.sqlite3").write_bytes(b"preserve registry")
    calls = []
    monkeypatch.setattr(cli, "resolve_image", lambda image: "new-digest")
    monkeypatch.setattr(cli, "prepare_runtime", lambda image: image)
    monkeypatch.setattr(cli, "run", lambda args, **kwargs: calls.append(args))
    upgrade(str(tmp_path), "new-image")
    actual = json.loads((tmp_path / "config.json").read_text())
    assert actual["owner"] == "keep-owner" and actual["images"]["buzz"] == "relay"
    assert actual["runtime_image"] == "new-digest"
    actual_compose = yaml.safe_load((tmp_path / "compose.yaml").read_text())
    assert actual_compose["services"]["relay"] == compose["services"]["relay"]
    assert actual_compose["volumes"] == compose["volumes"]
    assert (tmp_path / "registry.sqlite3").read_bytes() == b"preserve registry"
    backup = next((tmp_path / "upgrades").iterdir())
    assert json.loads((backup / "config.json").read_text()) == config
    assert "--no-deps" in calls[0] and calls[0][-1] == "manager"


def test_harness_upgrade_updates_images_and_backs_up(tmp_path, monkeypatch):
    import pytest

    from team_builder import cli

    monkeypatch.setattr("team_builder.upgrade.platform.system", lambda: "Linux")
    config = {
        "host_root": str(tmp_path),
        "runtime_image": "old",
        "images": {"hermes": "old", "buzz": "relay"},
        "owner": "keep-owner",
    }
    compose = {
        "services": {
            "manager": {"image": "old", "volumes": ["state:/state"]},
            "relay": {"image": "relay"},
        },
        "volumes": {"data": {}},
    }
    private_write(tmp_path / "config.json", config)
    private_write(tmp_path / "compose.yaml", yaml.safe_dump(compose).encode())
    calls = []
    monkeypatch.setattr(cli, "resolve_image", lambda image: "codex-digest")
    monkeypatch.setattr(cli, "require_harness_image", lambda image, name: None)
    monkeypatch.setattr(cli, "run", lambda args, **kwargs: calls.append(args))
    upgrade(str(tmp_path), harness="codex", harness_image="registry/codex:1.0")
    actual = json.loads((tmp_path / "config.json").read_text())
    assert actual["images"]["codex"] == "codex-digest"
    assert actual["runtime_image"] == "old"
    backup = next((tmp_path / "upgrades").iterdir())
    assert json.loads((backup / "config.json").read_text()) == config
    assert any("restart" in call for call in calls)
    with pytest.raises(ValueError, match="--image is required"):
        upgrade(str(tmp_path), harness="codex")
    with pytest.raises(ValueError, match="cannot be combined"):
        upgrade(str(tmp_path), "img", harness="codex", harness_image="ref")


def _installation(tmp_path):
    config = {
        "host_root": str(tmp_path),
        "runtime_image": "old-runtime",
        "images": {"hermes": "old-runtime", "buzz": "relay"},
    }
    compose = {"services": {"manager": {"image": "old-runtime"}}}
    private_write(tmp_path / "config.json", config)
    private_write(tmp_path / "compose.yaml", yaml.safe_dump(compose).encode())


def test_manager_only_upgrade_refuses_pre_buzz_acp_workers(tmp_path, monkeypatch):
    import pytest

    from team_builder import cli

    monkeypatch.setattr("team_builder.upgrade.platform.system", lambda: "Linux")
    _installation(tmp_path)
    monkeypatch.setattr(cli, "resolve_image", lambda image: "new-digest")
    monkeypatch.setattr(cli, "prepare_runtime", lambda image: image)
    monkeypatch.setattr(cli, "run", lambda args, **kwargs: "<no value>")
    with pytest.raises(ValueError, match="full upgrade without --manager-only"):
        upgrade(str(tmp_path), "new-image", manager_only=True)
    assert json.loads((tmp_path / "config.json").read_text())["runtime_image"] == (
        "old-runtime"
    )


def test_full_upgrade_records_published_harness_images(tmp_path, monkeypatch):
    from team_builder import cli

    monkeypatch.setattr("team_builder.upgrade.platform.system", lambda: "Linux")
    _installation(tmp_path)
    monkeypatch.setattr(cli, "resolve_image", lambda image: "new-digest")
    monkeypatch.setattr(cli, "prepare_runtime", lambda image: image)
    monkeypatch.setattr(cli, "run", lambda args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "PUBLISHED_IMAGES",
        {"buzz": "b", "hermes": "h", "pi": "ghcr.io/x/pi@sha256:1"},
    )
    upgrade(str(tmp_path), "new-image")
    images = json.loads((tmp_path / "config.json").read_text())["images"]
    assert images["pi"] == "ghcr.io/x/pi@sha256:1"
    assert "codex" not in images and images["hermes"] == "new-digest"
