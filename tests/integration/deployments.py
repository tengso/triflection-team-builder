"""Run on Linux with a local app image; creates and removes only its own resources."""

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from team_builder.deployments import Deployments
from team_builder.docker import Docker


def run():
    image = os.environ["APPLICATION_IMAGE"]
    project = "tb-deploy-test-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        manager = SimpleNamespace(
            root=Path(directory),
            config={"project": project, "host_root": directory, "bind": "127.0.0.1"},
            lock=threading.RLock(),
        )
        docker = Docker(project)
        deployments = Deployments(manager, docker)
        application = {
            "id": "acceptance",
            "environment": "staging",
            "repository": "test/isolated",
            "services": [
                {
                    "id": "api",
                    "command": [
                        "python",
                        "-m",
                        "uvicorn",
                        "modules.research.crm.data_api_main:app",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "8002",
                    ],
                    "port": 8002,
                    "health_path": "/health",
                    "health_token_env": "CRM_REST_TOKEN",
                    "data_path": "/app/data",
                }
            ],
        }
        try:
            deployments.register(
                application, secrets={"CRM_REST_TOKEN": "isolated-health-only"}
            )
            deployments.release(
                {
                    "id": "r1",
                    "application": "acceptance",
                    "environment": "staging",
                    "commit": "a" * 40,
                    "images": {"api": image},
                }
            )
            plan = deployments.plan("acceptance", "staging", release="r1")
            deployments.enqueue(plan["plan_id"])
            deployments = Deployments(
                manager, docker
            )  # Interrupted queue survives manager restart.
            deployments.run_job(deployments.get("job/" + plan["plan_id"]))
            assert deployments.get("job/" + plan["plan_id"])["state"] == "succeeded"
            app = deployments.get("app/acceptance/staging")
            original = deployments.inspect(app, "api")["Id"]
            docker.exec(
                deployments.name(app, "api"),
                [
                    "python",
                    "-c",
                    "from pathlib import Path; Path('/app/data/witness').write_text('retained')",
                ],
            )
            assert deployments.enqueue(plan["plan_id"])["state"] == "succeeded"
            restart = deployments.plan(
                "acceptance", "staging", action="restart", service="api"
            )
            deployments.enqueue(restart["plan_id"])
            deployments.run_job(deployments.get("job/" + restart["plan_id"]))
            assert deployments.get("job/" + restart["plan_id"])["state"] == "succeeded"
            assert deployments.inspect(app, "api")["Id"] == original
            deployments.release(
                {
                    "id": "r2",
                    "application": "acceptance",
                    "environment": "staging",
                    "commit": "b" * 40,
                    "images": {"api": image},
                }
            )
            new = deployments.plan("acceptance", "staging", release="r2")
            deployments.enqueue(new["plan_id"])
            deployments.run_job(deployments.get("job/" + new["plan_id"]))
            assert deployments.get("job/" + new["plan_id"])["state"] == "succeeded"
            assert deployments.inspect(app, "api")["Id"] != original
            rollback = deployments.plan("acceptance", "staging", action="rollback")
            deployments.enqueue(rollback["plan_id"])
            deployments.run_job(deployments.get("job/" + rollback["plan_id"]))
            assert deployments.get("app/acceptance/staging")["current"] == "r1"
            docker.exec(
                deployments.name(app, "api"),
                [
                    "python",
                    "-c",
                    "from pathlib import Path; assert Path('/app/data/witness').read_text()=='retained'",
                ],
            )
            deployments.observe()
            snapshot = deployments.read()
            assert snapshot["applications"][0]["services"][0]["health"] == "healthy"
            logs = deployments.logs("acceptance", "staging", "api")
            assert "isolated-health-only" not in json.dumps([snapshot, logs])
            assert logs["entries"]
            print(
                "PASS: deploy, persistent queue, deduplication, restart, release update, rollback, retained data, snapshots and sanitized logs"
            )
        finally:
            app = deployments.get("app/acceptance/staging")
            name = deployments.name(app, "api")
            if docker.inspect(name):
                docker.stop(name)
                docker.call("DELETE", "/containers/" + name)
            docker.call("DELETE", "/volumes/" + name + "-data")
            docker.call("DELETE", "/networks/" + deployments.name(app))


if __name__ == "__main__":
    run()
