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


def call(action, application, environment="production", **kwargs):
    result = client.post(
        "/deployments",
        json={
            "agent": os.environ["DEPLOYMENT_AGENT"],
            "action": action,
            "application": application,
            "environment": environment,
            **kwargs,
        },
    )
    if result.status_code != 200:
        raise ValueError(result.json().get("error", "Deployment request unavailable"))
    return result.json()


@server.tool()
def inspect_application(application: str, environment: str = "production") -> dict:
    """Read service health, deployed release and recent durable jobs. Assigned applications only."""
    return call("inspect", application, environment)


@server.tool()
def list_releases(application: str, environment: str = "production") -> dict:
    """List operator-registered immutable releases. Agents cannot register images or executable specifications."""
    return call("releases", application, environment)


@server.tool()
def get_service_logs(
    application: str, service: str, environment: str = "production"
) -> dict:
    """Bounded sanitized service diagnostics, never application secrets or raw request payloads."""
    return call("logs", application, environment, service=service)


@server.tool()
def plan_deployment(
    application: str,
    operation: str = "deploy",
    release: str | None = None,
    service: str | None = None,
    environment: str = "production",
) -> dict:
    """Freeze a deploy, restart, or rollback plan. This does not execute anything. Migrations are excluded; rollback restores images only."""
    return call(
        "plan",
        application,
        environment,
        action_type=operation,
        release=release,
        service=service,
    )


@server.tool()
def propose_deployment(
    application: str,
    plan_id: str,
    source_event_id: str,
    environment: str = "production",
) -> dict:
    """Publish a frozen plan in the source Buzz thread. Owner replies approve to authorize it."""
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
    environment: str = "production",
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
    application: str, approval_event_id: str, environment: str = "production"
) -> dict:
    """Queue exactly the frozen proposal the owner replied approve to. Unrelated approvals are rejected."""
    return call(
        "approve", application, environment, approval_event_id=approval_event_id
    )


@server.tool()
def get_deployment_operation(
    application: str, operation_id: str, environment: str = "production"
) -> dict:
    """Read a persistent deployment job and its timestamped progress. Queued is not success."""
    return call("operation", application, environment, operation_id=operation_id)


if __name__ == "__main__":
    server.run()
