"""Load managed skill assignments and report the exact gateway configuration."""

import hashlib
import json
import os
import re
from pathlib import Path

from .storage import private_write


def install_skills(home, managed, root=None):
    source = managed / "skills.json"
    if not source.exists():
        return
    skills = json.loads(source.read_text())
    root = root or home / "skills"
    previous = home / ".team-builder-skills.json"
    old = json.loads(previous.read_text()) if previous.exists() else []
    for identifier in set(old) | set(skills):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", identifier):
            raise ValueError("Invalid managed skill ID")
        directory = root / ("team-managed-" + identifier)
        if directory.is_symlink():
            raise ValueError("Managed skill directory must not be a symlink")
        if identifier in skills:
            directory.mkdir(parents=True, exist_ok=True)
            private_write(directory / "SKILL.md", skills[identifier].encode())
        else:
            (directory / "SKILL.md").unlink(missing_ok=True)
    private_write(previous, sorted(skills))


def process_start_time(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def applied_marker(home, managed):
    source = managed / "revision.json"
    if source.exists():
        pid = os.getpid()
        private_write(
            home / "team-builder-applied.json",
            {
                "pid": pid,
                "start_time": process_start_time(pid),
                **json.loads(source.read_text()),
            },
        )


def verify_bundle(managed):
    manifest = json.loads((managed / "manifest.json").read_text())
    required = {
        "config.yaml",
        "env.json",
        "SOUL.md",
        "skills.json",
        "revision.json",
    }
    for name, digest in manifest.items():
        if name not in required | {"harness.json", "rules.toml"}:
            raise ValueError("Unexpected managed configuration file")
        if hashlib.sha256((managed / name).read_bytes()).hexdigest() != digest:
            raise ValueError(
                "Incomplete configuration write; retry applying saved settings"
            )
    if not required.issubset(manifest):
        raise ValueError("Incomplete configuration manifest")
