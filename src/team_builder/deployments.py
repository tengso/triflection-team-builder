"""Scoped host-container deployments with immutable plans and durable jobs.

Only the local operator registers executable specifications, secrets and releases.
Agents can inspect and propose those releases; signed owner approval queues work.
"""

import hashlib
import hmac
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .docker import Docker, generation
from .storage import Registry, private_write

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Digest = Annotated[
    str, Field(pattern=r"^(?:[a-z0-9][a-z0-9./:_-]+@)?sha256:[0-9a-f]{64}$")
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Service(Strict):
    id: Slug
    command: Annotated[list[str], Field(min_length=1, max_length=32)]
    port: Annotated[int, Field(ge=1, le=65535)]
    host_port: Annotated[int, Field(ge=1024, le=65535)] | None = None
    health_path: Annotated[str, Field(pattern=r"^/[a-zA-Z0-9/_-]*$", max_length=120)]
    environment: dict[str, str] = {}
    health_token_env: Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")] | None = None
    data_path: (
        Annotated[str, Field(pattern=r"^/(?:app/)?data(?:/[a-z0-9_-]+)*$")] | None
    ) = None
    memory_mb: Annotated[int, Field(ge=128, le=8192)] = 1024


class Application(Strict):
    id: Slug
    environment: Literal["staging", "production"] = "production"
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    services: Annotated[list[Service], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique(self):
        ids = [s.id for s in self.services]
        ports = [s.host_port for s in self.services if s.host_port]
        if len(set(ids)) != len(ids) or len(set(ports)) != len(ports):
            raise ValueError("Duplicate service or host port")
        return self


class Release(Strict):
    id: Slug
    application: Slug
    environment: Literal["staging", "production"] = "production"
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    images: dict[Slug, Digest]


def token(secrets, agent=None):
    domain = (
        "deployments/operator/v1" if agent is None else "deployments/agent/v1/" + agent
    )
    return hmac.new(
        secrets["admin"].encode(), domain.encode(), hashlib.sha256
    ).hexdigest()


class Deployments:
    def __init__(self, manager, docker=None):
        self.manager = manager
        self.root = manager.root / "deployments"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.db = Registry(self.root / "registry.sqlite3")
        self.lock = threading.RLock()
        self.docker = docker or Docker(manager.config["project"])
        self.snapshot_lock = threading.Lock()
        self.snapshot = {
            "applications": [],
            "operations": [],
            "collected_at": None,
            "stale": True,
        }
        self.stop = threading.Event()

    def key(self, app, environment):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", app) or environment not in (
            "production",
            "staging",
        ):
            raise ValueError("Invalid application or environment")
        return app + "/" + environment

    def get(self, key):
        with self.lock:
            value = self.db.get(key)
        if value is None:
            raise ValueError("Unknown deployment resource")
        return value

    def put(self, key, kind, value):
        with self.lock:
            self.db.put(key, kind, value)

    def register(self, application, secrets=None, files=None):
        app = Application.model_validate(application).model_dump()
        key = self.key(app["id"], app["environment"])
        with self.lock:
            current = self.db.get("app/" + key)
            if current and current["spec"] != app:
                raise ValueError(
                    "Application specification is immutable; register a new environment/application ID"
                )
            writes = []
            if secrets is not None:
                if not isinstance(secrets, dict) or any(
                    not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k)
                    or not isinstance(v, str)
                    or len(v) > 16384
                    for k, v in secrets.items()
                ):
                    raise ValueError("Invalid application secret map")
                path = self.root / (key.replace("/", "-") + ".secrets.json")
                if not path.exists() or json.loads(path.read_text()) != secrets:
                    writes.append((path, secrets))
            if files is not None:
                if (
                    not isinstance(files, dict)
                    or set(files) - {"users.yaml"}
                    or any(
                        not isinstance(v, str) or len(v) > 262144
                        for v in files.values()
                    )
                ):
                    raise ValueError(
                        "Only a bounded users.yaml credential file is supported"
                    )
                for name, content in files.items():
                    path = self.root / key.replace("/", "-") / name
                    if not path.exists() or path.read_text() != content:
                        writes.append((path, content.encode()))
            if writes and current:
                if any(
                    j["application"] == app["id"]
                    and j["environment"] == app["environment"]
                    and j["state"] in ("queued", "running")
                    for j in self.db.list("job")
                ):
                    raise ValueError(
                        "Wait for the active deployment before changing credentials"
                    )
                current["revision"] += 1
                # Invalidate approvals before writing any new configuration.
                self.db.put("app/" + key, "application", current)
            for path, content in writes:
                private_write(path, content)
            self.db.put(
                "app/" + key,
                "application",
                current
                or {"spec": app, "current": None, "previous": None, "revision": 0},
            )
        return {
            "application": app["id"],
            "environment": app["environment"],
            "registered": True,
            "redeploy_required": bool(writes and current and current["current"]),
        }

    def release(self, release):
        release = Release.model_validate(release).model_dump()
        key = self.key(release["application"], release["environment"])
        app = self.get("app/" + key)
        if set(release["images"]) != {s["id"] for s in app["spec"]["services"]}:
            raise ValueError("Release must pin one image per service")
        with self.lock:
            old = self.db.get("release/" + key + "/" + release["id"])
            if old and old != release:
                raise ValueError("Release ID is immutable")
            self.db.put("release/" + key + "/" + release["id"], "release", release)
        return release

    def grant(self, agent, application, environment="production", allowed=True):
        self.get("app/" + self.key(application, environment))
        with self.manager.lock:
            item = self.manager.resource(agent, "agent")
            if item["state"] == "archived":
                raise ValueError("Agent is archived")
            rights = set(item.get("deployments", []))
            key = self.key(application, environment)
            rights.add(key) if allowed else rights.discard(key)
            item["deployments"] = sorted(rights)
            self.manager.save_agent(item)
            # Configuration-only reconciliation keeps detached UAT processes alive.
            from .runtime import write_agent_files

            write_agent_files(
                self.manager.root, self.manager.config, self.manager.secrets, item
            )
            if item["state"] == "running":
                self.manager.docker.restart(self.manager.name(item))
        return {"agent": agent, "applications": sorted(rights)}

    def authorized(self, agent, key):
        with self.manager.lock:
            item = self.manager.resource(agent, "agent")
            if item["state"] != "running" or key not in item.get("deployments", []):
                raise ValueError("Agent has no access to this deployment")

    def plan(
        self,
        application,
        environment="production",
        action="deploy",
        release=None,
        service=None,
    ):
        key = self.key(application, environment)
        with self.lock:
            app = self.get("app/" + key)
            if action not in ("deploy", "restart", "rollback"):
                raise ValueError("Unsupported deployment action")
            if action == "rollback":
                release = app["previous"]
            elif action == "restart":
                release = app["current"]
            if not release:
                raise ValueError("No release available for this operation")
            target = self.get("release/" + key + "/" + release)
            if service is not None and (
                action != "restart" or service not in target["images"]
            ):
                raise ValueError("Only restart accepts a known service")
            plan = {
                "application": application,
                "environment": environment,
                "action": action,
                "release": release,
                "service": service,
                "revision": app["revision"],
                "previous": app["current"],
                "commit": target["commit"],
                "images": target["images"],
                "spec_hash": generation(app["spec"]),
            }
            identifier = generation(plan)
            self.db.put("plan/" + identifier, "plan", plan)
        return {
            "plan_id": identifier,
            **plan,
            "approval": "Owner approval required. Database migrations are not performed; rollback restores images only.",
        }

    def enqueue(self, plan_id, source="local-operator", actor=None):
        with self.lock:
            plan = self.get("plan/" + plan_id)
            prior = self.db.get("job/" + plan_id)
            if prior:
                return self.public_job(prior)
            key = self.key(plan["application"], plan["environment"])
            app = self.get("app/" + key)
            if (
                app["revision"] != plan["revision"]
                or generation(app["spec"]) != plan["spec_hash"]
            ):
                raise ValueError("Deployment changed; create and approve a new plan")
            if any(
                j["application"] == plan["application"]
                and j["environment"] == plan["environment"]
                and j["state"] in ("queued", "running")
                for j in self.db.list("job")
            ):
                raise ValueError("A deployment is already active for this environment")
            now = time.time()
            job = {
                **plan,
                "id": plan_id,
                "state": "queued",
                "source": source,
                "actor": actor or self.manager.config.get("owner", "local-operator"),
                "created_at": now,
                "updated_at": now,
                "events": [
                    {"timestamp": now, "message": "Owner-authorized operation queued"}
                ],
            }
            self.db.put("job/" + plan_id, "job", job)
        return self.public_job(job)

    @staticmethod
    def public_job(job):
        return {
            k: job[k]
            for k in (
                "id",
                "application",
                "environment",
                "action",
                "release",
                "commit",
                "service",
                "state",
                "created_at",
                "updated_at",
                "events",
                "source",
                "actor",
            )
        }

    def event(self, job, message, state=None):
        job["updated_at"] = time.time()
        if state:
            job["state"] = state
        job["events"] = (
            job["events"] + [{"timestamp": job["updated_at"], "message": message}]
        )[-100:]
        self.put("job/" + job["id"], "job", job)

    def name(self, app, service=None):
        spec = app["spec"]
        return (
            self.manager.config["project"]
            + "-app-"
            + spec["id"]
            + "-"
            + spec["environment"]
            + ("-" + service if service else "")
        )

    def labels(self, app):
        return {
            "io.team-builder.project": self.manager.config["project"],
            "io.team-builder.application": app["spec"]["id"],
            "io.team-builder.environment": app["spec"]["environment"],
        }

    def inspect(self, app, service, docker=None):
        observed = (docker or self.docker).inspect(self.name(app, service))
        if observed and any(
            observed["Config"].get("Labels", {}).get(k) != v
            for k, v in self.labels(app).items()
        ):
            raise ValueError("Container does not belong to this application")
        return observed

    def network(self, app):
        name = self.name(app)
        labels = self.labels(app)
        found = self.docker.call("GET", "/networks/" + name)
        if found:
            if any(found.get("Labels", {}).get(k) != v for k, v in labels.items()):
                raise ValueError("Network ownership mismatch")
        else:
            self.docker.call(
                "POST", "/networks/create", json={"Name": name, "Labels": labels}
            )
        return name

    def secret_values(self, app):
        key = self.key(app["spec"]["id"], app["spec"]["environment"])
        path = self.root / (key.replace("/", "-") + ".secrets.json")
        return json.loads(path.read_text()) if path.exists() else {}

    def install(self, app, release):
        network = self.network(app)
        for service in app["spec"]["services"]:
            image = release["images"][service["id"]]
            if not self.docker.call(
                "GET", "/images/" + quote(image, safe="") + "/json"
            ):
                if "@sha256:" not in image:
                    raise ValueError(
                        "Pinned local image is unavailable; register a registry digest or load the image"
                    )
                # Pull output is never reflected to clients or logs.
                response = self.docker.client.post(
                    "/v1.45/images/create", params={"fromImage": image}, timeout=300
                )
                if response.status_code != 200 or any(
                    json.loads(line).get("error")
                    for line in response.text.splitlines()
                    if line
                ):
                    raise RuntimeError("Pinned image pull failed")
            if not self.docker.call(
                "GET", "/images/" + quote(image, safe="") + "/json"
            ):
                raise RuntimeError("Pinned image unavailable after pull")
        for service in app["spec"]["services"]:
            sid = service["id"]
            self.inspect(app, sid)
            mounts = []
            if service["data_path"]:
                volume = self.name(app, sid) + "-data"
                found = self.docker.call("GET", "/volumes/" + volume)
                if found and any(
                    found.get("Labels", {}).get(k) != v
                    for k, v in self.labels(app).items()
                ):
                    raise ValueError("Volume ownership mismatch")
                if not found:
                    self.docker.call(
                        "POST",
                        "/volumes/create",
                        json={"Name": volume, "Labels": self.labels(app)},
                    )
                mounts = [
                    {"Type": "volume", "Source": volume, "Target": service["data_path"]}
                ]
            key = self.key(app["spec"]["id"], app["spec"]["environment"])
            credential_file = self.root / key.replace("/", "-") / "users.yaml"
            if credential_file.is_file():
                mounts.append(
                    {
                        "Type": "bind",
                        "Source": str(self.manager.config["host_root"])
                        + "/deployments/"
                        + key.replace("/", "-")
                        + "/users.yaml",
                        "Target": "/app/config/users.yaml",
                        "ReadOnly": True,
                    }
                )
            port = str(service["port"]) + "/tcp"
            environment = {**service["environment"], **self.secret_values(app)}
            spec = {
                "Image": release["images"][sid],
                "Cmd": service["command"],
                "Labels": self.labels(app),
                "Env": [k + "=" + v for k, v in sorted(environment.items())],
                "ExposedPorts": {port: {}},
                "Healthcheck": {
                    "Test": [
                        "CMD",
                        "python",
                        "-c",
                        "import urllib.request,os; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:"
                        + str(service["port"])
                        + service["health_path"]
                        + "',headers="
                        + (
                            "{'Authorization':'Bearer '+os.environ["
                            + repr(service["health_token_env"])
                            + "]}"
                            if service["health_token_env"]
                            else "{}"
                        )
                        + "),timeout=3).read(1)",
                    ],
                    "Interval": 10_000_000_000,
                    "Timeout": 5_000_000_000,
                    "Retries": 3,
                    "StartPeriod": 20_000_000_000,
                },
                "HostConfig": {
                    "NetworkMode": network,
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "Init": True,
                    "Memory": service["memory_mb"] * 1048576,
                    "PidsLimit": 256,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Mounts": mounts,
                    "LogConfig": {
                        "Type": "json-file",
                        "Config": {"max-size": "10m", "max-file": "3"},
                    },
                    "PortBindings": {
                        port: [
                            {
                                "HostIp": self.manager.config.get("bind", "0.0.0.0"),
                                "HostPort": str(service["host_port"]),
                            }
                        ]
                    }
                    if service["host_port"]
                    else {},
                },
                "NetworkingConfig": {"EndpointsConfig": {network: {"Aliases": [sid]}}},
            }
            self.docker.ensure(
                self.name(app, sid), spec, generation([release["id"], spec])
            )

    def wait_ready(self, app, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(
                (self.inspect(app, s["id"]) or {})
                .get("State", {})
                .get("Health", {})
                .get("Status")
                == "healthy"
                for s in app["spec"]["services"]
            ):
                return
            time.sleep(2)
        raise RuntimeError("Application readiness timed out")

    def run_job(self, job):
        key = self.key(job["application"], job["environment"])
        app = self.get("app/" + key)
        self.event(
            job, "Applying pinned release; persistent volumes are retained", "running"
        )
        try:
            if job["action"] == "restart":
                for s in app["spec"]["services"]:
                    if job["service"] and s["id"] != job["service"]:
                        continue
                    if not self.inspect(app, s["id"]):
                        raise ValueError("Production service is missing")
                    self.docker.call(
                        "POST",
                        "/containers/" + self.name(app, s["id"]) + "/restart?t=20",
                    )
            else:
                release = self.get("release/" + key + "/" + job["release"])
                self.install(app, release)
            self.event(job, "Waiting for all application health checks")
            self.wait_ready(app)
            # Reconciliation is repeatable after a crash between Docker and DB writes.
            app.update(
                current=job["release"],
                previous=job["previous"]
                if job["action"] != "restart"
                else app["previous"],
                revision=job["revision"] + 1,
            )
            self.put("app/" + key, "application", app)
            self.event(job, "All service health checks passed", "succeeded")
        except Exception:  # noqa: BLE001 -- never expose Docker responses or credentials
            self.event(job, "Operation failed; inspecting the previous release")
            if job["previous"] and job["action"] != "restart":
                try:
                    self.install(
                        app, self.get("release/" + key + "/" + job["previous"])
                    )
                    self.wait_ready(app)
                    app.update(current=job["previous"], revision=job["revision"] + 1)
                    self.put("app/" + key, "application", app)
                    self.event(
                        job,
                        "Previous images restored and healthy; database contents were not rolled back",
                        "rolled_back",
                    )
                    return
                except Exception:  # noqa: BLE001 -- keep recovery failure sanitized
                    self.event(
                        job, "Automatic recovery failed; operator intervention required"
                    )
            # Even failure consumes the revision so an old plan cannot be reused.
            app["revision"] = job["revision"] + 1
            self.put("app/" + key, "application", app)
            self.event(
                job,
                "Service readiness not confirmed; inspect service diagnostics",
                "failed",
            )

    def read(self):
        with self.snapshot_lock:
            snap = dict(self.snapshot)
        snap["stale"] = (
            not snap["collected_at"] or time.time() - snap["collected_at"] > 15
        )
        return snap

    def observe(self):
        started = time.time()
        with self.lock:
            apps, jobs = self.db.list("application"), self.db.list("job")
        rows = []

        def collect(app):
            spec = app["spec"]
            row = {
                "id": spec["id"],
                "environment": spec["environment"],
                "repository": spec["repository"],
                "current": app["current"],
                "previous": app["previous"],
                "services": [],
            }
            docker = Docker(self.manager.config["project"])
            docker.client.timeout = httpx.Timeout(2)
            try:
                for s in spec["services"]:
                    try:
                        container = self.inspect(app, s["id"], docker) or {}
                        state = container.get("State", {})
                        status = {
                            "status": state.get("Status", "missing"),
                            "health": state.get("Health", {}).get("Status", "unknown"),
                            "started_at": state.get("StartedAt"),
                            "restart_count": container.get("RestartCount"),
                            "image": container.get("Image"),
                        }
                    except Exception:  # noqa: BLE001 -- observation failures must not stop snapshots
                        status = {"status": "unavailable", "health": "unknown"}
                    row["services"].append(
                        {"id": s["id"], "host_port": s["host_port"], **status}
                    )
            finally:
                docker.client.close()
            return row

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(collect, apps))
        with self.snapshot_lock:
            self.snapshot = {
                "applications": rows,
                "operations": [
                    self.public_job(j)
                    for j in sorted(jobs, key=lambda j: j["updated_at"], reverse=True)[
                        :100
                    ]
                ],
                "collected_at": started,
            }

    def logs(self, application, environment, service):
        app = self.get("app/" + self.key(application, environment))
        if service not in {s["id"] for s in app["spec"]["services"]}:
            raise ValueError("Unknown application service")
        from .dashboard_observe import Engine, Redactor

        # Existing diagnostic allowlist excludes transcripts, payloads and arbitrary output.
        redactor = Redactor(self.manager.root)
        redactor.values += [v for v in self.secret_values(app).values() if v]
        redactor.values += [
            v
            for s in app["spec"]["services"]
            for v in s["environment"].values()
            if len(v) >= 4
        ]
        engine = Engine(self.manager.config["project"])
        try:
            self.inspect(app, service, engine)
            return {
                "entries": redactor.clean(engine.logs(self.name(app, service))),
                "note": "Last 100 log lines; recognized diagnostic summaries only. Request payloads, commands and unrecognized lines are excluded.",
            }
        finally:
            engine.client.close()

    def start(self):
        def worker():
            while not self.stop.is_set():
                try:
                    with self.lock:
                        jobs = [
                            j
                            for j in self.db.list("job")
                            if j["state"] in ("queued", "running")
                        ]
                    for job in jobs:
                        self.run_job(job)
                except Exception:  # noqa: BLE001 -- durable jobs retry after transient storage failures
                    print(
                        "Deployment worker pending; retrying durable jobs", flush=True
                    )
                self.stop.wait(2)

        def observer():
            while not self.stop.is_set():
                try:
                    self.observe()
                except Exception:  # noqa: BLE001 -- stale snapshot explicitly reported
                    print("Deployment observations unavailable", flush=True)
                self.stop.wait(5)

        threading.Thread(target=worker, daemon=True).start()
        threading.Thread(target=observer, daemon=True).start()
