import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from team_builder.release_sync import Configuration, validate_bundle, validate_run

CONFIG = Configuration(repository="tengso/app", application="app", credential="github")
RUN = {
    "id": 123,
    "run_attempt": 1,
    "workflow_id": 4,
    "head_repository": {"id": 5},
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
    "head_sha": "a" * 40,
}


def bundle(tmp_path, **overrides):
    raw = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "config": {
                "Labels": {"org.opencontainers.image.revision": RUN["head_sha"]}
            },
        }
    ).encode()
    image = "sha256:" + hashlib.sha256(raw).hexdigest()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, data in [
            ("config.json", raw),
            (
                "manifest.json",
                b'[{"Config":"config.json","RepoTags":["ghcr.io/tengso/app:ci-123-1"]}]',
            ),
        ]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    data = stream.getvalue()
    manifest = {
        "repository": CONFIG.repository,
        "commit": RUN["head_sha"],
        "run_id": 123,
        "run_attempt": 1,
        "image": image,
        "digest": "sha256:" + "b" * 64,
        "registry": "ghcr.io/tengso/app",
        "archive_sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest.update(overrides)
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("release.json", json.dumps(manifest))
        z.writestr("image.tar", data)
    return path


def test_valid_bundle(tmp_path):
    manifest, image = validate_bundle(bundle(tmp_path), RUN, CONFIG, tmp_path)
    assert image.exists() and manifest["commit"] == RUN["head_sha"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": "attacker/app"},
        {"commit": "b" * 40},
        {"run_attempt": 2},
        {"archive_sha256": "0" * 64},
        {"image": "sha256:" + "c" * 64},
        {"registry": "ghcr.io/attacker/app"},
    ],
)
def test_reject_mismatched_artifacts(tmp_path, overrides):
    with pytest.raises(ValueError):
        validate_bundle(bundle(tmp_path, **overrides), RUN, CONFIG, tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_id", 7),
        ("head_repository", {"id": 6}),
        ("head_branch", "feature"),
        ("event", "pull_request"),
        ("conclusion", "failure"),
        ("status", "in_progress"),
    ],
)
def test_run_trust_boundary(field, value):
    validate_run(RUN, CONFIG, 4, 5)
    with pytest.raises(ValueError):
        validate_run({**RUN, field: value}, CONFIG, 4, 5)


def test_bundle_rejects_extra_paths(tmp_path):
    path = bundle(tmp_path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("../escape", "bad")
    with pytest.raises(ValueError):
        validate_bundle(path, RUN, CONFIG, tmp_path)


def test_sync_retry_registers_both_environments_without_deployment(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    import httpx

    from team_builder import release_sync as module

    payload = bundle(tmp_path).read_bytes()
    calls = []
    fail_once = [True]

    def operator(root, request):
        calls.append(request)
        if request["action"] == "inspect":
            return {
                "applications": [
                    {"id": "app", "environment": env, "repository": CONFIG.repository}
                    for env in CONFIG.environments
                ]
            }
        if (
            request["release"]["environment"] == "production" and fail_once.pop()
            if fail_once
            else False
        ):
            raise RuntimeError("interrupted registration")
        return {"ok": True}

    def respond(request):
        path = request.url.path
        if path.endswith("/zip"):
            return httpx.Response(
                302, headers={"location": "https://storage.example/artifact"}
            )
        if path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 7,
                            "name": "team-builder-release-1",
                            "expired": False,
                            "size_in_bytes": len(payload),
                            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        }
                    ]
                },
            )
        if path.endswith("/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            **RUN,
                            "html_url": "https://github.com/tengso/app/actions/runs/123",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"id": 4 if path.endswith("release.yml") else 5}
        )

    original = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )

    def stream(method, url, **kw):
        assert "headers" not in kw  # no GitHub credentials sent to blob storage
        return original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=payload)
            )
        ).stream(method, url)

    monkeypatch.setattr(module.httpx, "stream", stream)
    monkeypatch.setattr(module, "operator_request", operator)
    monkeypatch.setattr(module, "token_for", lambda *a: "private-test-token")
    monkeypatch.setattr(module, "loaded_image_id", lambda manifest: manifest["image"])
    docker = Mock()
    monkeypatch.setattr(module.subprocess, "run", docker)
    with pytest.raises(RuntimeError):
        module.sync(tmp_path, CONFIG)
    result = module.sync(tmp_path, CONFIG)
    assert result["latest"]["release"] == "ci-123-1"
    count = docker.call_count
    module.sync(tmp_path, CONFIG)
    assert docker.call_count == count
    releases = [r["release"] for r in calls if r["action"] == "release"]
    assert {r["environment"] for r in releases} == {"staging", "production"}
    assert len({r["images"]["ui"] for r in releases}) == 1
    assert all(r["action"] in ("inspect", "release") for r in calls)


@pytest.mark.parametrize("host_id", ["a", "b"])
def test_containerd_and_classic_loaded_image_identity(monkeypatch, host_id):
    from types import SimpleNamespace

    from team_builder import release_sync as module

    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"Cmd": ["ui"]},
        "rootfs": {"diff_ids": ["sha256:layer"]},
    }
    image = {
        "Id": "sha256:" + host_id * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Cmd": ["ui"]},
        "RootFS": {"Layers": ["sha256:layer"]},
    }
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=json.dumps([image])),
    )
    manifest = {
        "load_tag": "ghcr.io/test/app:ci-1-1",
        "image_config": config,
        "image": "sha256:" + "a" * 64,
    }
    assert module.loaded_image_id(manifest) == image["Id"]
    image["RootFS"]["Layers"] = ["sha256:other"]
    with pytest.raises(ValueError, match="config and layers"):
        module.loaded_image_id(manifest)
