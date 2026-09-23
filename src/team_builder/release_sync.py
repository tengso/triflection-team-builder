"""Outbound-only trusted GitHub Actions importer; never executes deployment plans.

The operator selects a repository/workflow/branch. Only successful runs of that
workflow in that repository can supply an image. No runner receives host access.
"""

import fcntl
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .deployment_cli import operator_request
from .github_access import token_for
from .storage import private_write

LIMIT = 4 * 1024**3


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    application: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    credential: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    workflow: str = Field(default="release.yml", pattern=r"^[a-zA-Z0-9_-]+\.ya?ml$")
    branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9_./-]+$")
    environments: list[str] = ["staging", "production"]
    services: list[str] = ["ui", "api"]


def validate_run(run, config, workflow_id, repository_id):
    if not (
        run.get("workflow_id") == workflow_id
        and run.get("head_repository", {}).get("id") == repository_id
        and run.get("head_branch") == config.branch
        and run.get("event") in ("push", "workflow_dispatch")
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
    ):
        raise ValueError("CI run does not match the trusted release policy")


def validate_bundle(path, run, config, destination):
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != ["image.tar", "release.json"]:
            raise ValueError("Unexpected release bundle contents")
        if sum(i.file_size for i in archive.infolist()) > LIMIT:
            raise ValueError("Release bundle exceeds size limit")
        if archive.getinfo("release.json").file_size > 16384:
            raise ValueError("Release manifest exceeds size limit")
        manifest = json.loads(archive.read("release.json"))
        if (
            manifest.get("repository") != config.repository
            or manifest.get("commit") != run["head_sha"]
            or manifest.get("run_id") != run["id"]
            or manifest.get("run_attempt") != run["run_attempt"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("image", ""))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("digest", ""))
            or manifest.get("registry") != "ghcr.io/" + config.repository.lower()
        ):
            raise ValueError("Release provenance does not match the verified CI run")
        image = destination / "image.tar"
        with archive.open("image.tar") as src, image.open("wb") as out:
            digest = hashlib.sha256()
            while chunk := src.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != manifest.get("archive_sha256"):
            raise ValueError("Image archive checksum mismatch")
    # Verify the content-addressed image config before giving the archive to Docker.
    with tarfile.open(image) as archive:
        entry = archive.getmember("manifest.json")
        if entry.size > 16384:
            raise ValueError("Invalid image manifest")
        images = json.load(archive.extractfile(entry))
        if len(images) != 1:
            raise ValueError("Expected exactly one image")
        entry = archive.getmember(images[0]["Config"])
        if not entry.isfile() or entry.size > 1024 * 1024:
            raise ValueError("Invalid image config")
        raw = archive.extractfile(entry).read()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != manifest["image"]:
            raise ValueError("Image config digest mismatch")
        metadata = json.loads(raw)
        if (
            metadata.get("os") != "linux"
            or metadata.get("architecture") != "amd64"
            or metadata.get("config", {})
            .get("Labels", {})
            .get("org.opencontainers.image.revision")
            != run["head_sha"]
        ):
            raise ValueError("Image platform or revision mismatch")
    manifest["image_config"] = metadata
    expected_tag = f"{manifest['registry']}:ci-{run['id']}-{run['run_attempt']}"
    if images[0].get("RepoTags") != [expected_tag]:
        raise ValueError("Image archive tag does not match the CI run")
    manifest["load_tag"] = expected_tag
    return manifest, image


