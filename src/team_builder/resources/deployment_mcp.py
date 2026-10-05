"""Standalone MCP facade, provisioned only to deployment-authorized agents."""

import os

import httpx
from mcp.server.fastmcp import FastMCP

server = FastMCP("Production operations")
client = httpx.Client(
    base_url="http://manager:8088",
    headers={"Authorization": "Bearer " + os.environ["DEPLOYMENT_TOKEN"]},
    timeout=30,
    trust_env=False,
)


def post(body):
    result = client.post(
        "/deployments", json={"agent": os.environ["DEPLOYMENT_AGENT"], **body}
    )
    if result.status_code != 200:
        raise ValueError(result.json().get("error", "Deployment request unavailable"))
    return result.json()


def call(action, application, environment="production", **kwargs):
    return post(
        {
            "action": action,
            "application": application,
            "environment": environment,
            **kwargs,
        }
    )


@server.tool()
def inspect_application(application: str, environment: str = "production") -> dict:
    """Read service health, deployed release and recent durable jobs. Assigned applications only."""
    return call("inspect", application, environment)


@server.tool()
def list_releases(application: str, environment: str = "production") -> dict:
    """List registered immutable releases and the CI importer state (importing, current, stale, failed). New CI releases appear a few minutes after the workflow finishes."""
    return call("releases", application, environment)


@server.tool()
def get_service_logs(
    application: str,
    service: str,
    environment: str = "production",
    detail: str = "summary",
) -> dict:
    """Service diagnostics. detail="summary" returns recognized entries; detail="redacted" returns the last 200 lines with secrets masked (staging, and production when the release policy allows)."""
    return call("logs", application, environment, service=service, detail=detail)


@server.tool()
def plan_deployment(
    application: str,
    operation: str = "deploy",
    release: str | None = None,
    service: str | None = None,
    environment: str = "production",
    profile: str | None = None,
) -> dict:
    """Freeze a deploy, restart, or rollback plan. This does not execute anything. Migrations are excluded; rollback restores images only."""
    return call(
        "plan",
        application,
        environment,
        action_type=operation,
        release=release,
        service=service,
        profile=profile,
    )


@server.tool()
def propose_deployment(
    application: str,
    plan_id: str,
    source_event_id: str,
    environment: str | None = None,
) -> dict:
    """Publish a frozen plan in the source Buzz thread. Owner replies approve to authorize it. The environment defaults to the plan's."""
    return call(
        "propose",
        application,
        environment,
        plan_id=plan_id,
        source_event_id=source_event_id,
    )


@server.tool()
def execute_deployment(
    application: str,
    plan_id: str,
    source_event_id: str,
    environment: str | None = None,
) -> dict:
    """Queue a frozen plan authorized by a direct specific signed owner instruction. Agent messages cannot authorize changes. Returns a job ID; poll get_deployment_operation."""
    return call(
        "execute",
        application,
        environment,
        plan_id=plan_id,
        source_event_id=source_event_id,
    )


@server.tool()
def approve_deployment(
    application: str, approval_event_id: str, environment: str | None = None
) -> dict:
    """Queue exactly the frozen proposal the owner replied approve to. Unrelated approvals are rejected."""
    return call(
        "approve", application, environment, approval_event_id=approval_event_id
    )


@server.tool()
def get_deployment_operation(
    application: str, operation_id: str, environment: str | None = None
) -> dict:
    """Read a persistent deployment job and its timestamped progress. Queued is not success."""
    return call("operation", application, environment, operation_id=operation_id)


@server.tool()
def list_environment_profiles(
    application: str, environment: str = "production"
) -> dict:
    """List operator-approved profiles, secret reference names and last preflight. Never returns values."""
    return call("profiles", application, environment)


@server.tool()
def check_deployment_preflight(
    application: str, environment: str = "production", profile: str | None = None
) -> dict:
    """Check required credentials, files and TCP dependencies before deployment. No database writes or application restarts."""
    return call("preflight", application, environment, profile=profile)


@server.tool()
def plan_environment_configuration(
    application: str,
    profile: str,
    environment: str = "production",
    release: str | None = None,
) -> dict:
    """Freeze operator-defined profile and credential versions for owner approval. Include a release for one approved configure-and-deploy operation. Use propose_deployment and approve_deployment for this plan too."""
    return call("configure", application, environment, profile=profile, release=release)


@server.tool()
def inspect_release_automation(
    application: str, environment: str = "production"
) -> dict:
    """Read automatic release policy, verified UAT handoff, check outcomes and plain-language blockers. Never claim UAT acceptance yourself."""
    return call("automation-status", application, environment)


@server.tool()
def retry_automatic_release(application: str, environment: str = "production") -> dict:
    """After investigating and fixing a technical blocker, retry your assigned blocked stage under the existing operator policy. Maximum two retries; cannot change policy or bypass acceptance."""
    return call("automation-retry", application, environment)


@server.tool()
def verify_release_checks(application: str, environment: str = "production") -> dict:
    """Run the release policy's acceptance checks against this environment now (at most once a minute). Does not deploy or spend a retry; failed checks include masked output."""
    return call("automation-verify", application, environment)


@server.tool()
def rollback_production(application: str, environment: str = "production") -> dict:
    """Production agent only: restore the previous production release after a blocked automatic run, when the release policy allows agent rollback. Images only; databases are not rolled back."""
    return call("automation-rollback", application, environment)


@server.tool()
def propose_configuration_change(source_event_id: str, operations: list[dict]) -> dict:
    """Publish a frozen release-setup proposal in the source thread for owner approval. Operations: register_application{spec}, register_profile{application,environment,profile}, generate_credential{application,environment,id,rotate}, attach_dependency{application,environment,container,alias}, configure_release_sync{application,repository,credential,workflow,branch,environments,services,enabled}, configure_release_policy{policy}, configure_deployment_access{agent,application,environment,allowed}. Every operation object needs an "action" key; the release runbook skill shows the exact JSON shape. Errors name the failing field. Never include secret values."""
    return post(
        {
            "action": "propose-change",
            "source_event_id": source_event_id,
            "operations": operations,
        }
    )


@server.tool()
def approve_configuration_change(approval_event_id: str) -> dict:
    """Execute exactly the frozen configuration proposal the owner replied approve to."""
    return post({"action": "approve-change", "approval_event_id": approval_event_id})


if __name__ == "__main__":
    server.run()
