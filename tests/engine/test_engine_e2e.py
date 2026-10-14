from __future__ import annotations

import base64
import functools
import hashlib
import json
import subprocess
import tempfile
import textwrap
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from eks.workspace import plan_spec
from eks_harness.daemon.context import AppContext
from eks_harness.ir.registry import Registry, default_registry

PROJECT = "acme/e2e"
PASSWORD = "branch-secret-1"
BRANCHES = {"items": [{"id": "b-kadikoy", "name": "Kadıköy"}, {"id": "b-moda", "name": "Moda"}]}


@functools.cache
def clip_bytes(color: str) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "clip.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color={color}:size=64x64:rate=30", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-threads", "1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
                        str(target)], check=True)
        return target.read_bytes()


def first_clip() -> bytes:
    return clip_bytes("red")


def other_clip() -> bytes:
    return clip_bytes("blue")
SIMULATED_FAMILIES = ("ui", "check", "capture", "overlay", "expect", "session")

SEED = """
    from collections.abc import Mapping
    from typing import Any

    import eks
    from eks_harness.plugins.sdk import BackendInfo, BackendSpec


    @eks.backend("testapi", description="The branches API of the end-to-end test")
    class TestApi:
        def describe(self) -> dict[str, Any]:
            return {"name": "testapi"}

        def fingerprint(self, context: Mapping[str, Any]) -> str:
            return "testapi-1"

        def prepare(self, context: Mapping[str, Any]) -> None:
            return None

        def spec(self, context: Mapping[str, Any]) -> BackendSpec:
            return BackendSpec(info=BackendInfo(url="http://127.0.0.1:9"))

        def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
            return None


    @eks.resource("e2e_tenant", description="A tenant of the end-to-end seed")
    class Tenant(eks.Resource):
        name: str = ""


    @eks.resource("e2e_user", parent="e2e_tenant", persona=True, description="A user of the end-to-end seed")
    class User(eks.Resource):
        role: str = "admin"


    @eks.seed("standard", backend="testapi", now="2026-01-15T09:00+03:00", seed=2026)
    def standard(d: eks.Data):
        acme = d.e2e_tenant("acme", name="Acme Kuyumculuk")
        acme.e2e_user("admin", role="admin")
"""

FLOW = """
    import eks
    from eks import by, expect


    @eks.flow(app="web-app", seed="standard", as_="admin")
    def e2e(s: eks.Session):
        branches = s.api.get("/api/branches")
        s.ui.nav(f"/tenant/branches/{branches.items[0].id}")
        expect(by.text("Kadıköy")).visible()
        s.checkpoint("list")
        shot = s.shot("branches")
        annotated = eks.annotate(shot, [eks.overlay.callout("#branch-list", "Şubeler")])
        created = s.api.post("/api/branches", json={"name": "Moda"}, expect_status=[201])
        with eks.when(created.id):
            s.ui.nav(f"/tenant/branches/{created.id}")
        return {"branches": branches, "annotated": annotated}
"""

