from __future__ import annotations

import json
import textwrap
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import AuthEnv
from fastapi.testclient import TestClient

import eks_harness.engine.runs as engine_runs
from eks_harness.daemon.context import AppContext
from eks_harness.ir.registry import Registry, default_registry

PROJECT = "acme/ops"

SEED = """
    from collections.abc import Mapping
    from typing import Any

    import eks
    from eks_harness.plugins.sdk import BackendInfo, BackendSpec


    @eks.backend("testapi", description="The backend the op tests call")
    class TestApi:
        def describe(self) -> dict[str, Any]:
            return {"name": "testapi"}

        def fingerprint(self, context: Mapping[str, Any]) -> str:
            return "testapi"

        def prepare(self, context: Mapping[str, Any]) -> None:
            return None

        def spec(self, context: Mapping[str, Any]) -> BackendSpec:
            return BackendSpec(info=BackendInfo(url="http://127.0.0.1:9"))

        def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
            return None


    @eks.op("acme", name="local", out="json", description="A tree op without an executor")
    class Local(eks.Args):
        value: int = 1
"""


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    write(root / ".harness" / "project.toml", f'[project]\nid = "{PROJECT}"\n')
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n')
    write(root / ".harness" / "seeds" / "standard.py", SEED)
    return root.resolve()


class Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        type(self).seen.append((self.command, self.path))
        payload = json.dumps({"pong": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    Recorder.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def items_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["summary"]["name"]: item for item in payload["items"]}


def test_ops_list_with_executor_info(client: TestClient, tree: Path) -> None:
    listed = client.get("/api/ops").json()
    found = items_by_name(listed)
    assert found["ctl.when"]["executor"] == {"name": "eks.native", "version": "1", "location": "hub",
                                             "available": True, "native": True}
    api = found["api.get"]["executor"]
    assert api["name"] == "eks.api" and api["available"] is True and api["native"] is False
    assert found["blender.render"]["executor"]["available"] is False
    assert found["blender.render"]["executor"]["name"] is None
    tap = found["ui.tap"]["executor"]
    assert tap["name"] == "eks.worker" and tap["location"] == "worker" and tap["available"] is True
    assert found["api.get"]["summary"]["family"] == "api" and found["api.get"]["summary"]["plugin"] == "eks"
    assert found["api.get"]["schema"] is None

    family = items_by_name(client.get("/api/ops", params={"family": "ctl"}).json())
    assert family and all(name.startswith("ctl.") for name in family)
    assert "acme.local" not in found
    with_tree = items_by_name(client.get("/api/ops", params={"tree": str(tree)}).json())
    assert with_tree["acme.local"]["executor"]["available"] is False
    assert with_tree["acme.local"]["summary"]["plugin"] == "project"


def test_op_detail_schema_and_suggestions(client: TestClient, tree: Path) -> None:
    detail = client.get("/api/ops/api.get").json()
    assert detail["summary"]["name"] == "api.get"
    assert detail["schema"]["type"] == "object" and "path" in json.dumps(detail["schema"])
    assert detail["executor"]["name"] == "eks.api"
    unknown = client.get("/api/ops/api.gett")
    assert unknown.status_code == 404
    payload = unknown.json()
    assert payload["error"] == "op_not_found" and "api.get" in payload["suggestions"]
    assert "did you mean" in payload["message"]
    local = client.get("/api/ops/acme.local", params={"tree": str(tree)}).json()
    assert local["executor"]["available"] is False and local["schema"]["type"] == "object"
    assert client.get("/api/ops/acme.local").status_code == 404
    bad_tree = client.get("/api/ops/api.get", params={"tree": str(tree / "nowhere")})
    assert bad_tree.status_code == 400 and bad_tree.json()["error"] == "bad_tree"


def acquire_backend(client: TestClient, ctx: AppContext, tree: Path, url: str) -> str:
    response = client.post("/api/backend-leases", json={"definition": "testapi", "tree": str(tree),
                                                        "project": PROJECT, "session": "main", "wait": True})
    assert response.status_code == 200, response.text
    lease = response.json()["lease"]
    record = ctx.pools.backends.records[lease["backendId"]]
    ctx.pools.backends.store_info(record, {"url": url, "credentials": {}})
    return lease["id"]


def test_do_runs_a_real_api_op_against_a_held_backend(client: TestClient, ctx: AppContext, tree: Path,
                                                      server: ThreadingHTTPServer) -> None:
    url = f"http://127.0.0.1:{server.server_address[1]}/base"
    lease_id = acquire_backend(client, ctx, tree, url)
    response = client.post("/api/ops/api.get/do", json={
        "args": {"path": "/api/ping", "query": {"q": "1"}}, "tree": str(tree), "backend": {"ref": lease_id},
        "fakes": {"families": ["api"]}, "wait": 60})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["run"]["status"] == "passed", result
    assert result["value"] == {"pong": True, "path": "/base/api/ping?q=1"}
    assert result["failure"] is None and result["artifacts"] == []
    assert Recorder.seen == [("GET", "/base/api/ping?q=1")]
    assert result["run"]["plan"]["name"] == "do" and result["run"]["project"] == PROJECT
    nodes = client.get(f"/api/runs/{result['run']['id']}/journal").json()["nodes"]
    assert [(node["op"], node["simulated"], node["executor"]) for node in nodes] == [("api.get", False, "eks.api@1")]
    assert ctx.pools.backends.find_lease(lease_id) is not None

    failing = client.post("/api/ops/api.get/do", json={
        "args": {"path": "/api/ping", "expect_status": [201]}, "tree": str(tree), "backend": {"ref": lease_id},
        "fakes": {"families": ["api"]}, "wait": 60}).json()
    assert failing["run"]["status"] == "failed" and failing["value"] is None
    assert failing["failure"]["code"] == "http_status" and failing["failure"]["op"] == "api.get"


def test_do_simulates_worker_ops_with_fakes(client: TestClient, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.post("/api/ops/ui.tap/do", json={
        "args": {"target": "#save"}, "tree": str(tree), "backend": {"ref": "testapi"},
        "fakes": {"families": ["*"]}, "wait": 60})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["run"]["status"] == "passed" and result["run"]["simulated"] is True
    assert result["value"] is None and result["failure"] is None
    nodes = client.get(f"/api/runs/{result['run']['id']}/journal").json()["nodes"]
    assert [(node["op"], node["simulated"]) for node in nodes] == [("ui.tap", True)]

    unknown = client.post("/api/ops/ui.tapp/do", json={"tree": str(tree)})
    assert unknown.status_code == 404 and "ui.tap" in unknown.json()["suggestions"]
    monkeypatch.setattr(engine_runs, "worker_driver_problem", lambda platform: "this test hub has no workers")
    refused = client.post("/api/ops/ui.tap/do", json={"args": {"target": "#save"}, "tree": str(tree),
                                                      "backend": {"ref": "testapi"}, "wait": 5})
    assert refused.status_code == 422
    assert "driver.missing" in [issue["code"] for issue in refused.json()["issues"]]
    container = client.post("/api/ops/ctl.when/do", json={"args": {}, "tree": str(tree)})
    assert container.status_code == 400 and container.json()["error"] == "plan_build"
    persona_without_session = client.post("/api/ops/acme.local/do", json={"tree": str(tree), "persona": "admin",
                                                                          "fakes": {"families": ["*"]}})
    assert persona_without_session.status_code == 400


def test_do_and_tree_ops_need_access(auth_env: AuthEnv, tmp_path: Path) -> None:
    root = tmp_path / "tree"
    write(root / ".harness" / "project.toml", f'[project]\nid = "{PROJECT}"\n')
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n')
    write(root / ".harness" / "seeds" / "standard.py", SEED)
    client = auth_env.client
    viewer, viewer_key = auth_env.make_user("viewer")
    auth_env.grant(viewer, PROJECT, "viewer")
    watching = auth_env.headers_for(viewer_key)
    assert client.get("/api/ops", headers=watching).status_code == 200
    assert client.get("/api/ops", params={"tree": str(root)}, headers=watching).status_code == 200
    denied = client.post("/api/ops/ui.tap/do", json={"args": {"target": "#save"}, "tree": str(root),
                                                     "fakes": {"families": ["*"]}}, headers=watching)
    assert denied.status_code == 403
    _stranger, stranger_key = auth_env.make_user("stranger")
    hidden = client.get("/api/ops", params={"tree": str(root)}, headers=auth_env.headers_for(stranger_key))
    assert hidden.status_code == 404
