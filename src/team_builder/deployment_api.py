"""Separate operator/agent authentication and authorization for deployment tools."""

import hmac
import json

from .deployments import token
from .nostr import tags


def handle(manager, path, authorization, body):
    service = manager.deployments
    if path == "/operator/deployments":
        if not hmac.compare_digest(authorization, "Bearer " + token(manager.secrets)):
            raise ValueError("Local operator authentication required")
        action = body.pop("action")
        methods = {
            "register": service.register,
            "release": service.release,
            "grant": service.grant,
            "plan": lambda operation="deploy", **kw: service.plan(
                action=operation, **kw
            ),
            "execute": service.enqueue,
            "inspect": service.read,
            "logs": service.logs,
            "credential": service.profiles.credential,
            "profile": service.profiles.register,
            "profiles": service.profiles.list,
            "preflight": service.profiles.preflight,
            "configure": service.profiles.plan,
            "automation-policy": service.automation.configure,
            "automation-status": service.automation.status,
            "attach": lambda **kw: service.apply_change(
                {"action": "attach_dependency", **kw}
            ),
            "release-sync-config": lambda **kw: service.apply_change(
                {"action": "configure_release_sync", **kw}
            ),
        }
        if action not in methods:
            raise ValueError("Unknown operator action")
        return methods[action](**body)
    agent = body.pop("agent")
    # Do not allow traversal or arbitrary tokens to reach the registry.
    if (
        not isinstance(agent, str)
        or len(agent) > 48
        or not hmac.compare_digest(
            authorization, "Bearer " + token(manager.secrets, agent)
        )
    ):
        raise ValueError("Deployment agent authentication required")
    action = body.pop("action")
    if action in ("propose-change", "approve-change"):
        return change(manager, agent, action, body)
    application, environment = (
        body.pop("application"),
        body.pop("environment", "production"),
    )
    key = service.key(application, environment)
    service.authorized(agent, key)
    if action == "inspect":
        snapshot = service.read()
        return {
            **snapshot,
            "applications": [
                a
                for a in snapshot["applications"]
                if a["id"] == application and a["environment"] == environment
            ],
            "operations": [
                j
                for j in snapshot["operations"]
                if j["application"] == application and j["environment"] == environment
            ],
        }
    if action == "releases":
        with service.lock:
            return {
                "releases": [
                    r
                    for r in service.db.list("release")
                    if r["application"] == application
                    and r["environment"] == environment
                ]
            }
    if action == "automation-status":
        return service.automation.status(application, environment)
    if action == "automation-retry":
        return service.automation.retry(application, environment, agent)
    if action == "automation-verify":
        return service.automation.verify(application, environment, agent)
    if action == "automation-rollback":
        if environment != "production":
            raise ValueError("Automatic rollback applies to production")
        return service.automation.rollback(application, agent)
    if action == "profiles":
        return service.profiles.list(application, environment)
    if action == "preflight":
        return service.profiles.preflight(application, environment, **body)
    if action == "configure":
        return service.profiles.plan(application, environment, **body)
    if action == "logs":
        detail = body.pop("detail", None) or "summary"
        if detail not in ("summary", "redacted"):
            raise ValueError("Log detail must be summary or redacted")
        if (
            detail == "redacted"
            and service.diagnostics(application, environment) != "redacted"
        ):
            raise ValueError("The release policy limits this environment to summaries")
        return service.logs(application, environment, detail=detail, **body)
    if action == "plan":
        return service.plan(
            application, environment, action=body.pop("action_type", "deploy"), **body
        )
    if action == "operation":
        job = service.get("job/" + body["operation_id"])
        if job["application"] != application or job["environment"] != environment:
            raise ValueError("Operation outside assigned scope")
        return service.public_job(job)
    if action in ("propose", "execute"):
        plan_id = body["plan_id"]
        plan = service.get("plan/" + plan_id)
        if plan["application"] != application or plan["environment"] != environment:
            raise ValueError("Plan outside assigned scope")
        with manager.lock:
            source = manager.source(body["source_event_id"], owner=action == "execute")
            member = manager.resource(agent, "agent")
            channel = tags(source, "h")[0][0]
            if channel not in member["channel_ids"]:
                raise ValueError("Source message outside agent channels")
            operations = [{"action": "execute_deployment", "plan_id": plan_id}]
            if action == "propose":
                return manager.propose(
                    body["source_event_id"], operations, proposer=agent
                )
            return manager.execute(body["source_event_id"], operations)
    if action == "approve":
        with manager.lock:
            approval = manager.source(body["approval_event_id"], owner=True)
            replies = [
                r[0] for r in tags(approval, "e") if len(r) >= 3 and r[2] == "reply"
            ]
            if len(replies) != 1:
                raise ValueError("Reply approve to the frozen deployment proposal")
            row = manager.registry.db.execute(
                "SELECT body,event FROM proposals WHERE json_extract(event,'$.id')=?",
                (replies[0],),
            ).fetchone()
            member = manager.resource(agent, "agent")
            proposal = json.loads(row["event"]) if row else {}
            channel = tags(approval, "h")[0][0]
            authoritative = manager.buzz.channel(channel)
            if (
                proposal.get("pubkey") != member["pubkey"]
                or channel not in member["channel_ids"]
                or not authoritative
                or member["pubkey"] not in authoritative["roles"]
            ):
                raise ValueError(
                    "Approval must target this agent's proposal in its channel"
                )
            operations = json.loads(row["body"]) if row else []
            if (
                len(operations) != 1
                or operations[0].get("action") != "execute_deployment"
            ):
                raise ValueError("Not a deployment proposal")
            plan = service.get("plan/" + operations[0]["plan_id"])
            if plan["application"] != application or plan["environment"] != environment:
                raise ValueError("Proposal outside assigned scope")
            return manager.approve(approval_event_id=body["approval_event_id"])
    raise ValueError("Unknown deployment action")


def change(manager, agent, action, body):
    """Agent-authored release setup: frozen proposal, owner approval, manager execution."""
    from .models import RELEASE_CHANGES, validate
    from .release_changes import check_scope

    service = manager.deployments
    with manager.lock:
        if action == "propose-change":
            operations = check_scope(service, agent, validate(body["operations"]))
            source = manager.source(body["source_event_id"])
            if (
                tags(source, "h")[0][0]
                not in manager.resource(agent, "agent")["channel_ids"]
            ):
                raise ValueError("Source message outside agent channels")
            return manager.propose(body["source_event_id"], operations, proposer=agent)
        approval = manager.source(body["approval_event_id"], owner=True)
        replies = [r[0] for r in tags(approval, "e") if len(r) >= 3 and r[2] == "reply"]
        if len(replies) != 1:
            raise ValueError("Reply approve to the frozen change proposal")
        row = manager.registry.db.execute(
            "SELECT body,event FROM proposals WHERE json_extract(event,'$.id')=?",
            (replies[0],),
        ).fetchone()
        member = manager.resource(agent, "agent")
        channel = tags(approval, "h")[0][0]
        authoritative = manager.buzz.channel(channel)
        if (
            not row
            or json.loads(row["event"]).get("pubkey") != member["pubkey"]
            or channel not in member["channel_ids"]
            or not authoritative
            or member["pubkey"] not in authoritative["roles"]
        ):
            raise ValueError(
                "Approval must target this agent's proposal in its channel"
            )
        # Scope was enforced when the frozen proposal was published.
        if any(op["action"] not in RELEASE_CHANGES for op in json.loads(row["body"])):
            raise ValueError("Not a release configuration proposal")
        return manager.approve(approval_event_id=body["approval_event_id"])
