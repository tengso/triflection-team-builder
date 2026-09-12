"""Prepare public source-only build contexts; never copy installation state."""

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    sources = json.loads((root / "packaging/sources.json").read_text())
    output = root / ".image-build"
    output.mkdir(exist_ok=True)
    for name, source in sources.items():
        destination = output / name
        if destination.exists():
            raise SystemExit(
                f"Remove the previous build context before retrying: {destination}"
            )
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "source.tar.gz"
            url = f"https://codeload.github.com/{source['repository']}/tar.gz/{source['revision']}"
            with (
                urllib.request.urlopen(url, timeout=120) as response,
                archive.open("wb") as file,
            ):
                shutil.copyfileobj(response, file)
            with tarfile.open(archive) as tar:
                tar.extractall(temp, filter="data")
            directories = [p for p in Path(temp).iterdir() if p.is_dir()]
            if len(directories) != 1:
                raise RuntimeError("Unexpected upstream source archive layout")
            shutil.move(directories[0], destination)
        if name == "buzz":
            patch = root / "packaging/patches/buzz-external-community.patch"
            subprocess.run(
                ["git", "apply", "--check", str(patch)], cwd=destination, check=True
            )
            subprocess.run(["git", "apply", str(patch)], cwd=destination, check=True)
            source["patch_sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
        else:
            builder = destination / "team-builder"
            builder.mkdir()
            shutil.copy2(root / "pyproject.toml", builder)
            shutil.copytree(
                root / "src/team_builder",
                builder / "src/team_builder",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        (destination / ".dockerignore").write_text(
            ".git\n.venv\ntarget\n**/__pycache__\n"
        )
        (destination / "team-builder-source.json").write_text(
            json.dumps(source, indent=2) + "\n"
        )
        print(f"Prepared {name} at {source['revision']}", flush=True)


if __name__ == "__main__":
    main()
