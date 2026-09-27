"""Check published images in an ephemeral Docker installation, without model calls."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from team_builder.cli import INFRA_IMAGES
from team_builder.nostr import key


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def main():
    buzz, hermes = os.environ["BUZZ_IMAGE"], os.environ["HERMES_IMAGE"]
    for image in (*INFRA_IMAGES.values(), buzz, hermes):
        try:
            run("docker", "image", "inspect", image)
        except subprocess.CalledProcessError:
            # Only public image references are used here; show registry errors so
            # clean-host pull failures can be diagnosed without dumping state.
            print("Pulling " + image, flush=True)
            subprocess.run(["docker", "pull", image], check=True)
    run("docker", "run", "--rm", "--entrypoint", "buzz", buzz, "--help")
    run("docker", "run", "--rm", "--entrypoint", "buzz-admin", buzz, "--help")
    run(
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        hermes,
        "-c",
        "import team_builder.server, team_builder.mcp, team_builder.worker",
    )
    run(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/opt/hermes/.venv/bin/hermes-acp",
        hermes,
        "--help",
    )
    run("docker", "run", "--rm", "--entrypoint", "buzz-acp", hermes, "--help")
    harness_images = {
        name: os.environ.get(name.upper() + "_IMAGE")
        for name in ("pi", "codex", "devin")
    }
    for name, image in harness_images.items():
        if not image:
            continue
        try:
            run("docker", "image", "inspect", image)
        except subprocess.CalledProcessError:
            print("Pulling " + image, flush=True)
            subprocess.run(["docker", "pull", image], check=True)
        labels = json.loads(
            run(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                image,
            )
        )
        assert labels.get("io.team-builder.harness") == name, (name, labels)
        run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/opt/team-builder/.venv/bin/python",
            image,
            "-c",
            "import team_builder.worker, team_builder.harness_config",
        )
        run("docker", "run", "--rm", "--entrypoint", "buzz-acp", image, "--help")
        adapter = {"pi": "pi-acp", "codex": "codex-acp", "devin": "devin"}[name]
        run("docker", "run", "--rm", "--entrypoint", adapter, image, "--version")
    with tempfile.TemporaryDirectory(prefix="team-builder-images-") as temp:
        directory = Path(temp)
        owner, provider = directory / "owner.key", directory / "provider.key"
        owner.write_text(key())
        # PROVIDER_KEY_FILE opts into live model calls for the harness checks.
        provider.write_text(
            Path(os.environ["PROVIDER_KEY_FILE"]).read_text().strip()
            if os.environ.get("PROVIDER_KEY_FILE")
            else "not-a-real-provider-key-no-model-calls"
        )
        owner.chmod(0o600)
        provider.chmod(0o600)
        state = directory / "state"
        try:
            command = [
                sys.executable,
                "-m",
                "team_builder.cli",
                "init",
                "--non-interactive",
                "--state-dir",
                str(state),
            ]
            print(
                run(
                    *command,
                    "--name",
                    "Image Smoke Test",
                    "--advertised-url",
                    "http://127.0.0.1:3340",
                    "--internal-url",
                    "http://relay:3000",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    "3310",
                    "--model",
                    os.environ.get("PROVIDER_MODEL", "openai/gpt-4.1-mini"),
                    "--owner-key-file",
                    str(owner),
                    "--provider-key-file",
                    str(provider),
                    "--buzz-image",
                    buzz,
                    "--runtime-image",
                    hermes,
                    *(
                        arg
                        for name, image in harness_images.items()
                        if image
                        for arg in ("--" + name + "-image", image)
                    ),
                ),
                flush=True,
            )
            before = json.loads((state / "config.json").read_text())
            print(run(*command), flush=True)
            assert json.loads((state / "config.json").read_text()) == before
            assert before["runtime_image"] == before["images"]["hermes"]
            from network_tunnel import exercise as exercise_tunnel

            exercise_tunnel(state, owner.read_text().strip())
            internal_script = Path(__file__).with_name("internal_relay.py").read_text()
            internal_test = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    before["project"] + "-manager-1",
                    "/opt/hermes/.venv/bin/python",
                    "-",
                ],
                input=internal_script
                + "\nexercise("
                + repr(owner.read_text().strip())
                + ")\n",
                capture_output=True,
                text=True,
                check=False,
            )
            if internal_test.returncode:
                raise RuntimeError(internal_test.stdout + internal_test.stderr)
            print(internal_test.stdout, flush=True)
            dashboard_key = directory / "dashboard.key"
            print(
                run(
                    sys.executable,
                    "-m",
                    "team_builder.cli",
                    "dashboard",
                    "enable",
                    "--state-dir",
                    str(state),
                    "--key-output",
                    str(dashboard_key),
                ),
                flush=True,
            )
            print(
                run(
                    sys.executable,
                    str(Path(__file__).with_name("dashboard.py")),
                    str(state),
                    str(dashboard_key),
                ),
                flush=True,
            )
            from agent_configuration import exercise as exercise_configuration

            exercise_configuration(state, dashboard_key.read_text().strip())
            from agent_proxy import exercise as exercise_proxy

            exercise_proxy(state)
            # Harness checks run first: the management exercise archives COA and deletes the office.
            if os.environ.get("EXERCISE_HARNESSES") == "1":
                script = Path(__file__).with_name("harnesses.py").read_text()
                script += "\nexercise(" + repr(owner.read_text().strip()) + ")\n"
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        *(
                            arg
                            for name in (
                                "HARNESS_EXPECT_FAILURE",
                                "TEAM_BUILDER_DEVIN_CREDENTIAL",
                                "APPLICATION_IMAGE",
                            )
                            if os.environ.get(name)
                            for arg in ("-e", f"{name}={os.environ[name]}")
                        ),
                        before["project"] + "-manager-1",
                        "/opt/hermes/.venv/bin/python",
                        "-",
                    ],
                    check=False,
                    input=script,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                print(result.stdout, flush=True)
            if os.environ.get("EXERCISE_MANAGEMENT") == "1":
                script = Path(__file__).with_name("community_operations.py").read_text()
                script += "\nexercise(" + repr(owner.read_text()) + ")\n"
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        before["project"] + "-manager-1",
                        "/opt/hermes/.venv/bin/python",
                        "-",
                    ],
                    check=False,
                    input=script,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    # Script assertions contain only sanitized operation results.
                    raise RuntimeError(result.stdout + result.stderr)
                print(result.stdout, flush=True)
            print(
                run(
                    sys.executable,
                    str(Path(__file__).with_name("dashboard.py")),
                    str(state),
                    str(dashboard_key),
                ),
                flush=True,
            )
            print(
                "PASS: published image bootstrap, gateway readiness, and identity-preserving resume",
                flush=True,
            )
        except subprocess.CalledProcessError as exc:
            # CLI output deliberately redacts secrets. Do not dump Compose config.
            print(exc.stdout, exc.stderr, file=sys.stderr)
            raise
        finally:
            config = state / "config.json"
            if config.exists():
                project = json.loads(config.read_text())["project"]
                assert project.startswith("tb-")
                subprocess.run(
                    ["docker", "stop", project + "-manager-1"],
                    capture_output=True,
                    check=False,
                )
                containers = run(
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=io.team-builder.project=" + project,
                ).split()
                if containers:
                    run("docker", "rm", "-f", *containers)
                # Only this generated, temporary test installation is disposable.
                if (state / "compose.yaml").exists():
                    run(
                        "docker",
                        "compose",
                        "-f",
                        str(state / "compose.yaml"),
                        "down",
                        "--volumes",
                    )
                # Agent workspaces are owned by container UID 10000.
                subprocess.run(
                    ["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(state)],
                    check=True,
                )


if __name__ == "__main__":
    main()
