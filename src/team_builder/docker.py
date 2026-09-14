"""Small Docker Engine client. Every mutation is restricted to this installation."""

import json
import time
from urllib.parse import quote

import httpx


class Docker:
    def __init__(self, project):
        self.project = project
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds="/var/run/docker.sock"),
            base_url="http://docker",
            timeout=180,
        )

    def call(self, method, path, **kwargs):
        result = self.client.request(method, "/v1.45" + path, **kwargs)
        if result.status_code == 404:
            return None
        if result.status_code not in (200, 201, 204, 304):
            raise RuntimeError(f"Docker {method} failed (HTTP {result.status_code})")
        return result.json() if result.content else {}

    def inspect(self, name):
        data = self.call("GET", "/containers/" + quote(name, safe="") + "/json")
        if (
            data
            and data["Config"].get("Labels", {}).get("io.team-builder.project")
            != self.project
        ):
            raise ValueError("Container is outside this installation")
        return data

    def stop(self, name):
        if self.inspect(name):
            self.call("POST", f"/containers/{quote(name, safe='')}/stop?t=20")

    def restart(self, name):
        observed = self.inspect(name)
        if not observed:
            raise ValueError("Agent container is missing")
        # Use the verified immutable ID, not a name that could be reassigned.
        result = self.call(
            "POST", f"/containers/{quote(observed['Id'], safe='')}/restart?t=20"
        )
        if result is None:
            raise RuntimeError("Agent container disappeared during restart")

    def ensure(self, name, spec, generation):
        observed = self.inspect(name)
        if (
            observed
            and observed["Config"]["Labels"].get("io.team-builder.generation")
            != generation
        ):
            self.stop(name)
            self.call("DELETE", f"/containers/{quote(name, safe='')}")
            observed = None
        if not observed:
            spec["Labels"] = {
                "io.team-builder.project": self.project,
                "io.team-builder.generation": generation,
            }
            self.call(
                "POST", "/containers/create?name=" + quote(name, safe=""), json=spec
            )
        self.call("POST", f"/containers/{quote(name, safe='')}/start")

    def exec(self, name, command):
        if not self.inspect(name):
            raise RuntimeError("Managed container is missing")
        created = self.call(
            "POST",
            f"/containers/{quote(name, safe='')}/exec",
            json={"Cmd": command, "AttachStdout": True, "AttachStderr": True},
        )
        # Detached execution avoids reflecting command output (which may contain credentials).
        self.call("POST", f"/exec/{created['Id']}/start", json={"Detach": True})
        for _ in range(120):
            status = self.call("GET", f"/exec/{created['Id']}/json")
            if not status["Running"]:
                if status["ExitCode"] != 0:
                    raise RuntimeError("Managed container command failed")
                return
            time.sleep(0.5)
        raise RuntimeError("Managed container command timed out")

    def ready(self, name):
        state = (self.inspect(name) or {}).get("State", {})
        return (
            state.get("Running") is True
            and state.get("Health", {}).get("Status") == "healthy"
        )

    def wait_ready(self, name, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready(name):
                return
            time.sleep(2)
        raise RuntimeError(f"Gateway is not ready: {name}; inspect its Docker logs")


def generation(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
