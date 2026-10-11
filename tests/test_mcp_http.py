from __future__ import annotations

from pathlib import Path

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


def rpc(client: TestClient, method: str, params: dict | None = None, **headers: str) -> dict:
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
    response = client.post("/mcp/", headers={**base, **headers}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_mcp_over_http_scopes_plugin_tools_and_tree_to_the_request(app, tmp_path: Path, tools_tree) -> None:
    tree_a = tools_tree(tmp_path / "web-a", "tool_from_a", "a")
    tree_b = tools_tree(tmp_path / "web-b", "tool_from_b", "b")
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    with TestClient(app) as client:
        rpc(client, "initialize", init)
        names_a = {t["name"] for t in rpc(client, "tools/list", **{"X-Eks-Harness-Tree": str(tree_a)})
                   ["result"]["tools"]}
        names_b = {t["name"] for t in rpc(client, "tools/list", **{"X-Eks-Harness-Tree": str(tree_b)})
                   ["result"]["tools"]}
        bare = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
        assert "tool_from_a" in names_a and "tool_from_b" not in names_a
        assert "tool_from_b" in names_b and "tool_from_a" not in names_b
        assert not {"tool_from_a", "tool_from_b"} & bare and "list_plugins" in bare
        called = rpc(client, "tools/call", {"name": "tool_from_b", "arguments": {}},
                     **{"X-Eks-Harness-Tree": str(tree_b)})
        assert called["result"]["content"][0]["text"] == f"b|{tree_b}"
        response = client.post(f"/mcp/?tree={tmp_path / 'missing'}", json={"jsonrpc": "2.0", "id": 1,
                                                                          "method": "tools/list"},
                               headers={"Accept": "application/json, text/event-stream"})
        assert response.status_code == 400


def test_flow_run_over_http_needs_a_tree(app) -> None:
    with TestClient(app) as client:
        called = rpc(client, "tools/call", {"name": "flow_run", "arguments": {"code": "result = 1"}})
        assert called["result"]["isError"] and "X-Eks-Harness-Tree" in called["result"]["content"][0]["text"]
