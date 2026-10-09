from __future__ import annotations

from fastapi.testclient import TestClient


def test_mcp_over_http_lists_and_calls_tools(app) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as client:
        init = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200, init.text
        listed = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert {"list_projects", "list_plugins", "video_validate", "driver_act", "score_plan"} <= names
