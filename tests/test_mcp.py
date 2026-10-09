from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, ImageContent, TextContent

from eks_harness.cli.client import HarnessClient
from eks_harness.config import Config
from eks_harness.mcp_server import create_server
from eks_harness.paths import HOME_ENV
from eks_harness.pools.fake import FAKE_ENV

from conftest import harness_client, make_app

EXPECTED_TOOLS = {
    "list_projects", "list_sessions", "list_artifacts", "get_artifact", "upload_artifact", "share_artifact",
    "mark_seen", "tag_artifacts", "set_artifact_retention", "search", "add_note", "session_timeline",
    "lease_status",
    "annotate", "annotate_rerender", "annotate_recapture", "list_annotation_versions",
    "restore_annotation_version",
}
PROJECT = "acme/web-app"
SESSION = "feature/yardim-merkezi"


def png_bytes(width: int = 4, height: int = 3, noise: bool = False) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + (os.urandom(3 * width) if noise else b"\xff\x00\x00" * width) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def payload(result: CallToolResult) -> Any:
    assert not result.is_error, text_of(result)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def text_of(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


@dataclass
class McpEnv:
    test_client: TestClient
    client: HarnessClient
    config: Config
    tmp: Path

    def server(self):
        server, _ = create_server(lambda: self.client)
        return server

    def png(self, name: str = "home.png") -> Path:
        path = self.tmp / name
        path.write_bytes(png_bytes())
        return path


@pytest.fixture
def mcp_env(config: Config, tmp_path: Path) -> Iterator[McpEnv]:
    app = make_app(config)
    with TestClient(app) as test_client:
        client = harness_client(test_client, config)
        try:
            yield McpEnv(test_client=test_client, client=client, config=config, tmp=tmp_path)
        finally:
            client.close()


async def call(env: McpEnv, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
    async with Client(env.server()) as mcp_client:
        return await mcp_client.call_tool(name, arguments or {})


async def upload(env: McpEnv, **arguments: Any) -> dict[str, Any]:
    arguments.setdefault("path", str(env.png()))
    return payload(await call(env, "upload_artifact", arguments))


async def test_lists_every_tool_with_schemas(mcp_env: McpEnv) -> None:
    async with Client(mcp_env.server()) as mcp_client:
        listed = await mcp_client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert EXPECTED_TOOLS <= set(tools)
    assert tools["list_projects"].annotations.read_only_hint is True
    assert tools["upload_artifact"].annotations.read_only_hint is False
    assert set(tools["list_artifacts"].input_schema["properties"]) >= {
        "project", "session", "kind", "tag", "query", "unseen", "cursor", "limit"}
    assert tools["get_artifact"].input_schema["required"] == ["artifact_id"]
    assert tools["annotate"].input_schema["required"] == ["artifact_id", "spec"]
    assert tools["annotate_rerender"].input_schema["required"] == ["artifact_id"]
    assert tools["annotate_recapture"].input_schema["required"] == ["artifact_id"]
    assert tools["list_annotation_versions"].input_schema["required"] == ["artifact_id"]
    assert tools["restore_annotation_version"].input_schema["required"] == ["artifact_id", "version"]
    assert tools["list_annotation_versions"].annotations.read_only_hint is True
    assert tools["annotate"].annotations.read_only_hint is False
    assert all(tool.description for tool in listed.tools)


async def test_upload_returns_links_and_lands_in_project_and_session(mcp_env: McpEnv) -> None:
    artifact = await upload(mcp_env, project=PROJECT, session=SESSION, caption="Help center home",
                            tags=["kb", "home"])
    assert artifact["kind"] == "screenshot"
    assert artifact["caption"] == "Help center home"
    for key in ("url", "rawUrl", "sessionUrl", "downloadUrl"):
        assert artifact[key].startswith("http")
    assert f"/a/{artifact['id']}" in artifact["url"]
    assert artifact["source"] == "mcp"
    assert sorted(artifact["tags"]) == ["home", "kb"]

    projects = payload(await call(mcp_env, "list_projects"))
    assert PROJECT in [p["id"] for p in projects["items"]]

    sessions = payload(await call(mcp_env, "list_sessions", {"project": PROJECT}))
    session = next(s for s in sessions["items"] if s["name"] == SESSION)
    assert session["slug"] == "feature-yardim-merkezi"
    assert session["artifactCount"] == 1

    by_name = payload(await call(mcp_env, "list_artifacts", {"project": PROJECT, "session": SESSION}))
    assert [a["id"] for a in by_name["items"]] == [artifact["id"]]
    by_slug = payload(await call(mcp_env, "list_artifacts", {"project": PROJECT, "session": session["slug"]}))
    assert [a["id"] for a in by_slug["items"]] == [artifact["id"]]
    by_kind = payload(await call(mcp_env, "list_artifacts", {"project": PROJECT, "kind": "video"}))
    assert by_kind["items"] == []
    by_tag = payload(await call(mcp_env, "list_artifacts", {"tag": "kb"}))
    assert [a["id"] for a in by_tag["items"]] == [artifact["id"]]


async def test_upload_project_level_artifact(mcp_env: McpEnv) -> None:
    log = mcp_env.tmp / "run.log"
    log.write_text("line one\nline two\n")
    artifact = await upload(mcp_env, path=str(log), project=PROJECT, kind="log")
    assert artifact["kind"] == "log"
    assert artifact["sessionId"] is None
    assert "/s/_project/a/" in artifact["url"]


async def test_get_artifact_metadata_and_images(mcp_env: McpEnv) -> None:
    source = mcp_env.png()
    artifact = await upload(mcp_env, path=str(source), project=PROJECT, session=SESSION)

    plain = await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"]})
    assert not plain.is_error
    assert len(plain.content) == 1
    detail = json.loads(plain.content[0].text)
    assert detail["id"] == artifact["id"]
    assert detail["width"] == 4 and detail["height"] == 3

    full = await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"], "image": "full"})
    images = [block for block in full.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data) == source.read_bytes()

    thumb = await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"], "image": "thumbnail"})
    thumbs = [block for block in thumb.content if isinstance(block, ImageContent)]
    if detail.get("thumbnailUrl"):
        assert len(thumbs) == 1 and thumbs[0].mime_type == "image/jpeg"
        assert base64.b64decode(thumbs[0].data)[:2] == b"\xff\xd8"
    else:
        assert thumbs == []
        assert "no thumbnail" in text_of(thumb)

    big = mcp_env.tmp / "big.png"
    big.write_bytes(png_bytes(64, 64, noise=True))
    big_artifact = await upload(mcp_env, path=str(big), project=PROJECT, session=SESSION)
    capped = await call(mcp_env, "get_artifact", {"artifact_id": big_artifact["id"], "image": "full",
                                                   "max_image_bytes": 1024})
    assert not capped.is_error
    assert "larger than 1024 bytes" in text_of(capped)
    assert not [b for b in capped.content if isinstance(b, ImageContent) and b.mime_type == "image/png"]


async def test_get_artifact_full_on_non_image_explains(mcp_env: McpEnv) -> None:
    log = mcp_env.tmp / "console.log"
    log.write_text("hello\n")
    artifact = await upload(mcp_env, path=str(log), project=PROJECT, session=SESSION)
    result = await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"], "image": "full"})
    assert not result.is_error
    assert not [block for block in result.content if isinstance(block, ImageContent)]
    assert "not an image" in text_of(result)


async def test_share_seen_and_search(mcp_env: McpEnv) -> None:
    artifact = await upload(mcp_env, project=PROJECT, session=SESSION, caption="Invoice approval dialog")

    share = payload(await call(mcp_env, "share_artifact", {"artifact_id": artifact["id"], "expires": "7d"}))
    assert share["artifactId"] == artifact["id"]
    assert f"/s/{share['token']}" in share["url"]
    assert share["expiresAt"]
    forever = payload(await call(mcp_env, "share_artifact", {"artifact_id": artifact["id"]}))
    assert forever["expiresAt"] is None
    bad_expiry = await call(mcp_env, "share_artifact", {"artifact_id": artifact["id"], "expires": "soon"})
    assert bad_expiry.is_error

    unseen = payload(await call(mcp_env, "list_artifacts", {"project": PROJECT, "unseen": True}))
    assert artifact["id"] in [a["id"] for a in unseen["items"]]
    marked = payload(await call(mcp_env, "mark_seen", {"artifact_ids": [artifact["id"]]}))
    assert marked["seen"] is True and marked["updated"] == 1
    detail = json.loads((await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"]})).content[0].text)
    assert detail["seen"] is True
    unseen = payload(await call(mcp_env, "list_artifacts", {"project": PROJECT, "unseen": True}))
    assert artifact["id"] not in [a["id"] for a in unseen["items"]]
    payload(await call(mcp_env, "mark_seen", {"artifact_ids": [artifact["id"]], "seen": False}))
    detail = json.loads((await call(mcp_env, "get_artifact", {"artifact_id": artifact["id"]})).content[0].text)
    assert detail["seen"] is False

    hits = payload(await call(mcp_env, "search", {"query": "approval"}))
    assert [hit["artifact"]["id"] for hit in hits["items"]] == [artifact["id"]]
    assert payload(await call(mcp_env, "search", {"query": "nothingmatcheshere"}))["items"] == []


async def test_tag_and_retention_tools(mcp_env: McpEnv) -> None:
    artifact = await upload(mcp_env, project=PROJECT, session=SESSION)
    tagged = payload(await call(mcp_env, "tag_artifacts", {"artifact_ids": [artifact["id"]], "add": ["evidence"]}))
    assert tagged["items"][0]["tags"] == ["evidence"]
    untagged = payload(await call(mcp_env, "tag_artifacts", {"artifact_ids": [artifact["id"]],
                                                              "remove": ["evidence"]}))
    assert untagged["items"][0]["tags"] == []
    kept = payload(await call(mcp_env, "set_artifact_retention", {"artifact_ids": [artifact["id"]], "days": 3}))
    assert kept["items"][0]["retentionDays"] == 3
    inherited = payload(await call(mcp_env, "set_artifact_retention", {"artifact_ids": [artifact["id"]]}))
    assert inherited["items"][0]["retentionDays"] is None


async def test_notes_and_timeline(mcp_env: McpEnv) -> None:
    artifact = await upload(mcp_env, project=PROJECT, session=SESSION)
    note = payload(await call(mcp_env, "add_note", {"body": "Checked the empty state", "project": PROJECT,
                                                     "session": SESSION}))
    assert note["body"] == "Checked the empty state"

    timeline = payload(await call(mcp_env, "session_timeline", {"project": PROJECT, "session": SESSION}))
    assert timeline["session"]["name"] == SESSION
    kinds = [entry["type"] for entry in timeline["items"]]
    assert "note" in kinds and "artifact" in kinds
    assert any(entry["type"] == "artifact" and entry["artifact"]["id"] == artifact["id"] for entry in timeline["items"])

    latest = payload(await call(mcp_env, "session_timeline", {"project": PROJECT, "session": SESSION, "limit": 1}))
    assert len(latest["items"]) == 1

    missing = await call(mcp_env, "session_timeline", {"project": PROJECT, "session": "no-such-session"})
    assert missing.is_error
    assert "No session 'no-such-session'" in text_of(missing)


async def test_lease_sid_flow(mcp_env: McpEnv) -> None:
    acquired = mcp_env.client.post("/api/leases/acquire", json={
        "kind": "browser", "project": PROJECT, "session": SESSION, "instance": "mcp-test"})
    assert acquired["status"] == "granted"
    sid = acquired["sid"]

    status = payload(await call(mcp_env, "lease_status", {"sid": sid}))
    assert status["valid"] is True
    assert status["session"]["name"] == SESSION

    everything = payload(await call(mcp_env, "lease_status", {}))
    assert sid in [lease["sid"] for lease in everything["items"]]

    by_sid = await upload(mcp_env, sid=sid, caption="via sid")
    assert by_sid["leaseSid"] == sid
    assert by_sid["projectId"] == PROJECT

    note = payload(await call(mcp_env, "add_note", {"body": "from the agent", "sid": sid}))
    assert note["leaseSid"] == sid

    mcp_env.client.post(f"/api/leases/{sid}/release", json={})
    released = payload(await call(mcp_env, "lease_status", {"sid": sid}))
    assert released["valid"] is False
    assert "eks-harness lease acquire" in (released.get("reacquire") or "")

    gone = await call(mcp_env, "upload_artifact", {"path": str(mcp_env.png()), "sid": sid})
    assert gone.is_error
    assert "Reacquire with: eks-harness lease acquire" in text_of(gone)
    late_note = payload(await call(mcp_env, "add_note", {"body": "after release", "sid": sid}))
    assert late_note["leaseSid"] == sid


async def test_argument_errors_are_tool_errors(mcp_env: McpEnv) -> None:
    missing_file = await call(mcp_env, "upload_artifact", {"path": str(mcp_env.tmp / "absent.png"), "project": PROJECT})
    assert missing_file.is_error and "No such file" in text_of(missing_file)

    directory = await call(mcp_env, "upload_artifact", {"path": str(mcp_env.tmp), "project": PROJECT})
    assert directory.is_error and "is a directory" in text_of(directory)

    nowhere = await call(mcp_env, "upload_artifact", {"path": str(mcp_env.png())})
    assert nowhere.is_error and "Pass sid, or project" in text_of(nowhere)

    bad_sid = await call(mcp_env, "upload_artifact", {"path": str(mcp_env.png()), "sid": "NOT-A-SID"})
    assert bad_sid.is_error and "is not a sid" in text_of(bad_sid)

    bad_project = await call(mcp_env, "list_sessions", {"project": "no-slash"})
    assert bad_project.is_error and "invalid project id" in text_of(bad_project)

    unknown = await call(mcp_env, "get_artifact", {"artifact_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ"})
    assert unknown.is_error

    unknown_session = await call(mcp_env, "list_artifacts", {"session": SESSION})
    assert unknown_session.is_error and "No session" in text_of(unknown_session)

    no_target = await call(mcp_env, "add_note", {"body": "hi"})
    assert no_target.is_error and "Pass sid, or project and session" in text_of(no_target)

    empty_search = await call(mcp_env, "search", {"query": "  "})
    assert empty_search.is_error


async def test_daemon_down_is_a_tool_error(config: Config) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = HarnessClient(base_url=f"http://127.0.0.1:{port}", api_key="", paths=config.paths, config=config)
    server, tools = create_server(lambda: client)
    try:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool("list_projects", {})
    finally:
        tools.close()
    assert result.is_error
    assert "not reachable" in text_of(result)


async def test_auth_uses_the_callers_grants(auth_env, auth_config: Config, tmp_path: Path) -> None:
    admin_client = harness_client(auth_env.client, auth_config, auth_env.admin_key)
    member, member_key = auth_env.make_user("viewer-user")
    auth_env.grant(member, PROJECT, "viewer")
    member_client = harness_client(auth_env.client, auth_config, member_key)
    anonymous = harness_client(auth_env.client, auth_config)
    png = tmp_path / "shot.png"
    png.write_bytes(png_bytes())
    try:
        admin_server, _ = create_server(lambda: admin_client)
        async with Client(admin_server) as mcp_client:
            created = payload(await mcp_client.call_tool("upload_artifact", {
                "path": str(png), "project": PROJECT, "session": SESSION}))
            payload(await mcp_client.call_tool("upload_artifact", {"path": str(png), "project": "other/secret"}))

        member_server, _ = create_server(lambda: member_client)
        async with Client(member_server) as mcp_client:
            projects = payload(await mcp_client.call_tool("list_projects", {}))
            assert [p["id"] for p in projects["items"]] == [PROJECT]
            detail = await mcp_client.call_tool("get_artifact", {"artifact_id": created["id"]})
            assert not detail.is_error
            denied = await mcp_client.call_tool("upload_artifact", {"path": str(png), "project": PROJECT,
                                                                    "session": SESSION})
            assert denied.is_error
            hidden = await mcp_client.call_tool("list_sessions", {"project": "other/secret"})
            assert hidden.is_error

        anonymous_server, _ = create_server(lambda: anonymous)
        async with Client(anonymous_server) as mcp_client:
            refused = await mcp_client.call_tool("list_projects", {})
            assert refused.is_error
    finally:
        for client in (admin_client, member_client, anonymous):
            client.close()


@dataclass
class LiveDaemon:
    url: str
    home: Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_daemon(config: Config, harness_home: Path) -> Iterator[LiveDaemon]:
    port = _free_port()
    config.set("server.host", "127.0.0.1")
    config.set("server.port", port)
    app = make_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline and thread.is_alive():
        time.sleep(0.05)
    assert server.started, "test daemon did not start"
    try:
        yield LiveDaemon(url=f"http://127.0.0.1:{port}", home=harness_home)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


async def test_stdio_server_end_to_end(live_daemon: LiveDaemon, tmp_path: Path) -> None:
    png = tmp_path / "stdio.png"
    png.write_bytes(png_bytes())
    env = {key: value for key, value in os.environ.items() if not key.startswith("EKS_HARNESS_")}
    env.update({HOME_ENV: str(live_daemon.home), FAKE_ENV: "1", "EKS_HARNESS_URL": live_daemon.url,
                "EKS_HARNESS_SKIP_WEB": "1"})
    params = StdioServerParameters(command=sys.executable, args=["-m", "eks_harness.cli", "mcp"], env=env)
    async with Client(params) as mcp_client:
        listed = await mcp_client.list_tools()
        assert EXPECTED_TOOLS <= {tool.name for tool in listed.tools}
        created = payload(await mcp_client.call_tool("upload_artifact", {
            "path": str(png), "project": PROJECT, "session": SESSION, "caption": "over stdio"}))
        assert created["rawUrl"]
        projects = payload(await mcp_client.call_tool("list_projects", {}))
        assert PROJECT in [p["id"] for p in projects["items"]]
        full = await mcp_client.call_tool("get_artifact", {"artifact_id": created["id"], "image": "full"})
        images = [block for block in full.content if isinstance(block, ImageContent)]
        assert base64.b64decode(images[0].data) == png.read_bytes()


async def test_sessions_span_projects(mcp_env: McpEnv) -> None:
    await upload(mcp_env, project=PROJECT, session=SESSION)
    await upload(mcp_env, project="acme/mobile-app", session=SESSION)
    listed = payload(await call(mcp_env, "list_sessions", {}))["items"]
    assert [sorted(s["projectIds"]) for s in listed] == [["acme/mobile-app", PROJECT]]
    both = payload(await call(mcp_env, "list_artifacts", {"session": SESSION}))
    assert len(both["items"]) == 2
    timeline = payload(await call(mcp_env, "session_timeline", {"session": SESSION}))
    assert sum(1 for e in timeline["items"] if e["type"] == "artifact") == 2
