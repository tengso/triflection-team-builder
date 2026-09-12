"""Upgrade only the management/agent runtime, preserving community data."""

import fcntl
import json
import platform
import uuid
from pathlib import Path

import yaml

from .storage import private_write


def upgrade(state_dir, image):
    from .cli import prepare_runtime, resolve_image, run

    if platform.system() != "Linux":
        raise ValueError("Run upgrades on the Linux host of the installation")
    root = Path(state_dir).expanduser().resolve()
    if not (root / "config.json").is_file():
        raise ValueError("Installation not found")
    with (root / "init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((root / "config.json").read_text())
        if config["host_root"] != str(root):
            raise ValueError("Installation was moved; restore its recorded state path")
        runtime = prepare_runtime(resolve_image(image))
        compose_path = root / "compose.yaml"
        document = yaml.safe_load(compose_path.read_text())
        backup = root / "upgrades" / str(uuid.uuid4())
        private_write(backup / "config.json", (root / "config.json").read_bytes())
        private_write(backup / "compose.yaml", compose_path.read_bytes())
        config["runtime_image"] = runtime
        config["images"]["hermes"] = runtime
        document["services"]["manager"]["image"] = runtime
        private_write(root / "config.json", config)
        private_write(compose_path, yaml.safe_dump(document).encode())
        print(
            f"Starting updated management runtime. Previous configuration: {backup}",
            flush=True,
        )
        run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "up",
                "-d",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                "300",
                "manager",
            ],
            timeout=360,
        )
        print(
            "Upgrade complete. Running agents are updated; stopped and archived agents remain stopped."
        )
