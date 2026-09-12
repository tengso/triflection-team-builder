"""COA-only stdio MCP facade; the service validates every signed authorization."""

import os

import httpx
from mcp.server.fastmcp import FastMCP


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
        """Read managed agents, channels, and gateway health."""
        return call("/inspect", {})

    @server.tool()
    def propose_changes(source_event_id: str, operations: list[dict]) -> dict:
        """Post immutable operations for owner approval. Use for broad requests or any agent proposal."""
        return call(
            "/propose", {"source_event_id": source_event_id, "operations": operations}
        )

    @server.tool()
    def execute_direct(source_event_id: str, operations: list[dict]) -> dict:
        """Execute a specific instruction from the human owner. Other authors are rejected."""
        return call(
            "/execute", {"source_event_id": source_event_id, "operations": operations}
        )

    @server.tool()
    def execute_proposal(approval_event_id: str) -> dict:
        """Execute a frozen proposal after the owner replies 'approve' directly to it."""
        return call(
            "/approve",
            {"approval_event_id": approval_event_id},
        )

    server.run()
