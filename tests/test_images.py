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
    monkeypatch.setattr(cli, "run", lambda _: "1")
    assert (
        cli.prepare_runtime("sha256:custom", custom_base=True) == "built:sha256:custom"
    )


def test_require_harness_image_accepts_matching_labels(monkeypatch):
    monkeypatch.setattr(
        cli,
        "run",
        lambda _: (
            '{"io.team-builder.runtime": "1", "io.team-builder.harness": "pi",'
            ' "io.team-builder.buzz-acp": "1"}'
        ),
    )
    cli.require_harness_image("image-ref", "pi")


def test_require_harness_image_rejects_mismatched_labels(monkeypatch):
    monkeypatch.setattr(
        cli,
        "run",
        lambda _: (
            '{"io.team-builder.runtime": "1", "io.team-builder.harness": "codex",'
            ' "io.team-builder.buzz-acp": "1"}'
        ),
    )
    with pytest.raises(ValueError, match="not a Team Builder pi harness"):
        cli.require_harness_image("image-ref", "pi")
    monkeypatch.setattr(
        cli,
        "run",
        lambda _: '{"io.team-builder.runtime": "1", "io.team-builder.harness": "pi"}',
    )
    with pytest.raises(ValueError, match="not a Team Builder pi harness"):
        cli.require_harness_image("image-ref", "pi")
    monkeypatch.setattr(cli, "run", lambda _: "null")
    with pytest.raises(ValueError, match="not a Team Builder pi harness"):
        cli.require_harness_image("image-ref", "pi")


def test_ensure_pulls_a_missing_image_reference():
    from team_builder.docker import Docker

    requests = []

    class Response:
        def __init__(self, status_code, body=b""):
            self.status_code = status_code
            self.content = body

        def json(self):
            import json

            return json.loads(self.content) if self.content else {}

    class Client:
        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            if url.endswith("/json") and "/images/" in url:
                return Response(404)
            if url.endswith("/json") and "/containers/" in url:
                return Response(404)
            return Response(201)

    docker = Docker("proj")
    docker.client = Client()
    docker.ensure(
        "agent",
        {"Image": "registry.example/pi:1.0", "Labels": {}},
        "gen",
    )
    pulls = [r for r in requests if r[1].endswith("/images/create")]
    assert pulls and pulls[0][2]["params"] == {"fromImage": "registry.example/pi:1.0"}
    assert any(r[1].endswith("/containers/create?name=agent") for r in requests)


def test_ensure_does_not_pull_an_image_id():
    from team_builder.docker import Docker

    requests = []

    class Response:
        def __init__(self, status_code, body=b""):
            self.status_code = status_code
            self.content = body

        def json(self):
            import json

            return json.loads(self.content) if self.content else {}

    class Client:
        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            return Response(404 if url.endswith("/json") else 201)

    docker = Docker("proj")
    docker.client = Client()
    docker.ensure(
        "agent",
        {"Image": "sha256:" + "ab" * 32, "Labels": {}},
        "gen",
    )
    assert not [r for r in requests if r[1].endswith("/images/create")]


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


def test_pre_buzz_acp_runtime_is_rejected(monkeypatch):
    labels = {"runtime": "1", "buzz-acp": "<no value>"}
    monkeypatch.setattr(
        cli, "run", lambda command: labels[command[4].split('"')[1].split(".")[-1]]
    )
    with pytest.raises(ValueError, match="predates it"):
        cli.prepare_runtime("sha256:v060")
    monkeypatch.setattr(cli, "build_runtime", lambda image: "built:" + image)
    with pytest.raises(ValueError, match="predates it"):
        cli.prepare_runtime("sha256:old-base", custom_base=True)
