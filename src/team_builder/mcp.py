"""COA-only stdio MCP facade; the service validates every signed authorization."""

import os

import httpx
from mcp.server.fastmcp import FastMCP

from .models import ManagementOperation


def main():
    server = FastMCP("Buzz community management")
    client = httpx.Client(
        base_url=os.environ["TEAM_BUILDER_URL"],
        headers={"Authorization": "Bearer " + os.environ["TEAM_BUILDER_TOKEN"]},
        timeout=900,
        trust_env=False,
    )

    def call(path, data):
        result = client.post(path, json=data)
        if result.status_code != 200:
            raise ValueError(result.json().get("error", "Management request failed"))
        return result.json()

    @server.tool()
    def inspect_team() -> dict:
        """Read managed agents, channels, projects, provider settings, credential names, and gateway health."""
        return call("/inspect", {})

    @server.tool()
    def inspect_projects() -> dict:
        """List managed Buzz projects, repository coordinates, and linked channels."""
        return call("/projects", {})

    @server.tool()
    def store_provider_credential(source_event_id: str, id: str, api_key: str) -> dict:
        """Store an owner-supplied key under an immutable name. Never include keys in proposals.

        Prefer `team-builder credential` with a hidden local prompt when possible;
        keys pasted in Buzz chat remain in that chat's history.
        Use a separate owner message to authorize configure_provider afterward.
        """
        return call(
            "/credential",
            {"source_event_id": source_event_id, "id": id, "api_key": api_key},
        )

    @server.tool()
    def propose_changes(
        source_event_id: str, operations: list[ManagementOperation]
    ) -> dict:
        """Propose agents, channels, invitations, provider settings, or Buzz projects for owner approval.

        Includes create_project, update_project and delete_project. Use for broad
        requests or any agent proposal; operations are frozen for approval."""
        return call(
            "/propose",
            {
                "source_event_id": source_event_id,
                "operations": [op.model_dump(exclude_none=True) for op in operations],
            },
        )

    @server.tool()
    def execute_direct(
        source_event_id: str, operations: list[ManagementOperation]
    ) -> dict:
        """Execute owner-authorized community changes, including create_project, update_project,
        delete_project, agents, channels, invitations, and provider settings.

        Project creation is an operation in this tool, not a separate MCP tool.
        Other authors are rejected."""
        return call(
            "/execute",
            {
                "source_event_id": source_event_id,
                "operations": [op.model_dump(exclude_none=True) for op in operations],
            },
        )

    @server.tool()
    def execute_proposal(approval_event_id: str) -> dict:
        """Execute a frozen proposal after the owner replies 'approve' directly to it."""
        return call(
            "/approve",
            {"approval_event_id": approval_event_id},
        )

    server.run()