EDIT = """
    import eks
    from eks import t


    @eks.edit(fps=30, size=(64, 64), seed=7)
    def clip(e: eks.Edit):
        source = e.media("assets/clip.mp4")
        e.track("main").clip(source, start=t("0s"), src=t.span(t("0s"), t("1s")))
        e.render_settings(encoder="libx264")
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
    root = tmp_path / "e2e"
    write(root / ".harness" / "project.toml", f'[project]\nid = "{PROJECT}"\n')
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n'
          'timezone = "Europe/Istanbul"\nlocale = "tr-TR"\n')
    write(root / ".harness" / "seeds" / "standard.py", SEED)
    write(root / ".harness" / "flows" / "e2e.py", FLOW)
    write(root / ".harness" / "edits" / "clip.py", EDIT)
    (root / ".harness" / "edits" / "assets").mkdir(parents=True)
    (root / ".harness" / "edits" / "assets" / "clip.mp4").write_bytes(first_clip())
    return root.resolve()


class BranchApi(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, str | None]]] = []
    created: ClassVar[list[str]] = []
    expected = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()

    def _reply(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization")
        type(self).seen.append((self.command, self.path, header))
        if header != self.expected:
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path != "/api/branches":
            self._reply(404, {"error": "not found"})
            return
        self._reply(200, BRANCHES)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        branch = {"id": f"b-{uuid.uuid4().hex[:8]}", "name": body.get("name")}
        type(self).created.append(branch["id"])
        self._reply(201, branch)

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    BranchApi.seen = []
    BranchApi.created = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BranchApi)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def plan_of(tree: Path, spec: str) -> dict[str, Any]:
    return plan_spec(spec, cwd=tree).model_dump(mode="json", by_alias=True, exclude_none=True)


def start(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201, response.text
    run = response.json()["run"]
    assert run["status"] == "passed", (run, client.get(f"/api/runs/{run['id']}/report").json()["failures"])
    return run


def journal(client: TestClient, run_id: str) -> dict[str, Any]:
    return client.get(f"/api/runs/{run_id}/journal", params={"events": "true", "limit": 5000}).json()


def nodes_by_op(page: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for node in page["nodes"]:
        found.setdefault(node["op"], []).append(node)
    return found


def cache_outcomes(page: dict[str, Any]) -> dict[str, str]:
    return {event["nodeId"]: event["detail"]["outcome"] for event in page["events"] if event["type"] == "node.cache"}


def sse_frames(text: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        frames.append(fields)
    return frames


def test_end_to_end_acceptance(client: TestClient, ctx: AppContext, tree: Path,
                               server: ThreadingHTTPServer) -> None:
    url = f"http://127.0.0.1:{server.server_address[1]}"
    acquired = client.post("/api/backend-leases", json={"definition": "testapi", "tree": str(tree),
                                                        "project": PROJECT, "session": "main", "wait": True})
    assert acquired.status_code == 200, acquired.text
    lease = acquired.json()["lease"]
    record = ctx.pools.backends.records[lease["backendId"]]
    ctx.pools.backends.store_info(record, {"url": url, "credentials": {
        "admin": {"username": "admin", "password": PASSWORD}}})

    flow_plan = plan_of(tree, ".harness/flows/e2e.py")
    flow_request = {"plan": flow_plan, "tree": str(tree), "backend": {"ref": lease["id"]},
                    "fakes": {"families": ["*"], "force": ["overlay.annotate"]}, "wait": 60}
    first = start(client, flow_request)
    assert first["simulated"] is True and first["project"] == PROJECT

    assert [(method, path) for method, path, _ in BranchApi.seen] == [("GET", "/api/branches"),
                                                                      ("POST", "/api/branches")]
    assert all(header == BranchApi.expected for _method, _path, header in BranchApi.seen)

    page = journal(client, first["id"])
    planned = [node["id"] for node in flow_plan["nodes"]] + [
        node["id"] for parent in flow_plan["nodes"] for branch in (parent.get("branches") or {}).values()
        for node in branch]
    assert sorted(node["nodeId"] for node in page["nodes"]) == sorted(planned)
    assert all(node["status"] == "passed" for node in page["nodes"])
    for node in page["nodes"]:
        family = node["op"].split(".", 1)[0]
        if family == "api":
            assert node["simulated"] is False and node["executor"] == "eks.api@1", node
        elif family in SIMULATED_FAMILIES:
            assert node["simulated"] is True, node
    by_op = nodes_by_op(page)
    assert by_op["api.get"][0]["output"] == BRANCHES
    nav = by_op["ui.nav"][0]
    assert nav["args"]["route"] == "/tenant/branches/b-kadikoy"

    stream = client.get(f"/api/runs/{first['id']}/events")
    frames = sse_frames(stream.text)
    ids = [int(frame["id"]) for frame in frames if "id" in frame]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    statuses = [json.loads(frame["data"])["type"] for frame in frames
                if frame.get("event") == "run" and json.loads(frame["data"])["type"] in (
                    "run.preparing", "run.running", "run.passed")]
    assert statuses == ["run.preparing", "run.running", "run.passed"]
    assert frames[-1] == {"event": "end", "data": "passed"}

    report = client.get(f"/api/runs/{first['id']}/report").json()
    assert report["values"]["branches"] == BRANCHES
    annotated = report["values"]["annotated"]
    assert annotated["id"] and annotated["url"] and annotated["raw_url"] and annotated["sha256"].startswith("sha256:")
    (checkpoint,) = report["checkpoints"]
    assert checkpoint["label"] == "list" and checkpoint["ok"] is True and checkpoint["shots"]
    artifacts = {item["id"]: item for item in report["artifacts"]}
    shot_id = by_op["capture.shot"][0]["output"]["id"]
    assert shot_id in artifacts and annotated["id"] in artifacts
    assert all(item["url"] and item["rawUrl"] for item in report["artifacts"])
    assert report["timings"]["totalMs"] > 0 and report["timings"]["nodes"]
    assert report["cache"]["stored"] >= 1
    assert "overlay.annotate" in report["environment"]["fakes"]
    assert report["environment"]["backends"]
    assert {item["kind"] for item in report["resources"]} >= {"backend", "lease.web", "seed"}

    second = start(client, flow_request)
    assert len(BranchApi.seen) == 4 and len(BranchApi.created) == 2
    again = journal(client, second["id"])
    annotate_node = nodes_by_op(again)["overlay.annotate"][0]
    assert cache_outcomes(again)[annotate_node["nodeId"]] == "hit"
    second_report = client.get(f"/api/runs/{second['id']}/report").json()
    assert second_report["cache"]["hits"] >= 1
    assert second_report["values"]["annotated"]["id"] == annotated["id"]
    assert [node["cache"] for node in nodes_by_op(again)["api.get"]] == [None]

    edit_request = {"plan": plan_of(tree, ".harness/edits/clip.py"), "tree": str(tree),
                    "fakes": {"families": ["*"]}, "wait": 60}
    edit_first = start(client, edit_request)
    first_edit = nodes_by_op(journal(client, edit_first["id"]))
    assert first_edit["media.source"][0]["cache"] == "stored" and first_edit["edit.render"][0]["cache"] == "stored"
    edit_second = start(client, edit_request)
    second_page = journal(client, edit_second["id"])
    second_edit = nodes_by_op(second_page)
    outcomes = cache_outcomes(second_page)
    assert outcomes[second_edit["media.source"][0]["nodeId"]] == "hit"
    assert outcomes[second_edit["edit.render"][0]["nodeId"]] == "hit"
    assert second_edit["edit.render"][0]["output"] == first_edit["edit.render"][0]["output"]
    sha = "sha256:" + hashlib.sha256(first_clip()).hexdigest()
    assert first_edit["media.source"][0]["resolution"]["content"]["sha256"] == sha

    compared = client.get(f"/api/runs/{first['id']}/compare/{second['id']}").json()
    ops = {node["nodeId"]: node["op"] for node in page["nodes"]}
    assert compared["diverging"], compared
    assert all(item["field"] == "output" and ops[item["node"]].startswith("api.") for item in compared["diverging"])
    edits = client.get(f"/api/runs/{edit_first['id']}/compare/{edit_second['id']}").json()
    assert edits["same"] is True

    (tree / ".harness" / "edits" / "assets" / "clip.mp4").write_bytes(other_clip())
    edit_third = start(client, edit_request)
    third_page = journal(client, edit_third["id"])
    third_edit = nodes_by_op(third_page)
    third_outcomes = cache_outcomes(third_page)
    for op in ("media.source", "edit.render"):
        node = third_edit[op][0]
        assert third_outcomes[node["nodeId"]] != "hit"
        assert node["cacheKey"] != second_edit[op][0]["cacheKey"]
    content = third_edit["media.source"][0]["resolution"]["content"]
    assert content["sha256"] == "sha256:" + hashlib.sha256(other_clip()).hexdigest()
    assert content["uri"] == "file:assets/clip.mp4"
    assert ctx.pools.backends.find_lease(lease["id"]) is not None
    assert ctx.pools.backends.leases(owner=f"run:{first['id']}") == []
