"""Upgrade only the management/agent runtime, preserving community data."""

import fcntl
import json
import platform
import uuid
from pathlib import Path

import yaml

from .storage import private_write


def upgrade(
    state_dir, image=None, *, manager_only=False, harness=None, harness_image=None
):
    from .cli import (
        PUBLISHED_IMAGES,
        prepare_runtime,
        require_buzz_acp,
        require_harness_image,
        resolve_image,
        run,
    )

    if platform.system() != "Linux":
        raise ValueError("Run upgrades on the Linux host of the installation")
    if harness:
        if manager_only or image:
            raise ValueError(
                "--harness cannot be combined with --manager-only or --runtime-image"
            )
        if not harness_image:
            raise ValueError("--image is required with --harness")
    root = Path(state_dir).expanduser().resolve()
    if not (root / "config.json").is_file():
        raise ValueError("Installation not found")
    with (root / "init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((root / "config.json").read_text())
        if config["host_root"] != str(root):
            raise ValueError("Installation was moved; restore its recorded state path")
        if harness:
            resolved = resolve_image(harness_image)
            require_harness_image(resolved, harness)
        else:
            runtime = prepare_runtime(
                resolve_image(image or PUBLISHED_IMAGES["hermes"])
            )
        compose_path = root / "compose.yaml"
        document = yaml.safe_load(compose_path.read_text())
        backup = root / "upgrades" / str(uuid.uuid4())
        private_write(backup / "config.json", (root / "config.json").read_bytes())
        private_write(backup / "compose.yaml", compose_path.read_bytes())
        if harness:
            config.setdefault("images", {})[harness] = resolved
            private_write(root / "config.json", config)
            print(
                f"Installed {harness} harness image; restarting the manager. Previous configuration: {backup}",
                flush=True,
            )
            compose = ["docker", "compose", "-f", str(compose_path)]
            run([*compose, "restart", "manager"], timeout=120)
            run(
                [
                    *compose,
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
                f"{harness} harness image updated; running {harness} agents pick it up on restart."
            )
            return
        if manager_only:
            try:
                require_buzz_acp(config["runtime_image"])
            except ValueError:
                raise ValueError(
                    "Workers still run a pre-0.7.0 runtime that cannot load buzz-acp "
                    "bundles; run a full upgrade without --manager-only"
                ) from None
            config["manager_image"] = runtime
        else:
            config["runtime_image"] = runtime
            config.pop("manager_image", None)
            config["images"]["hermes"] = runtime
            for name in ("pi", "codex", "devin"):
                # Harness images are resolved lazily on first agent start.
                if not config["images"].get(name) and PUBLISHED_IMAGES.get(name):
                    config["images"][name] = PUBLISHED_IMAGES[name]
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
            "Management upgrade complete; worker containers retained."
            if manager_only
            else "Upgrade complete. Running agents are updated; stopped and archived agents remain stopped."
        )
