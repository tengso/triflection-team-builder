import asyncio
import json

from team_builder import mcp


def test_project_operations_are_visible_and_serialized_in_mcp(monkeypatch):
    captured = {}
    requests = []
    monkeypatch.setenv("TEAM_BUILDER_URL", "http://manager:8088")
    monkeypatch.setenv("TEAM_BUILDER_TOKEN", "test-token")
    monkeypatch.setattr(mcp.FastMCP, "run", lambda self: captured.update(server=self))

    class Response:
        status_code = 200

        def json(self):
            return {"state": "complete"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, path, json):
            requests.append((path, json))
            return Response()

    monkeypatch.setattr(mcp.httpx, "Client", Client)
    mcp.main()

    async def check():
        server = captured["server"]
        tools = {t.name: t for t in await server.list_tools()}
        for name in ("execute_direct", "propose_changes"):
            schema = json.dumps(tools[name].inputSchema)
            for action in (
                "create_project",
                "link_github_repository",
                "update_project",
                "delete_project",
                "configure_provider",
                "configure_github_access",
                "create_invite",
            ):
                assert action in schema
            assert "create_project" in tools[name].description
        await server.call_tool(
            "execute_direct",
            {
                "source_event_id": "source",
                "operations": [
                    {
                        "action": "create_project",
                        "id": "platform",
                        "name": "Platform",
                        "channel": "office",
                    }
                ],
            },
        )

    asyncio.run(check())
    path, body = requests[0]
    assert path == "/execute"
    assert body["operations"][0]["action"] == "create_project"
    assert body["operations"][0]["repositories"] == []
