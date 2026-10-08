"""Owner management of environment profiles and credentials in Mission Control.

Profiles stay immutable (edits register a new ID). Credential values can be
stored, generated, rotated and, after re-entering the dashboard key, revealed.
Every change and reveal is appended to an audit log that never holds values.
"""

import json
import re
import time

from pydantic import ValidationError

from .deployment_api import validation_summary

MAX_BODY = 300_000
AUDIT_FILE = "dashboard-audit.jsonl"
CREDENTIAL = re.compile(r"[a-z][a-z0-9-]{0,47}")
ACTIONS = {"profile", "preflight", "credential", "reveal", "policy-profile", "redeploy"}


class Refused(ValueError):
    """A request the owner can fix; the message is safe to show."""


def error_message(exc):
    if isinstance(exc, ValidationError):
        return "Invalid request: " + validation_summary(exc)
    return str(exc)[:600]


def audit_path(manager):
    return manager.deployments.root / AUDIT_FILE


def audit(manager, entry):
    path = audit_path(manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write(json.dumps({"time": time.time(), **entry}, sort_keys=True) + "\n")
    path.chmod(0o600)


def recent_audit(manager, application, environment, limit=20):
    try:
        lines = audit_path(manager).read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (
            item.get("application") == application
            and item.get("environment") == environment
        ):
            entries.append(item)
            if len(entries) >= limit:
                break
    return entries


def credentials(service, application, environment, profiles):
    directory = service.profiles.directory(application, environment) / "credentials"
    used = {}
    for profile in profiles:
        for ref in [*profile["secret_refs"].values(), *profile["file_refs"].values()]:
            used.setdefault(ref, []).append(profile["id"])
    result = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        record = json.loads(path.read_text())
        result.append(
            {
                "id": record["id"],
                "version": record["version"],
                "updated_at": path.stat().st_mtime,
                "used_by": sorted(used.pop(record["id"], [])),
            }
        )
    # Referenced by a profile but never stored: show it so the owner can add it.
    result += [
        {"id": ref, "version": None, "updated_at": None, "used_by": sorted(ids)}
        for ref, ids in sorted(used.items())
    ]
    return result


def overview(manager, application, environment):
    service = manager.deployments
    with service.lock:
        app = service.get("app/" + service.key(application, environment))
        listed = service.profiles.list(application, environment)
        policy = service.db.get("automation-policy/" + application)
    profiles = sorted(listed["profiles"], key=lambda p: p["id"])
    return {
        "application": application,
        "environment": environment,
        "current": app["current"],
        "active": listed["active"],
        "preflight": listed["preflight"],
        "profiles": profiles,
        "credentials": credentials(service, application, environment, profiles),
        "policy": {
            "enabled": policy["enabled"],
            "version": policy["version"],
            "profile": policy[environment + "_profile"],
        }
        if policy
        else None,
        "audit": recent_audit(manager, application, environment),
    }


def text(body, name, required=True):
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise Refused(f"{name} is required")
    return value


def act(manager, body, verify):
    """Run one management action. `verify(key)` re-checks the dashboard key."""
    if not isinstance(body, dict) or body.get("action") not in ACTIONS:
        raise Refused("Unknown management action")
    service = manager.deployments
    action = body["action"]
    application, environment = text(body, "application"), text(body, "environment")
    service.key(application, environment)
    entry = {"action": action, "application": application, "environment": environment}
    try:
        result = dispatch(
            manager, service, action, application, environment, body, verify
        )
    except Exception:
        audit(manager, {**entry, **target(body), "outcome": "refused"})
        raise
    audit(manager, {**entry, **target(body), "outcome": "done"})
    return result


def target(body):
    return {
        k: body[k]
        for k in ("id", "profile")
        if isinstance(body.get(k), str) and len(body[k]) <= 64
    } | (
        {"profile": body["profile"].get("id")}
        if isinstance(body.get("profile"), dict)
        and isinstance(body["profile"].get("id"), str)
        else {}
    )


def dispatch(manager, service, action, application, environment, body, verify):
    if action == "profile":
        profile = body.get("profile")
        if not isinstance(profile, dict):
            raise Refused("profile must be an object")
        existing = service.db.get(
            "profile/"
            + service.key(application, environment)
            + "/"
            + str(profile.get("id"))
        )
        if existing:
            raise Refused(
                f"Profile {profile.get('id')} already exists; profiles are immutable, choose a new ID"
            )
        return service.profiles.register(application, environment, profile)
    if action == "preflight":
        return service.profiles.preflight(
            application, environment, text(body, "profile")
        )
    if action == "credential":
        identifier = text(body, "id")
        if not CREDENTIAL.fullmatch(identifier):
            raise Refused("Credential names use lowercase letters, digits and dashes")
        generate = body.get("generate") is True
        value = text(body, "value", required=not generate)
        if generate and value is not None:
            raise Refused("Supply a value or ask for a generated token, not both")
        return service.profiles.credential(
            application,
            environment,
            identifier,
            value=value,
            generate=generate,
            rotate=body.get("rotate") is True,
        )
    if action == "reveal":
        identifier = text(body, "id")
        if not CREDENTIAL.fullmatch(identifier):
            raise Refused("Unknown credential")
        verify(text(body, "key"))
        path = (
            service.profiles.directory(application, environment)
            / "credentials"
            / (identifier + ".json")
        )
        if not path.is_file():
            raise Refused("Credential is not stored")
        version = json.loads(path.read_text())["version"]
        return {
            "id": identifier,
            "version": version,
            "value": service.profiles.value(
                application, environment, identifier, version
            ),
        }
    if action == "policy-profile":
        profile, expected = text(body, "profile"), body.get("expected_version")
        with service.lock:
            if not service.db.get("automation-policy/" + application):
                raise Refused("No release policy is configured for this application")
            policy, public = service.automation.policy(application)
            if expected != public["version"]:
                raise Refused(
                    "The release policy changed since this page was loaded; reload and try again"
                )
            service.get(
                "profile/" + service.key(application, environment) + "/" + profile
            )
            policy[environment + "_profile"] = profile
            result = service.automation.configure(policy)
        return {
            "version": result["version"],
            "profile": result[environment + "_profile"],
        }
    # redeploy: the environment's current release with the chosen profile.
    profile = text(body, "profile")
    with service.lock:
        app = service.get("app/" + service.key(application, environment))
        if not app["current"]:
            raise Refused("Nothing is deployed in this environment yet")
        plan = service.profiles.plan(
            application, environment, profile, release=app["current"]
        )
        if (service.db.get("job/" + plan["plan_id"]) or {}).get("state") == "succeeded":
            raise Refused(
                "This release is already deployed with exactly this profile and these credential versions"
            )
        job = service.enqueue(
            plan["plan_id"], source="dashboard", actor=manager.config.get("owner")
        )
    return {"job": job["id"], "state": job["state"], "release": job["release"]}
