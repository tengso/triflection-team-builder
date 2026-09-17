"""Load managed skill assignments and report the exact gateway configuration."""

import hashlib
import json
import os
import re
from pathlib import Path

from .storage import private_write


def install_skills(home, managed):
    source = managed / "skills.json"
    if not source.exists():
        return
    skills = json.loads(source.read_text())
    previous = home / ".team-builder-skills.json"
    old = json.loads(previous.read_text()) if previous.exists() else []
    for identifier in set(old) | set(skills):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", identifier):
            raise ValueError("Invalid managed skill ID")
        directory = home / "skills" / ("team-managed-" + identifier)
        if directory.is_symlink():
            raise ValueError("Managed skill directory must not be a symlink")
        if identifier in skills:
            directory.mkdir(parents=True, exist_ok=True)
            private_write(directory / "SKILL.md", skills[identifier].encode())
        else:
            (directory / "SKILL.md").unlink(missing_ok=True)
    private_write(previous, sorted(skills))


def applied_marker(home, managed):
    source = managed / "revision.json"
    if source.exists():
        pid = os.getpid()
        start = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        private_write(
            home / "team-builder-applied.json",
            {"pid": pid, "start_time": start, **json.loads(source.read_text())},
        )


def verify_bundle(managed):
    manifest = json.loads((managed / "manifest.json").read_text())
    for name, digest in manifest.items():
        if name not in {
            "config.yaml",
            "env.json",
            "SOUL.md",
            "skills.json",
            "revision.json",
        }:
            raise ValueError("Unexpected managed configuration file")
        if hashlib.sha256((managed / name).read_bytes()).hexdigest() != digest:
            raise ValueError(
                "Incomplete configuration write; retry applying saved settings"
            )
    if set(manifest) != {
        "config.yaml",
        "env.json",
        "SOUL.md",
        "skills.json",
        "revision.json",
    }:
        raise ValueError("Incomplete configuration manifest")
