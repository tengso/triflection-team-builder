"""Release setup that agents propose and the owner approves; manager-run CI imports.

Agents author application specifications, profiles, dependency attachments, CI
import settings, access grants and release policies. Nothing here executes until
the owner replies `approve` to the agent's frozen proposal; the manager validates
every payload again at execution. Supplied secret values stay operator-only.
"""

import json
import time
from urllib.parse import quote

from .deployment_profiles import Profile
from .deployments import Application
from .models import RELEASE_CHANGES
from .release_automation import Policy

APP_SCOPED = {"register_profile", "generate_credential", "attach_dependency"}
IMPORT_INTERVAL = 120


def application_of(op):
    if op["action"] == "register_application":
        return op["spec"].get("id")
    if op["action"] == "configure_release_policy":
        return op["policy"].get("application")
    return op.get("application")


def validate_payload(op):
    """Reject malformed specifications at proposal time, not after approval."""
    action = op["action"]
    if action == "register_application":
        Application.model_validate(op["spec"])
    elif action == "register_profile":
        Profile.model_validate(op["profile"])
    elif action == "configure_release_policy":
        Policy.model_validate(op["policy"])


def validate_operations(operations):
    """Typed validation with per-operation context an agent can act on."""
    from pydantic import ValidationError

    from .deployment_api import validation_summary
    from .models import validate

    if not isinstance(operations, list):
        raise ValueError("operations must be a JSON list of operation objects")  # noqa: TRY004
    for index, op in enumerate(operations):
        if not isinstance(op, dict) or "action" not in op:
            raise ValueError(
                f"operations[{index}] needs an 'action' key (e.g. register_application); see the release runbook skill for exact shapes"
            )
    try:
        operations = validate(operations)
    except ValidationError as exc:
        raise ValueError(
            "Invalid operations: " + validation_summary(exc, "operations.")
        ) from None
    for index, op in enumerate(operations):
        try:
            validate_payload(op)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid operations[{index}] {op['action']}: "
                + validation_summary(exc)
                + ". See the release runbook skill for the exact JSON shape."
            ) from None
    return operations


def check_scope(service, agent_id, operations):
    """Limit an agent's proposals to applications it operates or introduces."""
    agent = service.manager.resource(agent_id, "agent")
    if agent["state"] != "running":
        raise ValueError("Agent is not running")
    grants = set(agent.get("deployments", []))
    granted_apps = {g.split("/")[0] for g in grants}
    new_apps = set()
    for op in operations:
        if op["action"] not in RELEASE_CHANGES:
            raise ValueError("Agents may propose only release configuration changes")
        app = application_of(op)
        if op["action"] == "register_application":
            existing = any(
                service.db.get("app/" + app + "/" + env)
                for env in ("staging", "production")
            )
            if existing and app not in granted_apps and app not in new_apps:
                raise ValueError("Application exists outside your deployment scope")
            if not existing:
                if not agent.get("release_agent") and not grants:
                    raise ValueError("Only release agents may propose new applications")
                new_apps.add(app)
            continue
        if app in new_apps:
            continue
        if op["action"] in APP_SCOPED:
            if app + "/" + op["environment"] not in grants:
                raise ValueError("Change is outside your deployment scope")
        elif app not in granted_apps:
            raise ValueError("Change is outside your deployment scope")
    return operations


def apply_change(service, op):
    action = op["action"]
    if action == "register_application":
        return service.register(op["spec"])
    if action == "register_profile":
        return service.profiles.register(
            op["application"], op["environment"], op["profile"]
        )
    if action == "generate_credential":
        result = service.profiles.credential(
            op["application"],
            op["environment"],
            op["id"],
            generate=True,
            rotate=op.get("rotate", False),
        )
        return {k: v for k, v in result.items() if k != "version"}
    if action == "attach_dependency":
        return attach(
            service,
            op["application"],
            op["environment"],
            op["container"],
            op["alias"],
        )
    if action == "configure_release_sync":
        return configure_sync(service, op)
    if action == "configure_release_policy":
        return service.automation.configure(op["policy"])
    if action == "configure_release_agent":
        return release_agent(service, op["agent"], op.get("enabled", True))
    raise ValueError("Unknown release change")


def release_agent(service, agent_id, enabled):
    manager = service.manager
    with manager.lock:
        item = manager.resource(agent_id, "agent")
        if item["state"] == "archived":
            raise ValueError("Agent is archived")
        item["release_agent"] = bool(enabled)
        manager.save_agent(item)
        from .runtime import python_for, write_agent_files

        write_agent_files(manager.root, manager.config, manager.secrets, item)
        if item["state"] == "running":
            manager.docker.restart(manager.name(item), python_for(item))
    return {"agent": agent_id, "release_agent": bool(enabled)}


