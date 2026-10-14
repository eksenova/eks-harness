from __future__ import annotations

import asyncio
import json
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import harness_client
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from eks_harness.cli import main, run_cmds
from eks_harness.cli.client import HarnessClient
from eks_harness.cli.ui import CliExit
from eks_harness.config import Config
from eks_harness.engine.model import NodeEventRecord, RunSummary
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.mcp_server import create_server

PROJECT = "acme/cli"

FLOW = """
    import eks
    from eks import by, expect


    @eks.flow(app="web-app")
    def smoke(s: eks.Session):
        s.ui.nav("/tenant/branches")
        expect(by.text("Kadıköy")).visible()
        s.checkpoint("list")
        return {"done": True}
"""

BACKEND = """
    from collections.abc import Mapping
    from typing import Any

    import eks
    from eks_harness.plugins.sdk import BackendInfo, BackendSpec


    @eks.backend("cliapi", description="The backend of the CLI test")
    class CliApi:
        def describe(self) -> dict[str, Any]:
            return {"name": "cliapi"}

        def fingerprint(self, context: Mapping[str, Any]) -> str:
            return "cliapi-1"

        def prepare(self, context: Mapping[str, Any]) -> None:
            return None

        def spec(self, context: Mapping[str, Any]) -> BackendSpec:
            return BackendSpec(info=BackendInfo(url="http://127.0.0.1:9"))

        def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
            return None
"""