def loaded_image_id(manifest):
    """Docker containerd stores use manifest IDs; classic stores use config IDs."""
    result = subprocess.run(
        ["docker", "image", "inspect", manifest["load_tag"]],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    image = json.loads(result.stdout)[0]
    config = manifest["image_config"]
    if (
        image.get("Os") != config["os"]
        or image.get("Architecture") != config["architecture"]
        or image.get("RootFS", {}).get("Layers")
        != config.get("rootfs", {}).get("diff_ids")
        or any(
            image.get("Config", {}).get(k) != v
            for k, v in config.get("config", {}).items()
        )
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", ""))
    ):
        raise ValueError(
            "Loaded Docker image does not match verified config and layers"
        )
    return image["Id"]


def sync(root, config):
    if not config.environments or set(config.environments) - {"staging", "production"}:
        raise ValueError("Invalid release environments")
    if not config.services or any(
        not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", s) for s in config.services
    ):
        raise ValueError("Invalid service names")
    directory = root / "release-sync" / config.application
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_file = directory / "status.json"
        state = (
            json.loads(state_file.read_text())
            if state_file.exists()
            else {"imported": []}
        )
        snapshot = operator_request(root, {"action": "inspect"})
        for env in config.environments:
            if not any(
                a["id"] == config.application
                and a["environment"] == env
                and a["repository"] == config.repository
                for a in snapshot["applications"]
            ):
                raise ValueError("Register the matching application/environment first")
        headers = {
            "Authorization": "Bearer " + token_for(root, config.credential),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Redirects to artifact storage must not carry the GitHub authorization token.
        with httpx.Client(
            headers=headers, timeout=120, follow_redirects=False
        ) as client:
            base = "https://api.github.com/repos/" + config.repository

            def get(path, **kwargs):
                response = client.get(base + path, **kwargs)
                response.raise_for_status()
                return response.json()

            repo = get("")
            workflow = get("/actions/workflows/" + config.workflow)
            runs = get(
                "/actions/workflows/" + str(workflow["id"]) + "/runs",
                params={"branch": config.branch, "status": "success", "per_page": 20},
            )["workflow_runs"]
            for run in reversed(runs):
                if run.get("event") not in ("push", "workflow_dispatch"):
                    continue
                validate_run(run, config, workflow["id"], repo["id"])
                key = f"{repo['id']}:{run['id']}:{run['run_attempt']}"
                if key in state["imported"]:
                    continue
                name = f"team-builder-release-{run['run_attempt']}"
                artifacts = [
                    a
                    for a in get(
                        f"/actions/runs/{run['id']}/artifacts", params={"per_page": 100}
                    )["artifacts"]
                    if a["name"] == name and not a["expired"]
                ]
                if len(artifacts) != 1:
                    continue
                artifact = artifacts[0]
                if artifact["size_in_bytes"] > LIMIT:
                    raise ValueError("Release artifact exceeds size limit")
                with tempfile.TemporaryDirectory(
                    prefix="release-", dir=directory
                ) as tmp:
                    tmp = Path(tmp)
                    cache = directory / f"artifact-{artifact['id']}.zip"
                    if cache.exists():
                        with cache.open("rb") as cached:
                            actual = (
                                "sha256:"
                                + hashlib.file_digest(cached, "sha256").hexdigest()
                            )
                        if cache.stat().st_size > LIMIT or actual != artifact.get(
                            "digest"
                        ):
                            cache.unlink()
                            raise ValueError("Cached artifact checksum mismatch")
                        bundle = cache
                    else:
                        response = client.get(
                            base + f"/actions/artifacts/{artifact['id']}/zip"
                        )
                        if response.status_code != 302:
                            raise ValueError(
                                "Expected authenticated artifact download redirect"
                            )
                        location = response.headers["location"]
                        if not location.startswith("https://"):
                            raise ValueError("Artifact storage must use HTTPS")
                        bundle = tmp / "bundle.zip"
                        bundle_digest = hashlib.sha256()
                        with httpx.stream(
                            "GET", location, timeout=120, follow_redirects=False
                        ) as download:
                            download.raise_for_status()
                            total = 0
                            with bundle.open("wb") as out:
                                for chunk in download.iter_bytes():
                                    total += len(chunk)
                                    if total > LIMIT:
                                        raise ValueError(
                                            "Release download exceeds size limit"
                                        )
                                    bundle_digest.update(chunk)
                                    out.write(chunk)
                        if (
                            artifact.get("digest")
                            != "sha256:" + bundle_digest.hexdigest()
                        ):
                            raise ValueError("GitHub artifact digest mismatch")
                        bundle.replace(cache)
                        bundle = cache
                        for previous in directory.glob("artifact-*.zip"):
                            if previous != cache:
                                previous.unlink()
                    manifest, image = validate_bundle(bundle, run, config, tmp)
                    subprocess.run(
                        ["docker", "image", "load", "--input", str(image)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=600,
                    )
                    host_image = loaded_image_id(manifest)
                    release_id = f"ci-{run['id']}-{run['run_attempt']}"
                    for env in config.environments:
                        operator_request(
                            root,
                            {
                                "action": "release",
                                "release": {
                                    "id": release_id,
                                    "application": config.application,
                                    "environment": env,
                                    "commit": run["head_sha"],
                                    "images": {s: host_image for s in config.services},
                                },
                            },
                        )
                    state["imported"] = (state["imported"] + [key])[-200:]
                    state["latest"] = {
                        "release": release_id,
                        "commit": run["head_sha"],
                        "image": host_image,
                        "registry_digest": manifest["digest"],
                        "run_url": run["html_url"],
                    }
                    private_write(state_file, state)
                    cache.unlink(missing_ok=True)
                    print(
                        "Registered "
                        + release_id
                        + " for "
                        + ", ".join(config.environments),
                        flush=True,
                    )
        state["checked_at"] = time.time()
        (directory / "error.json").unlink(missing_ok=True)
        private_write(state_file, state)
        return state


def command(args):
    root = Path(args.state_dir).expanduser().resolve()
    config = Configuration.model_validate_json(Path(args.config_file).read_text())
    try:
        result = sync(root, config)
    except BlockingIOError:
        print("Release sync already running")
        return
    except Exception as exc:  # noqa: BLE001 -- never leak signed URLs or credentials
        error = {"failed_at": time.time(), "error": type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            error["http_status"] = exc.response.status_code
        directory = root / "release-sync" / config.application
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(directory / "error.json", error)
        # Never print signed download URLs, API responses or credentials.
        raise SystemExit(
            "Release sync failed ("
            + type(exc).__name__
            + "); check workflow, credential permissions and host services"
        ) from None
    print(json.dumps(result, indent=2))
