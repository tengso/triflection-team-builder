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
        "import team_builder.server, team_builder.mcp, plugins.platforms.buzz.adapter",
    )
    prompt_test = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-e",
            "HERMES_HOME=/tmp/hermes-prompt-test",
            "--entrypoint",
            "/opt/hermes/.venv/bin/python",
            hermes,
            "-",
        ],
        input=Path(__file__).with_name("hermes_prompt_refresh.py").read_text(),
        text=True,
        capture_output=True,
        check=False,
    )
    if prompt_test.returncode:
        raise RuntimeError(prompt_test.stdout + prompt_test.stderr)
    print(prompt_test.stdout, flush=True)
    with tempfile.TemporaryDirectory(prefix="team-builder-images-") as temp:
        directory = Path(temp)
        owner, provider = directory / "owner.key", directory / "provider.key"
        owner.write_text(key())
        provider.write_text("not-a-real-provider-key-no-model-calls")
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
                    "http://image-test.local:3310",
                    "--port",
                    "3310",
                    "--model",
                    "openai/gpt-4.1-mini",
                    "--owner-key-file",
                    str(owner),
                    "--provider-key-file",
                    str(provider),
                    "--buzz-image",
                    buzz,
                    "--runtime-image",
                    hermes,
                ),
                flush=True,
            )
            before = json.loads((state / "config.json").read_text())
            print(run(*command), flush=True)
            assert json.loads((state / "config.json").read_text()) == before
            assert before["runtime_image"] == before["images"]["hermes"]
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