BAD_FLOW = """
    import eks
    from eks import by


    @eks.flow(app="mobile-app")
    def bad(s: eks.Session):
        s.ui.tap(by.css("li.row"))
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
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "cli-tree").resolve()
    write(root / ".harness" / "project.toml", f'[project]\nid = "{PROJECT}"\n\n[backends]\ndefault = "cliapi"\n')
    write(root / ".harness" / "seeds" / "backend.py", BACKEND)
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n')
    write(root / "mobile-app" / ".harness" / "app.toml",
          '[app]\nname = "mobile-app"\nplatforms = ["ios"]\n')
    write(root / ".harness" / "flows" / "smoke.py", FLOW)
    write(root / ".harness" / "flows" / "bad.py", BAD_FLOW)
    monkeypatch.chdir(root)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


@pytest.fixture
def routed(client: TestClient, config: Config, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def factory(args: Any) -> HarnessClient:
        return harness_client(client, config)

    monkeypatch.setattr(run_cmds, "client_from_args", factory)
    return client


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_parse_pairs_reads_json_values_and_merges_over_args() -> None:
    parsed = run_cmds.parse_pairs(["route=/x", "count=3", "flags=[1, 2]", "name=Kadıköy", "empty="],
                                  '{"route": "/old", "keep": true}')
    assert parsed == {"route": "/x", "count": 3, "flags": [1, 2], "name": "Kadıköy", "empty": "", "keep": True}
    with pytest.raises(ValueError, match="KEY=VALUE"):
        run_cmds.parse_pairs(["no-equals"], None)
    with pytest.raises(CliExit):
        run_cmds.parse_pairs([], "[1, 2]")


@pytest.mark.parametrize(("status", "code"), [("passed", 0), ("failed", 1), ("error", 1), ("aborted", 13),
                                              ("paused", 12), ("running", 1)])
def test_exit_codes_follow_the_run_status(status: str, code: int) -> None:
    summary = RunSummary.model_validate({
        "id": "r1", "status": status, "plan": {"kind": "flow", "name": "x", "hash": "sha256:0"}, "createdAt": 1.0,
        "links": {"run": "/r", "report": "/r", "journal": "/r", "events": "/r", "plan": "/r"}})
    assert run_cmds.exit_code(summary) == code


def record(event_id: int, type: str = "node.passed", node: str | None = "n1") -> str:
    return json.dumps({"id": event_id, "runId": "r1", "type": type, "nodeId": node, "ts": 1.0 + event_id,
                       "detail": {}})


class FakeStreams:
    def __init__(self, bodies: list[list[str] | Exception]) -> None:
        self.bodies = bodies
        self.headers: list[dict[str, str]] = []

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: Any) -> Iterator[Any]:
        self.headers.append(dict(kwargs.get("headers") or {}))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body

        class Response:
            def iter_lines(self) -> Iterator[str]:
                yield from body

        yield Response()


def test_follow_skips_progress_and_heartbeats_and_resumes_after_a_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cmds.time, "sleep", lambda _seconds: None)
    streams = FakeStreams([
        ["retry: 3000", "", ": keep-alive", "id: 1", "event: node", f"data: {record(1)}", "",
         "event: progress", 'data: {"pct": 50}', ""],
        httpx.ReadError("dropped"),
        ["id: 1", "event: node", f"data: {record(1)}", "", "id: 2", "event: run",
         f"data: {record(2, 'run.passed', None)}", "", "event: end", "data: passed", ""],
    ])
    seen = list(run_cmds.follow(streams, "r1", timeout=30))  # type: ignore[arg-type]
    assert [item.id for item in seen] == [1, 2]
    assert streams.headers[0] == {} and streams.headers[1] == {"Last-Event-ID": "1"}
    assert streams.headers[2] == {"Last-Event-ID": "1"}


def test_run_follows_and_prints_outcomes_then_the_report(routed: TestClient, tree: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run_cli(capsys, "run", ".harness/flows/smoke.py", "--fake", "*")
    assert code == 0, out
    assert "passed ui.nav" in out and ".harness/flows/smoke.py:" in out
    assert "flow smoke: passed" in out and '"done": true' in out
    assert "run passed" in out

    code, out, _err = run_cli(capsys, "run", ".harness/flows/smoke.py", "--fake", "*", "--json")
    report = json.loads(out)
    assert code == 0 and report["run"]["status"] == "passed" and report["values"] == {"done": True}
    assert report["run"]["project"] == PROJECT and report["run"]["simulated"] is True

    code, out, _err = run_cli(capsys, "runs", "list", "--json")
    listed = json.loads(out)["items"]
    assert code == 0 and len(listed) == 2 and {item["status"] for item in listed} == {"passed"}

    run_id = report["run"]["id"]
    code, out, _err = run_cli(capsys, "runs", "show", run_id, "--journal", "--events")
    assert code == 0 and "ui.nav" in out and "run.passed" in out

    code, out, _err = run_cli(capsys, "runs", "replay", run_id, "--json")
    replayed = json.loads(out)
    assert code == 0 and replayed["run"]["replayOf"] == run_id


def test_repeat_compares_every_run_with_the_first(routed: TestClient, tree: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run_cli(capsys, "run", ".harness/flows/smoke.py", "--fake", "*", "--repeat", "2")
    assert code == 0, out + err
    assert "every repeat matched the first run" in err
    code, out, _err = run_cli(capsys, "run", ".harness/flows/smoke.py", "--fake", "*", "--repeat", "2", "--json")
    payload = json.loads(out)
    assert code == 0 and len(payload["runs"]) == 2 and len(payload["comparisons"]) == 1
    first, second = payload["runs"]
    assert second["run"]["repeatOf"] == first["run"]["id"] and payload["comparisons"][0]["same"] is True


def test_an_invalid_plan_never_reaches_the_hub(routed: TestClient, tree: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run_cli(capsys, "run", ".harness/flows/bad.py", "--fake", "*")
    assert code == 1
    assert "nothing ran" in err and "css" in (out + err).lower()
    assert routed.get("/api/runs").json()["items"] == []


def polling_follow(client: TestClient) -> Any:
    # The test transport buffers whole responses, so a paused run's endless event stream is read from the journal.
    def follow(_harness: Any, run_id: str, *, timeout: float, last_event_id: int | None = None) -> Iterator[Any]:
        deadline = time.monotonic() + timeout
        after = last_event_id or 0
        while time.monotonic() < deadline:
            page = client.get(f"/api/runs/{run_id}/journal", params={"events": "true", "after": after}).json()
            for event in page["events"]:
                after = event["id"]
                yield NodeEventRecord.model_validate(event)
            if page["run"]["status"] in ("passed", "failed", "aborted", "error"):
                return
            time.sleep(0.05)

    return follow


def test_a_paused_run_without_a_terminal_exits_with_the_attach_hint(routed: TestClient, tree: Path,
                                                                     capsys: pytest.CaptureFixture[str],
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cmds, "follow", polling_follow(routed))
    code, out, err = run_cli(capsys, "run", ".harness/flows/smoke.py", "--fake", "*", "--break", "op:ui.nav")
    assert code == 12, out + err
    assert "eks-harness debug" in err
    run_id = routed.get("/api/runs").json()["items"][0]["id"]

    code, out, _err = run_cli(capsys, "runs", "control", run_id, "status", "--json")
    assert code == 0 and json.loads(out)["status"] == "paused"
    code, out, _err = run_cli(capsys, "runs", "control", run_id, "break_list", "--json")
    assert json.loads(out)["breakpoints"][0]["text"] == "op:ui.nav"

    code, out, _err = run_cli(capsys, "runs", "cancel", run_id, "--reason", "test", "--json")
    assert code == 13 and json.loads(out)["status"] == "aborted"


def test_do_runs_one_op(routed: TestClient, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run_cli(capsys, "do", "ui.nav", "route=/tenant/branches", "--platform", "web", "--app",
                             "web-app", "--fake", "*", "--json")
    assert code == 0, out + err
    result = json.loads(out)
    assert result["run"]["status"] == "passed" and result["run"]["simulated"] is True


def test_mcp_engine_tools_run_plan_and_manage_flows(client: TestClient, config: Config, tree: Path) -> None:
    server, tools = create_server(lambda: harness_client(client, config))
    try:
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {"eks_run", "eks_plan", "eks_validate", "eks_ops", "eks_do", "eks_run_status", "eks_run_control",
                "eks_flows", "eks_flow_get", "eks_flow_save", "eks_flow_delete"} <= names

        def call(name: str, arguments: dict[str, Any]) -> Any:
            result = asyncio.run(server.call_tool(name, arguments))
            assert not result.is_error, result.content
            structured = result.structured_content or {}
            return structured.get("result", structured)

        ran = call("eks_run", {"spec": ".harness/flows/smoke.py", "tree": str(tree), "fakes": ["*"], "wait": 60})
        assert ran["report"]["run"]["status"] == "passed"
        run_id = ran["report"]["run"]["id"]

        status = call("eks_run_status", {"run_id": run_id, "report": True, "events_after": 0})
        assert status["run"]["status"] == "passed" and status["events"]

        planned = call("eks_plan", {"spec": ".harness/flows/smoke.py", "tree": str(tree)})
        assert planned["validation"]["ok"] is True
        checked = call("eks_validate", {"plan": planned["plan"], "tree": str(tree)})
        assert checked["ok"] is True

        catalog = call("eks_ops", {"name": "ui.nav", "tree": str(tree)})
        assert catalog["summary"]["name"] == "ui.nav" and catalog["schema"]

        flows = call("eks_flows", {"tree": str(tree), "kind": "flow"})
        assert ".harness/flows/smoke.py" in {item["path"] for item in flows["files"]}
        source = call("eks_flow_get", {"path": ".harness/flows/smoke.py", "tree": str(tree)})
        saved = call("eks_flow_save", {"path": ".harness/flows/copy.py", "source": source["source"],
                                       "tree": str(tree)})
        assert saved["validations"] and all(item["ok"] for item in saved["validations"].values())
        call("eks_flow_delete", {"path": ".harness/flows/copy.py", "tree": str(tree)})
        assert not (tree / ".harness" / "flows" / "copy.py").exists()

        refused = asyncio.run(_tool_error(server, "eks_run", {"spec": ".harness/flows/bad.py", "tree": str(tree),
                                                              "fakes": ["*"]}))
        assert "target.surface" in refused and ".harness/flows/bad.py:7" in refused
    finally:
        tools.close()


async def _tool_error(server: Any, name: str, arguments: dict[str, Any]) -> str:
    try:
        result = await server.call_tool(name, arguments)
    except ToolError as problem:
        return str(problem)
    assert result.is_error, result
    return " ".join(getattr(item, "text", "") for item in result.content)
