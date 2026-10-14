from __future__ import annotations

import asyncio
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import harness_client
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from eks_harness.api.routes_sessions_engine import flow_name, flow_source, open_body
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.engine.runs import engine
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.mcp_server import create_server

PROJECT = "acme/sessions"
SESSIONS = "/api/engine/sessions"
FAKES = {"families": ["*"]}


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
    root = (tmp_path / "session-tree").resolve()
    write(root / ".harness" / "project.toml", f'[project]\nid = "{PROJECT}"\n')
    write(root / "web-app" / ".harness" / "app.toml",
          '[app]\nname = "web-app"\nplatforms = ["web"]\n\n[web]\nurl = "http://localhost:3000"\n')
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def open_session(client: TestClient, tree: Path, **extra: Any) -> dict[str, Any]:
    body = {"app": "web-app", "platform": "web", "tree": str(tree), "fakes": FAKES, "wait": 30, **extra}
    response = client.post(SESSIONS, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def do(client: TestClient, session: str, op: str, **args: Any) -> dict[str, Any]:
    response = client.post(f"{SESSIONS}/{session}/do", json={"op": op, "args": args})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_session_runs_ops_exports_them_as_a_flow_and_closes(client: TestClient, ctx: AppContext,
                                                              tree: Path) -> None:
    opened = open_session(client, tree)
    session = opened["session"]
    assert opened["run"]["status"] == "paused"
    assert opened["pause"]["reason"] == "source" and opened["pause"]["label"] == "session"

    tapped = do(client, session, "ui.tap", target="#save", timeout="5s")
    assert tapped["ok"] is True and tapped["node"] == "adhoc:1" and tapped["status"] == "paused"
    filled = do(client, session, "ui.fill", target={"by": "label", "args": {"text": "Ad"}, "nth": 1}, value="Ayşe")
    assert filled["ok"] is True
    shot = do(client, session, "capture.shot", name="after-fill")
    assert shot["ok"] is True and shot["artifacts"] and shot["artifacts"][0]["node"] == shot["node"]
    seen = do(client, session, "expect.target", target="text=Kaydet", assertion={"kind": "visible"})
    assert seen["ok"] is True
    unknown = do(client, session, "ui.nowhere")
    assert unknown["ok"] is False and "no op ui.nowhere" in unknown["message"]

    exported = client.get(f"{SESSIONS}/{session}/export", params={"name": "Kayıt akışı"})
    assert exported.status_code == 200, exported.text
    result = exported.json()
    assert result["name"] == "kayit_akisi" and "def kayit_akisi(s: eks.Session):" in result["source"]
    assert result["validation"]["ok"] is True, result
    assert [item["op"] for item in result["ops"]] == ["ui.tap", "ui.fill", "capture.shot", "expect.target"]
    assert all(item["surface"] for item in result["ops"])
    source = result["source"]
    assert 'from eks import by, expect, t' in source
    assert '@eks.flow(app="web-app", platform="web")' in source
    assert 's.ui.tap("#save", timeout=t("5s"))' in source
    assert 's.ui.fill(by.label("Ad").nth(1), "Ayşe")' in source
    assert 's.shot("after-fill")' in source
    assert 'expect("text=Kaydet").visible()' in source or 'expect("Kaydet").visible()' in source

    closed = client.delete(f"{SESSIONS}/{session}")
    assert closed.status_code == 200, closed.text
    assert closed.json()["ok"] is True and closed.json()["run"]["status"] == "passed"
    again = client.delete(f"{SESSIONS}/{session}").json()
    assert again["ok"] is True and "already ended" in again["message"]
    assert engine(ctx).journal.run(session) is not None


def test_a_run_that_is_not_a_session_is_refused(client: TestClient, tree: Path) -> None:
    write(tree / ".harness" / "flows" / "plain.py", """
        import eks


        @eks.flow(app="web-app")
        def plain(s: eks.Session):
            s.ui.nav("/")
    """)
    started = client.post("/api/runs", json={"spec": ".harness/flows/plain.py", "cwd": str(tree), "fakes": FAKES,
                                             "wait": 30})
    assert started.status_code == 201, started.text
    run_id = started.json()["run"]["id"]
    refused = client.get(f"{SESSIONS}/{run_id}/export")
    assert refused.status_code == 400 and refused.json()["error"] == "not_a_session"


def test_ops_without_a_surface_call_export_as_s_op(client: TestClient, tree: Path) -> None:
    session = open_session(client, tree)["session"]
    assert do(client, session, "ui.scroll", to="bottom")["ok"] is True
    both = do(client, session, "expect.target", target="#total", assertion={"kind": "visible"}, soft=True,
              eventually=True)
    assert both["ok"] is True
    result = client.get(f"{SESSIONS}/{session}/export").json()
    assert result["validation"]["ok"] is True, result
    assert [item["surface"] for item in result["ops"]] == [True, False]
    assert 's.ui.scroll(to="bottom")' in result["source"]
    assert 's.op("expect.target", {' in result["source"] and '"soft": true' not in result["source"]
    assert '"soft": True, "eventually": True' in result["source"]
    client.delete(f"{SESSIONS}/{session}")


def test_the_session_timeout_is_the_pause_timeout_and_each_do_renews_it(client: TestClient, ctx: AppContext,
                                                                        tree: Path) -> None:
    ctx.config.set("engine.sessionTimeoutSeconds", 10)
    session = open_session(client, tree)["session"]
    manager = engine(ctx)
    with manager._lock:
        scheduler = manager._active[session].scheduler
    assert scheduler is not None and scheduler.config.pause_timeout == 10
    pause = scheduler._pause
    assert pause is not None
    pause.started -= 8
    before = pause.started
    assert do(client, session, "ui.url")["ok"] is True
    assert pause.started > before + 7
    pause.started -= 11
    deadline = time.monotonic() + 20
    while client.get(f"/api/runs/{session}").json()["status"] != "aborted":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert "paused longer than 10 s" in client.get(f"/api/runs/{session}").json()["reason"]


def test_mcp_session_and_seed_tools(client: TestClient, config: Config, tree: Path) -> None:
    server, tools = create_server(lambda: harness_client(client, config))
    try:
        def call(name: str, arguments: dict[str, Any]) -> Any:
            result = asyncio.run(server.call_tool(name, arguments))
            assert not result.is_error, result.content
            structured = result.structured_content or {}
            return structured.get("result", structured)

        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {"eks_session_open", "eks_session_do", "eks_session_close", "eks_session_export", "eks_seeds",
                "eks_seed_show"} <= names
        opened = call("eks_session_open", {"app": "web-app", "platform": "web", "tree": str(tree), "fakes": ["*"],
                                           "wait": 30})
        session = opened["session"]
        assert opened["status"] == "paused"
        done = call("eks_session_do", {"session": session, "op": "ui.tap", "args": {"target": "#go"}})
        assert done["ok"] is True
        exported = call("eks_session_export", {"session": session, "name": "go"})
        assert exported["validation"]["ok"] is True and 's.ui.tap("#go")' in exported["source"]
        closed = call("eks_session_close", {"session": session})
        assert closed["run"]["status"] == "passed"

        assert call("eks_seeds", {}) == {"items": []}
        missing = asyncio.run(_tool_error(server, "eks_seed_show", {"key": "nothing"}))
        assert "No seed snapshot nothing" in missing
        refused = asyncio.run(_tool_error(server, "eks_session_open", {"app": "no-such-app", "tree": str(tree),
                                                                       "fakes": ["*"], "wait": 30}))
        assert refused
    finally:
        tools.close()


async def _tool_error(server: Any, name: str, arguments: dict[str, Any]) -> str:
    try:
        result = await server.call_tool(name, arguments)
    except ToolError as problem:
        return str(problem)
    assert result.is_error, result
    return " ".join(getattr(item, "text", "") for item in result.content)


def test_the_session_flow_holds_a_backend_only_when_asked() -> None:
    plain = {"app": "web-app", "platform": "web", "seed": None, "persona": None, "viewport": None, "backend": None}
    assert open_body(plain) == ['s.debug.break_("session")']
    assert open_body({**plain, "backend": "bl_0123456789abcdef"}) == ["s.backend.info", 's.debug.break_("session")']
    assert open_body({**plain, "seed": "standard", "backend": "testapi"}) == ['s.debug.break_("session")']
    source = flow_source({**plain, "seed": "standard", "persona": "admin", "viewport": "mobile"}, [])
    assert '@eks.flow(app="web-app", platform="web", seed="standard", as_="admin", viewport="mobile")' in source
    assert source.endswith("def session(s: eks.Session):\n    pass\n") and "from eks import" not in source
    assert flow_name("class") == "flow_class" and flow_name("2fa giriş") == "flow_2fa_giris" and flow_name("") == "session"
