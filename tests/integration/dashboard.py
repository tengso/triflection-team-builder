"""Read-only dashboard acceptance against an isolated running Compose stack."""

import json
import time
from pathlib import Path

import httpx


def exercise(state, key):
    config = json.loads((state / "config.json").read_text())
    origin = f"http://127.0.0.1:{config['dashboard']['port']}"
    with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
        assert client.get("/dashboard/").status_code == 200
        assert client.get("/dashboard/api/v1/overview").status_code == 401
        assert (
            client.post(
                "/dashboard/api/v1/login", headers={"Origin": origin}, json={"key": key}
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 45
        while True:
            response = client.get("/dashboard/api/v1/overview")
            assert response.status_code == 200
            value = response.json()
            if value.get("collected_at"):
                break
            if time.monotonic() > deadline:
                raise AssertionError("Dashboard failed to collect its first snapshot")
            time.sleep(1)
        assert not value["stale"], value["errors"]
        assert not value["errors"]
        assert len(value["services"]) == 6
        assert (
            next(s for s in value["services"] if s["id"] == "minio-init")["status"]
            == "completed"
        )
        coa = next(a for a in value["agents"] if a["id"] == "coa")
        assert coa["owner"] == config["owner"]
        office = next(c for c in value["channels"] if c["id"] == "office")
        if coa["state"] == "archived":
            assert coa["gateway"]["buzz"] == "unknown"
            assert office["buzz"]["status"] == "missing"
        else:
            assert coa["gateway"]["responsive"] is True, coa["gateway"]
            assert coa["gateway"]["buzz"] == "connected"
            assert office["buzz"]["status"] == "present"
            assert config["owner"] in [m["pubkey"] for m in office["buzz"]["members"]]
        serialized = json.dumps(value)
        for secret in json.loads((state / "secrets.json").read_text()).values():
            assert secret not in serialized
        assert key not in serialized
        assert client.get("/dashboard/api/v1/logs?component=relay").status_code == 200
        assert (
            client.get("/dashboard/api/v1/logs?component=unrelated").status_code == 400
        )
        assert client.post("/execute", json={}).status_code == 405
        assert client.post("/dashboard/api/v1/restart", json={}).status_code == 405
        for route in ("agents", "services", "channels", "projects", "activity"):
            assert client.get("/dashboard/api/v1/" + route).status_code == 200
    print(
        "PASS: owner-only read-only dashboard, live gateway witness, community readback, secret isolation"
    )


if __name__ == "__main__":
    import sys

    exercise(Path(sys.argv[1]), Path(sys.argv[2]).read_text().strip())
