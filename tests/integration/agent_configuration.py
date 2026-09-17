"""Configuration acceptance on the disposable Image Smoke Test installation."""

import json
import subprocess
import time

import httpx


def exercise(state, key):
    config = json.loads((state / "config.json").read_text())
    assert config["name"] == "Image Smoke Test"
    name = config["project"] + "-agent-coa"
    origin = f"http://127.0.0.1:{config['dashboard']['port']}"
    route = "/dashboard/api/v1/agents/coa/configuration"

    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    def worker_script(script):
        return run("docker", "exec", name, "/opt/hermes/.venv/bin/python", "-c", script)

    def gateway_pid():
        return int(
            worker_script(
                "import json; from pathlib import Path; print(json.loads(Path('/home/hermes/.hermes/gateway_state.json').read_text())['pid'])"
            )
        )

    def container_id():
        return run("docker", "inspect", "--format", "{{.Id}}", name).strip()

    def wait_revision(revision, old_pid=None):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = client.get(route)
            if (
                result.status_code == 200
                and result.json()["applied_revision"] == revision
            ):
                pid = gateway_pid()
                health = subprocess.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "/opt/hermes/.venv/bin/python",
                        "-m",
                        "team_builder.worker",
                        "health",
                    ],
                    capture_output=True,
                    check=False,
                )
                if health.returncode == 0 and pid != old_pid:
                    return pid
            time.sleep(1)
        raise AssertionError("Configuration failed to load into a healthy new gateway")

    # Start a detached app in the disposable agent; no shell payload or model call.
    run(
        "docker",
        "exec",
        name,
        "/opt/hermes/.venv/bin/python",
        "-c",
        "import subprocess; from pathlib import Path; "
        "p=subprocess.Popen(['/opt/hermes/.venv/bin/python','-m','http.server','18999','--bind','127.0.0.1'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);"
        "Path('/work/config-test-app.pid').write_text(str(p.pid))",
    )
    before = container_id()
    pid = gateway_pid()
    with httpx.Client(
        base_url=origin, trust_env=False, timeout=30, headers={"Origin": origin}
    ) as client:
        assert (
            client.post("/dashboard/api/v1/login", json={"key": key}).status_code == 200
        )
        initial = client.get(route).json()
        entry = {
            "id": "review-v1",
            "kind": "skill",
            "name": "Review",
            "content": "Review tests before reporting success.",
        }
        assert client.post("/dashboard/api/v1/catalog", json=entry).status_code == 200
        value = {
            **initial["settings"],
            "soul": "Be clear and concise.",
            "skills": ["review-v1"],
            "tools": ["terminal", "file", "skills"],
        }
        response = client.post(route, json={"expected_revision": 0, "settings": value})
        assert (
            response.status_code == 200 and response.json()["status"] == "starting"
        ), response.text
        pid = wait_revision(1, pid)
        assert container_id() == before
        worker_script(
            "from pathlib import Path; home=Path('/home/hermes/.hermes'); assert (home/'skills/team-managed-review-v1/SKILL.md').is_file(); assert 'Be clear and concise.' in (home/'SOUL.md').read_text()"
        )
        run(
            "docker",
            "exec",
            name,
            "/opt/hermes/.venv/bin/python",
            "-c",
            "import os,urllib.request; from pathlib import Path; "
            "os.kill(int(Path('/work/config-test-app.pid').read_text()),0); "
            "assert urllib.request.urlopen('http://127.0.0.1:18999').status==200",
        )
        assert (
            client.post(
                route, json={"expected_revision": 0, "settings": initial["settings"]}
            ).status_code
            == 409
        )
        # Rollback writes another revision, removes managed skill, and retains app.
        assert (
            client.post(
                route, json={"expected_revision": 1, "settings": initial["settings"]}
            ).status_code
            == 200
        )
        wait_revision(2, pid)
        worker_script(
            "from pathlib import Path; assert not Path('/home/hermes/.hermes/skills/team-managed-review-v1/SKILL.md').exists()"
        )
        assert container_id() == before
        assert len(client.get(route).json()["history"]) == 3
        # Resume setup must preserve versions and container identity.
        run("docker", "restart", config["project"] + "-manager-1")
        time.sleep(2)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if (
                    client.post(
                        "/dashboard/api/v1/login", json={"key": key}
                    ).status_code
                    == 200
                ):
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        wait_revision(2)
        assert container_id() == before
    print(
        "PASS: configuration reload, skill assignment/removal, conflict, rollback, manager restart persistence, and detached app retention"
    )
