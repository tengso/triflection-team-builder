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
