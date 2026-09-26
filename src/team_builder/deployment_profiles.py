"""Operator-defined profiles; agents select references, never credential values."""

import json
import os
import re
import secrets
import time
import uuid
from typing import Annotated

from pydantic import Field, model_validator

from .deployments import Slug, Strict
from .docker import generation
from .storage import private_write

EnvName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")]
Target = Annotated[
    str,
    Field(
        pattern=r"^/(?:app/config|run/application-config)/[a-zA-Z0-9_][a-zA-Z0-9_.-]*$"
    ),
]


class Connection(Strict):
    id: Slug
    host: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$")]
    port: Annotated[int, Field(ge=1, le=65535)]


class Profile(Strict):
    id: Slug
    values: dict[EnvName, str] = Field(default_factory=dict)
    secrets: dict[EnvName, Slug] = Field(default_factory=dict)
    files: dict[Target, Slug] = Field(default_factory=dict)
    required_env: list[EnvName] = Field(default_factory=list)
    connections: Annotated[list[Connection], Field(max_length=8)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def unique(self):
        if set(self.values) & set(self.secrets):
            raise ValueError(
                "A profile variable cannot be both a value and a secret reference"
            )
        if len({c.id for c in self.connections}) != len(self.connections):
            raise ValueError("Duplicate connection check")
        return self


class Profiles:
    def __init__(self, service):
        self.service = service
        self.root = service.root / "profiles"

    def directory(self, application, environment):
        self.service.key(application, environment)
        return self.root / application / environment

    def credential(
        self, application, environment, id, value=None, generate=False, rotate=False
    ):
        directory = self.directory(application, environment)
        self.service.get("app/" + self.service.key(application, environment))
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", id):
            raise ValueError("Invalid credential reference")
        if (value is None) == (not generate) or (
            value is not None
            and (not isinstance(value, str) or not 0 < len(value) <= 262144)
        ):
            raise ValueError("Supply a secret file or request a generated token")
        with self.service.lock:
            self.idle(application, environment)
            path = directory / "credentials" / (id + ".json")
            old = json.loads(path.read_text()) if path.exists() else None
            if old and not rotate:
                if (
                    generate
                    or self.value(application, environment, id, old["version"]) == value
                ):
                    return {"id": id, "version": old["version"], "changed": False}
                raise ValueError("Credential exists; explicitly rotate it")
            version = uuid.uuid4().hex
            private_write(
                directory / "versions" / id / version,
                (secrets.token_urlsafe(48) if generate else value).encode(),
            )
            os.chmod(directory / "versions" / id / version, 0o444)
            private_write(path, {"id": id, "version": version})
            return {"id": id, "version": version, "changed": True}

    def idle(self, application, environment):
        if any(
            j["application"] == application
            and j["environment"] == environment
            and j["state"] in ("queued", "running")
            for j in self.service.db.list("job")
        ):
            raise ValueError(
                "Wait for the active deployment before changing credentials"
            )

    def value(self, application, environment, id, version):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", id) or not re.fullmatch(
            r"[0-9a-f]{32}", version
        ):
            raise ValueError("Invalid credential reference")
        return (
            self.directory(application, environment) / "versions" / id / version
        ).read_text()

    def register(self, application, environment, profile):
        profile = Profile.model_validate(profile).model_dump()
        key = self.service.key(application, environment)
        self.service.get("app/" + key)
        with self.service.lock:
            path = self.directory(application, environment) / (profile["id"] + ".json")
            if path.exists() and json.loads(path.read_text()) != profile:
                raise ValueError("Profile IDs are immutable; register a new profile ID")
            private_write(path, profile)
            public = {
                "id": profile["id"],
                "application": application,
                "environment": environment,
                "variables": sorted(set(profile["values"]) | set(profile["secrets"])),
                "secret_refs": profile["secrets"],
                "file_refs": profile["files"],
                "required_env": profile["required_env"],
                "connection_checks": [c["id"] for c in profile["connections"]],
            }
            self.service.put("profile/" + key + "/" + profile["id"], "profile", public)
            return public

    def list(self, application, environment):
        key = self.service.key(application, environment)
        with self.service.lock:
            profiles = [
                p
                for p in self.service.db.list("profile")
                if p["application"] == application and p["environment"] == environment
            ]
            app = self.service.get("app/" + key)
            preflight = self.service.db.get("preflight/" + key)
        return {
            "profiles": profiles,
            "active": app.get("configuration", {}).get("profile"),
            "preflight": preflight,
        }

    def binding(self, application, environment, profile):
        self.service.get(
            "profile/" + self.service.key(application, environment) + "/" + profile
        )
        p = json.loads(
            (self.directory(application, environment) / (profile + ".json")).read_text()
        )
        versions = {}
        for id in sorted(set(p["secrets"].values()) | set(p["files"].values())):
            path = (
                self.directory(application, environment)
                / "credentials"
                / (id + ".json")
            )
            versions[id] = (
                json.loads(path.read_text())["version"] if path.exists() else None
            )
        return {"profile": profile, "versions": versions}

    def resolve(self, app, binding=None):
        binding = binding if binding is not None else app.get("configuration")
        if not binding:
            return None, {}, {}
        application, environment = app["spec"]["id"], app["spec"]["environment"]
        p = json.loads(
            (
                self.directory(application, environment)
                / (binding["profile"] + ".json")
            ).read_text()
        )
        values = dict(p["values"])
        files = {}
        for target, ref in p["secrets"].items():
            version = binding["versions"].get(ref)
            if version:
                values[target] = self.value(application, environment, ref, version)
        for target, ref in p["files"].items():
            version = binding["versions"].get(ref)
            if version:
                files[target] = (
                    self.directory(application, environment)
                    / "versions"
                    / ref
                    / version
                )
        return p, values, files

    def mounts(self, app):
        _, _, files = self.resolve(app)
        return [
            {
                "Type": "bind",
                "Source": str(self.service.manager.config["host_root"])
                + "/deployments/"
                + str(path.relative_to(self.service.root)),
                "Target": target,
                "ReadOnly": True,
            }
            for target, path in files.items()
        ]

    def preflight(self, application, environment="production", profile=None, live=True):
        key = self.service.key(application, environment)
        app = self.service.get("app/" + key)
        binding = (
            self.binding(application, environment, profile)
            if profile
            else app.get("configuration")
        )
        p, values, files = self.resolve(app, binding)
        if p is None:
            values = self.service.secret_values(app)
        checks = []

        def add(id, ok, kind):
            checks.append(
                {"id": id, "kind": kind, "status": "passed" if ok else "failed"}
            )

        if binding:
            for id, version in binding["versions"].items():
                add(id, bool(version), "credential")
        for service in app["spec"]["services"]:
            merged = {**service["environment"], **values}
            required = set(p["required_env"] if p else [])
            if service.get("health_token_env"):
                required.add(service["health_token_env"])
            for name in sorted(required):
                add(
                    service["id"] + ":" + name,
                    bool(merged.get(name)),
                    "required_variable",
                )
        if p:
            for target in p["files"]:
                add(
                    target,
                    target in files
                    and files[target].is_file()
                    and files[target].stat().st_size > 0,
                    "required_file",
                )
            for connection in p["connections"]:
                if live and all(c["status"] == "passed" for c in checks):
                    add(
                        connection["id"],
                        self.probe(app, connection),
                        "tcp_connectivity",
                    )
                else:
                    checks.append(
                        {
                            "id": connection["id"],
                            "kind": "tcp_connectivity",
                            "status": "not_checked",
                        }
                    )
        result = {
            "application": application,
            "environment": environment,
            "profile": binding["profile"] if binding else None,
            "ready": all(c["status"] == "passed" for c in checks),
            "checks": checks,
            "checked_at": time.time(),
            "note": "TCP probes check reachability, not database credentials, grants or schema. No database writes are performed.",
        }
        self.service.put("preflight/" + key, "preflight", result)
        return result

    def probe(self, app, connection):
        docker = self.service.docker
        identifier = None
        try:
            network = self.service.name(app)
            observed = docker.call("GET", "/networks/" + network, timeout=5)
            if observed:
                if any(
                    observed.get("Labels", {}).get(k) != v
                    for k, v in self.service.labels(app).items()
                ):
                    return False
            else:
                docker.call(
                    "POST",
                    "/networks/create",
                    json={"Name": network, "Labels": self.service.labels(app)},
                    timeout=5,
                )
            image = (
                self.service.manager.config.get("manager_image")
                or self.service.manager.config["runtime_image"]
            )
            script = (
                "import socket; socket.create_connection(("
                + repr(connection["host"])
                + ","
                + str(connection["port"])
                + "),3).close()"
            )
            spec = {
                "Image": image,
                "Entrypoint": ["/opt/hermes/.venv/bin/python", "-c", script],
                "Cmd": [],
                "Labels": {
                    **self.service.labels(app),
                    "io.team-builder.component": "preflight",
                },
                "HostConfig": {
                    "NetworkMode": network,
                    "ReadonlyRootfs": True,
                    "Memory": 64 * 1024 * 1024,
                    "PidsLimit": 32,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "LogConfig": {"Type": "none"},
                },
            }
            identifier = docker.call(
                "POST", "/containers/create", json=spec, timeout=5
            )["Id"]
            docker.call("POST", "/containers/" + identifier + "/start", timeout=5)
            result = docker.call(
                "POST", "/containers/" + identifier + "/wait", timeout=8
            )
            return result.get("StatusCode") == 0
        except Exception:  # noqa: BLE001 -- do not expose Docker diagnostics
            return False
        finally:
            if identifier:
                try:
                    docker.call(
                        "DELETE", "/containers/" + identifier + "?force=true", timeout=5
                    )
                except Exception:  # noqa: BLE001 -- no raw Docker errors
                    return False

    def assert_current(self, plan):
        binding = plan.get("configuration")
        if (
            binding
            and self.binding(
                plan["application"], plan["environment"], binding["profile"]
            )
            != binding
        ):
            raise ValueError(
                "Profile credentials changed; create and approve a new plan"
            )

    def plan(self, application, environment, profile, release=None):
        key = self.service.key(application, environment)
        with self.service.lock:
            app = self.service.get("app/" + key)
            binding = self.binding(application, environment, profile)
            if not all(binding["versions"].values()):
                raise ValueError(
                    "Profile credentials are missing; run preflight for details"
                )
            base = (
                self.service.plan(application, environment, release=release)
                if release
                else {
                    "application": application,
                    "environment": environment,
                    "action": "configure",
                    "release": None,
                    "service": None,
                    "revision": app["revision"],
                    "previous": app["current"],
                    "commit": None,
                    "images": {},
                    "spec_hash": generation(app["spec"]),
                }
            )
            base = {k: v for k, v in base.items() if k not in ("plan_id", "approval")}
            base["configuration"] = binding
            base["configuration_summary"] = self.service.get(
                "profile/" + key + "/" + profile
            )
            id = generation(base)
            self.service.put("plan/" + id, "plan", base)
            return {
                "plan_id": id,
                **base,
                "approval": "Owner approval required for the exact profile and credential versions. No database migrations.",
            }