def connect(service, network, container, alias):
    observed = service.docker.call(
        "GET", "/containers/" + quote(container, safe="") + "/json"
    )
    if not observed:
        raise ValueError("Dependency container not found on this host")
    labels = observed.get("Config", {}).get("Labels") or {}
    if any(k.startswith("io.team-builder.") for k in labels):
        raise ValueError("Team Builder containers cannot be attached as dependencies")
    if observed.get("HostConfig", {}).get("Privileged") or any(
        "docker.sock" in (m.get("Source") or "") for m in observed.get("Mounts", [])
    ):
        raise ValueError("Privileged or Docker-socket containers cannot be attached")
    endpoint = observed.get("NetworkSettings", {}).get("Networks", {}).get(network)
    if endpoint and alias in (endpoint.get("Aliases") or []):
        return False
    if endpoint:
        # Only this application network changes; other networks keep the container.
        service.docker.call(
            "POST",
            "/networks/" + quote(network, safe="") + "/disconnect",
            json={"Container": observed["Id"]},
        )
    service.docker.call(
        "POST",
        "/networks/" + quote(network, safe="") + "/connect",
        json={"Container": observed["Id"], "EndpointConfig": {"Aliases": [alias]}},
    )
    return True


def attach(service, application, environment, container, alias):
    app = service.get("app/" + service.key(application, environment))
    network = service.network(app)
    changed = connect(service, network, container, alias)
    record = {
        "application": application,
        "environment": environment,
        "container": container,
        "alias": alias,
        "network": network,
    }
    service.put(
        "attachment/" + application + "/" + environment + "/" + alias,
        "attachment",
        record,
    )
    return {**record, "changed": changed}


def reattach(service, app):
    """Restore recorded dependency attachments, e.g. after a database is recreated."""
    spec = app["spec"]
    with service.lock:
        records = [
            r
            for r in service.db.list("attachment")
            if r["application"] == spec["id"]
            and r["environment"] == spec["environment"]
        ]
    for record in records:
        try:
            connect(service, service.network(app), record["container"], record["alias"])
        except Exception:  # noqa: BLE001, S112 -- preflight reports the unreachable dependency
            continue


def configure_sync(service, op):
    from .release_sync import Configuration

    config = Configuration.model_validate(
        {
            k: op[k]
            for k in (
                "repository",
                "application",
                "credential",
                "workflow",
                "branch",
                "environments",
                "services",
            )
            if k in op
        }
    )
    for env in config.environments:
        app = service.get("app/" + service.key(config.application, env))
        if app["spec"]["repository"] != config.repository:
            raise ValueError("Repository does not match the registered application")
        if set(config.services) - {s["id"] for s in app["spec"]["services"]}:
            raise ValueError("Release services must exist in the application")
    root = service.manager.root
    if not (root / "github" / "credentials" / (config.credential + ".json")).is_file():
        raise ValueError(
            "Unknown GitHub credential; store it with team-builder github-credential"
        )
    record = {
        **config.model_dump(),
        "enabled": op.get("enabled", True),
        "updated_at": time.time(),
    }
    service.put(
        "release-sync-config/" + config.application, "release_sync_config", record
    )
    return record


def import_releases(service):
    """Run every enabled CI importer inside the manager (no host timer needed)."""
    from .deployment_api import handle
    from .deployments import token
    from .release_sync import Configuration, record_failure, sync

    manager = service.manager

    def request(root, body):
        return handle(
            manager,
            "/operator/deployments",
            "Bearer " + token(manager.secrets),
            json.loads(json.dumps(body)),
        )

    def load(path):
        with open(path, "rb") as archive:
            response = service.docker.client.post(
                "/v1.45/images/load",
                content=archive,
                headers={"Content-Type": "application/x-tar"},
                timeout=600,
            )
        if response.status_code != 200 or any(
            '"error"' in line for line in response.text.splitlines()
        ):
            raise RuntimeError("Docker image load failed")

    def inspect(tag):
        image = service.docker.call("GET", "/images/" + quote(tag, safe="") + "/json")
        if not image:
            raise RuntimeError("Loaded image is unavailable")
        return image

    with service.lock:
        configs = [c for c in service.db.list("release_sync_config") if c["enabled"]]
    for record in configs:
        config = Configuration.model_validate(
            {k: v for k, v in record.items() if k not in ("enabled", "updated_at")}
        )
        try:
            sync(manager.root, config, request=request, load=load, inspect=inspect)
        except BlockingIOError:
            continue
        except Exception as exc:  # noqa: BLE001 -- status records only sanitized error classes
            record_failure(manager.root, config, exc)
