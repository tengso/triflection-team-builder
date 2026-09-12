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


def test_source_patch_applies_inside_publisher_git_checkout(tmp_path):
    import runpy
    import subprocess
    from pathlib import Path

    helper = runpy.run_path(
        str(Path(__file__).parents[1] / "packaging/prepare_images.py")
    )
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / ".image-build" / "upstream"
    source.mkdir(parents=True)
    (source / "example.txt").write_text("before\n")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-before\n+after\n"
    )
    helper["apply_source_patch"](source, patch)
    assert (source / "example.txt").read_text() == "after\n"
