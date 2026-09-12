import pytest

from team_builder import cli


def test_published_runtime_is_used_without_a_local_build(monkeypatch):
    commands = []

    def run(command):
        commands.append(command)
        return "1"

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(
        cli, "build_runtime", lambda _: pytest.fail("Unexpected local build")
    )
    assert cli.prepare_runtime("sha256:verified") == "sha256:verified"
    assert commands[0][-1] == "sha256:verified"


def test_unbundled_runtime_is_rejected(monkeypatch):
    monkeypatch.setattr(cli, "run", lambda _: "<no value>")
    with pytest.raises(ValueError, match="--hermes-image"):
        cli.prepare_runtime("sha256:unbundled")


def test_custom_hermes_base_preserves_the_local_build_path(monkeypatch):
    monkeypatch.setattr(cli, "build_runtime", lambda image: "built:" + image)
    assert (
        cli.prepare_runtime("sha256:custom", custom_base=True) == "built:sha256:custom"
    )
