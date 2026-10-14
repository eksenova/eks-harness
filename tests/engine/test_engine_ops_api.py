from __future__ import annotations

import base64
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from pydantic import JsonValue

from eks_harness._version import __version__
from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.auth import PersonaAuthRegistry
from eks_harness.engine.errors import NodeFailure, PersonaAuthError
from eks_harness.engine.executors import (
    ArtifactSink,
    DaemonArtifacts,
    ExecContext,
    ExecutorRegistry,
    SessionState,
)
from eks_harness.engine.model import (
    BackendHandle,
    BackendIdentity,
    RunInfo,
    RunResources,
)
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.types import Artifact
from eks_harness.ops import (
    API_EXECUTOR,
    BUILTIN_EXECUTOR_VERSION,
    EXPECT_EXECUTOR,
    SESSION_EXECUTOR,
    WORKER_EXECUTOR,
    register_builtin_executors,
)
from eks_harness.ops.api import (
    API_OPS,
    BODY_LIMIT_BYTES,
    INFO_OP,
    NODE_HEADER,
    RUN_HEADER,
    ApiExecutor,
    BackendInfoExecutor,
    parse_body,
    query_params,
    request_url,
)
from eks_harness.pools.backend_info import (
    REDACTED,
    SECRET_MARK,
    redact_info,
    reveal_info,
)

PROJECT = "acme/api"
SESSION = "api-tests"
RUN_ID = "01JAPITESTRUN0000000000000"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
BACKEND_ID = "rback001"
PASSWORD = "hunter2-secret"
TOKEN = "tok-viewer-123"
ENV_SECRET = "envsecret-999"
ADMIN_BASIC = "Basic " + base64.b64encode(f"apiuser:{PASSWORD}".encode()).decode("ascii")
SPECIAL = 'p@ss w0rd!/\u00e9"'
SPECIAL_STJ = "p@ss w0rd!/\\u00E9\\u0022"
EXTRA_SECRET = "xtra-SECRET-1"
NESTED_SECRET = "nested-secret-2"
ENV_MARKED = "envpass-1"
SESSION_COOKIE = "sid=persona-a-session"


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class Seen:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.config: dict[str, Any] = {}

    def add(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(entry)

    def last(self) -> dict[str, Any]:
        with self.lock:
            return self.requests[-1]


class Handler(BaseHTTPRequestHandler):
    seen: Seen

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        entry = {"method": self.command, "path": parts.path, "query": parse_qsl(parts.query, keep_blank_values=True),
                 "headers": {key.lower(): value for key, value in self.headers.items()}, "body": raw,
                 "has_length": "Content-Length" in self.headers}
        self.seen.add(entry)
        route = parts.path.removeprefix("/root")
        if route == "/api/slow":
            time.sleep(2.0)
            self._reply(200, b'{"slow": true}', "application/json")
        elif route == "/api/created":
            self._reply(201, b'{"id": 7}', "application/json")
        elif route == "/api/fail":
            self._reply(500, ("server exploded " + "x" * 6000).encode(), "text/plain")
        elif route == "/api/empty":
            self._reply(200, b"", "application/json")
        elif route == "/api/text":
            self._reply(200, "merhaba dünya".encode(), "text/plain; charset=utf-8")
        elif route == "/api/redirect":
            self.send_response(302)
            self.send_header("Location", "/root/api/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif route == "/api/login":
            self.send_response(200)
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}; Path=/")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        elif route == "/api/config":
            self._reply(200, json.dumps(self.seen.config).encode(), "application/json")
        elif route == "/api/reflect":
            query = dict(entry["query"])
            body = json.loads(raw) if raw else None
            text = ('{"q":' + json.dumps(query.get("k")) + ',"body":' + json.dumps(body)
                    + ',"stj":"' + SPECIAL_STJ + '"}')
            self._reply(int(query.get("status") or 200), text.encode(), "application/json")
        elif route == "/api/echo":
            echoed = {"headers": entry["headers"], "body": raw.decode("utf-8", errors="replace"),
                      "padding": "y" * 9000}
            self._reply(200, json.dumps(echoed).encode(), "application/json")
        else:
            self._reply(200, json.dumps({"ok": True, "route": route}).encode(), "application/json")

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


@pytest.fixture
def server() -> Iterator[tuple[str, Seen]]:
    seen = Seen()
    handler = type("BoundHandler", (Handler,), {"seen": seen})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/root/", seen
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def store(db: Database, config: Config) -> DaemonArtifacts:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        sessions.ensure_in_project(conn, PROJECT, SESSION)
    return DaemonArtifacts(db, config)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []
        self.artifacts: list[Artifact] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)

    def http(self) -> list[dict[str, JsonValue]]:
        return [detail for kind, detail in self.events if kind == "node.http"]


def secrets_for(url: str) -> dict[str, JsonValue]:
    return {
        "url": url,
        "credentials": {
            "personas": {
                "admin": {"username": "apiuser", "password": PASSWORD},
                "viewer": {"token": TOKEN},
                "special": {"username": "spec", "password": SPECIAL},
            },
        },
        "env": {"API_KEY": ENV_SECRET, "PLAIN": "visible"},
        "secretEnv": ["API_KEY"],
        "version": "4.5.6",
        "extra": {"tenant": "harman"},
    }


def backend_handle(url: str) -> BackendHandle:
    identity = BackendIdentity(definition="testapi", backend_id="testapi@tree", lease_id="bl_1", source="request",
                               plugin="acme-backends", plugin_version="2.0.1", version="4.5.6", generation=3)
    secrets = secrets_for(url)
    return BackendHandle(resource_id=BACKEND_ID, session_key="main", url=url, identity=identity,
                         info=redact_info(secrets), secrets=secrets)


def make_node(op: str, args: dict[str, JsonValue], node_id: str = "n0000000001") -> Node:
    spec = default_registry().op(op)
    return Node(id=node_id, op=op, args=args, out=spec.out, hash=HASH_A, uses=[BACKEND_ID])


def make_plan(*nodes: Node) -> Plan:
    resources = [Resource(id=BACKEND_ID, kind="backend", app="web-app", session="main", definition="testapi")]
    return Plan(kind="flow", name="api", session=SessionSpec(app="web-app", platform="web"), resources=resources,
                nodes=list(nodes), hash=HASH_B)


def make_call(node: Node, store: DaemonArtifacts, handle: BackendHandle, *, sink: RecordingSink | None = None,
              persona: str | None = None, instance: str = "", deadline: float | None = None,
              cancel: threading.Event | None = None) -> ExecContext:
    plan = make_plan(node)
    registry = default_registry()
    spec = registry.op(node.op)
    resolved = validate_args(spec, node.args)
    info = RunInfo(id=RUN_ID, project=PROJECT, session=SESSION, plan_hash=plan.hash, plan_kind=plan.kind,
                   plan_name=plan.name, actor="tester", created_at=time.time())
    recording = sink or RecordingSink()
    artifacts = ArtifactSink(store, run=info, node_id=node.id, instance=instance, op=node.op, sink=recording)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance=instance, attempt=1,
                       args=resolved.model, data=resolved.data, session=plan.session,
                       state=SessionState("main", persona=persona),
                       resources=RunResources(backends={BACKEND_ID: handle}), driver=None, sink=recording,
                       artifacts=artifacts, seed=0, clock=plan.clock, deadline=deadline,
                       cancel=cancel or threading.Event())


@pytest.fixture
def executor() -> Iterator[ApiExecutor]:
    client = httpx.Client(follow_redirects=False)
    try:
        yield ApiExecutor(PersonaAuthRegistry(), client=client)
    finally:
        client.close()


def test_request_url_and_query_params() -> None:
    assert request_url("http://h:1/root/", "/api/x") == "http://h:1/root/api/x"
    assert request_url("http://h:1/root", "api/x") == "http://h:1/root/api/x"
    assert request_url("http://h:1", "/api/x?a=1") == "http://h:1/api/x?a=1"
    for bad in ("http://evil/api", "https://h/api", "//evil/api", "file:///etc/passwd"):
        with pytest.raises(NodeFailure) as caught:
            request_url("http://h:1/root/", bad)
        assert caught.value.code == "args_invalid"
    assert query_params(None) == []
    assert query_params({"a": 1, "b": True, "c": False, "tags": ["x", 2, None, True], "obj": {"k": [1, "v"]},
                         "gone": None, "f": 1.5, "s": "t x"}) == [
        ("a", "1"), ("b", "true"), ("c", "false"), ("tags", "x"), ("tags", "2"), ("tags", "true"),
        ("obj", '{"k":[1,"v"]}'), ("f", "1.5"), ("s", "t x"),
    ]
    assert query_params({"nested": [{"a": 1}, [1, 2]]}) == [("nested", '{"a":1}'), ("nested", "[1,2]")]


def test_parse_body() -> None:
    def response(content: bytes, content_type: str | None) -> httpx.Response:
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, content=content, headers=headers)

    assert parse_body(response(b'{"a": [1, 2]}', "application/json")) == {"a": [1, 2]}
    assert parse_body(response(b"[1, 2]", "text/plain")) == [1, 2]
    assert parse_body(response(b"", "application/json")) is None
    assert parse_body(response(b"  \n", None)) is None
    assert parse_body(response("merhaba dünya".encode(), "text/plain; charset=utf-8")) == "merhaba dünya"
    assert parse_body(response(b"{broken", "application/json")) == "{broken"
    assert parse_body(response(b"NaN", "application/json")) == "NaN"


def test_get_sends_query_headers_and_basic_auth(server: tuple[str, Seen], store: DaemonArtifacts,
                                                executor: ApiExecutor) -> None:
    url, seen = server
    node = make_node("api.get", {"path": "/api/x", "persona": "admin",
                                 "query": {"a": 1, "b": True, "c": False, "tags": ["x", "y"], "obj": {"k": 1},
                                           "gone": None},
                                 "headers": {"X-Custom": "hello"}})
    result = executor.run(make_call(node, store, backend_handle(url)))
    assert result.value == {"ok": True, "route": "/api/x"}
    request = seen.last()
    assert request["method"] == "GET"
    assert request["path"] == "/root/api/x"
    assert request["query"] == [("a", "1"), ("b", "true"), ("c", "false"), ("tags", "x"), ("tags", "y"),
                                ("obj", '{"k":1}')]
    headers = request["headers"]
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == f"eks-harness/{__version__}"
    assert headers[RUN_HEADER.lower()] == RUN_ID
    assert headers[NODE_HEADER.lower()] == "n0000000001"
    assert headers["x-custom"] == "hello"
    assert headers["authorization"] == ADMIN_BASIC
    assert request["body"] == b""


def test_node_header_names_the_instance(server: tuple[str, Seen], store: DaemonArtifacts,
                                        executor: ApiExecutor) -> None:
    url, seen = server
    node = make_node("api.get", {"path": "api/x"})
    executor.run(make_call(node, store, backend_handle(url), instance="n0000000009#2"))
    assert seen.last()["headers"][NODE_HEADER.lower()] == "n0000000001@n0000000009#2"


@pytest.mark.parametrize("op", ["api.post", "api.put", "api.patch"])
def test_body_methods_send_json(op: str, server: tuple[str, Seen], store: DaemonArtifacts,
                                executor: ApiExecutor) -> None:
    url, seen = server
    node = make_node(op, {"path": "/api/items", "json": {"name": "Moda", "count": 2, "tags": ["a"]}})
    result = executor.run(make_call(node, store, backend_handle(url)))
    assert result.value == {"ok": True, "route": "/api/items"}
    request = seen.last()
    assert request["method"] == op.rpartition(".")[2].upper()
    assert json.loads(request["body"]) == {"name": "Moda", "count": 2, "tags": ["a"]}
    assert request["headers"]["content-type"] == "application/json"


def test_delete_sends_no_body_and_bodies_parse(server: tuple[str, Seen], store: DaemonArtifacts,
                                               executor: ApiExecutor) -> None:
    url, seen = server
    handle = backend_handle(url)
    executor.run(make_call(make_node("api.delete", {"path": "/api/items/1"}), store, handle))
    request = seen.last()
    assert request["method"] == "DELETE" and request["body"] == b""
    assert "content-type" not in request["headers"]
    empty = executor.run(make_call(make_node("api.post", {"path": "/api/empty"}), store, handle))
    assert empty.value is None
    assert seen.last()["body"] == b""
    text = executor.run(make_call(make_node("api.get", {"path": "/api/text"}), store, handle))
    assert text.value == "merhaba dünya"


def test_expect_status(server: tuple[str, Seen], store: DaemonArtifacts, executor: ApiExecutor) -> None:
    url, _seen = server
    handle = backend_handle(url)
    created = executor.run(make_call(make_node("api.post", {"path": "/api/created", "expect_status": [200, 201]}),
                                     store, handle))
    assert created.value == {"id": 7}
    with pytest.raises(NodeFailure) as caught:
        executor.run(make_call(make_node("api.get", {"path": "/api/created", "expect_status": [200]}), store, handle))
    assert caught.value.code == "http_status"
    assert caught.value.detail["status"] == 201
    with pytest.raises(NodeFailure) as failed:
        executor.run(make_call(make_node("api.get", {"path": "/api/fail"}), store, handle))
    problem = failed.value
    assert problem.code == "http_status" and not problem.retryable
    assert "returned 500" in problem.message and "any 2xx" in problem.message
    assert problem.detail["method"] == "GET"
    assert problem.detail["url"] == url + "api/fail"
    assert problem.detail["status"] == 500
    body = problem.detail["body"]
    assert isinstance(body, str) and body.startswith("server exploded")
    assert len(body.encode()) == BODY_LIMIT_BYTES
    with pytest.raises(NodeFailure) as redirected:
        executor.run(make_call(make_node("api.get", {"path": "/api/redirect"}), store, handle))
    assert redirected.value.detail["status"] == 302
    accepted = executor.run(make_call(make_node("api.get", {"path": "/api/fail", "expect_status": [500]}), store,
                                      handle))
    assert isinstance(accepted.value, str) and accepted.value.startswith("server exploded")


def test_closed_port_is_a_retryable_http_error(store: DaemonArtifacts, executor: ApiExecutor) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sink = RecordingSink()
    node = make_node("api.get", {"path": "/api/x"})
    with pytest.raises(NodeFailure) as caught:
        executor.run(make_call(node, store, backend_handle(f"http://127.0.0.1:{port}/"), sink=sink))
    assert caught.value.code == "http_error"
    assert caught.value.retryable
    assert caught.value.detail["url"] == f"http://127.0.0.1:{port}/api/x"
    [http] = sink.http()
    assert http["status"] is None and isinstance(http["error"], str)


def test_persona_choice_and_missing_credentials(server: tuple[str, Seen], store: DaemonArtifacts,
                                                executor: ApiExecutor) -> None:
    url, seen = server
    handle = backend_handle(url)
    explicit = make_node("api.get", {"path": "/api/x", "persona": "admin"})
    call = make_call(explicit, store, handle, persona="viewer")
    assert executor.persona(call) == "admin"
    executor.run(call)
    assert seen.last()["headers"]["authorization"] == ADMIN_BASIC

    from_state = make_call(make_node("api.get", {"path": "/api/x"}), store, handle, persona="viewer")
    assert executor.persona(from_state) == "viewer"
    executor.run(from_state)
    assert seen.last()["headers"]["authorization"] == f"Bearer {TOKEN}"

    anonymous = make_call(make_node("api.get", {"path": "/api/x"}), store, handle)
    assert executor.persona(anonymous) is None
    executor.run(anonymous)
    assert "authorization" not in seen.last()["headers"]

    count = len(seen.requests)
    with pytest.raises(PersonaAuthError) as caught:
        executor.run(make_call(make_node("api.get", {"path": "/api/x", "persona": "ghost"}), store, handle))
    assert caught.value.code == "persona_auth"
    assert "ghost" in caught.value.message
    assert len(seen.requests) == count


def test_http_events_are_redacted_and_bounded(server: tuple[str, Seen], store: DaemonArtifacts,
                                              executor: ApiExecutor) -> None:
    url, _seen = server
    handle = backend_handle(url)
    sink = RecordingSink()
    node = make_node("api.post", {"path": "/api/echo", "persona": "admin", "query": {"k": ENV_SECRET},
                                  "json": {"password": PASSWORD, "note": "hello"}})
    result = executor.run(make_call(node, store, handle, sink=sink))
    assert isinstance(result.value, dict)
    [http] = sink.http()
    assert http["method"] == "POST"
    assert http["status"] == 200
    assert isinstance(http["ms"], int) and http["ms"] >= 0
    assert isinstance(http["bytes"], int) and http["bytes"] > BODY_LIMIT_BYTES
    assert http["url"] == f"{url}api/echo?k={REDACTED}"
    request_excerpt, response_excerpt = http["request"], http["response"]
    assert isinstance(request_excerpt, str) and isinstance(response_excerpt, str)
    assert json.loads(request_excerpt) == {"password": REDACTED, "note": "hello"}
    assert len(response_excerpt.encode()) <= BODY_LIMIT_BYTES
    assert REDACTED in response_excerpt
    journaled = json.dumps(sink.events)
    for secret in (PASSWORD, ENV_SECRET, ADMIN_BASIC, ADMIN_BASIC.removeprefix("Basic ")):
        assert secret not in journaled

    bearer = RecordingSink()
    executor.run(make_call(make_node("api.get", {"path": "/api/echo", "headers": {"X-Api-Token": "manual-777"}}),
                           store, handle, sink=bearer, persona="viewer"))
    shown = json.dumps(bearer.events)
    assert TOKEN not in shown and "manual-777" not in shown
    assert REDACTED in shown


def test_scheme_paths_and_deadlines(server: tuple[str, Seen], store: DaemonArtifacts,
                                    executor: ApiExecutor) -> None:
    url, seen = server
    handle = backend_handle(url)
    with pytest.raises(NodeFailure) as refused:
        executor.run(make_call(make_node("api.get", {"path": "http://elsewhere/api"}), store, handle))
    assert refused.value.code == "args_invalid"
    assert seen.requests == []

    started = time.monotonic()
    with pytest.raises(NodeFailure) as late:
        executor.run(make_call(make_node("api.get", {"path": "/api/slow"}), store, handle,
                               deadline=time.monotonic() + 0.3))
    assert time.monotonic() - started < 1.5
    assert late.value.code == "timeout" and not late.value.retryable

    short = ApiExecutor(PersonaAuthRegistry(), timeout=0.3)
    started = time.monotonic()
    with pytest.raises(NodeFailure) as slow:
        short.run(make_call(make_node("api.get", {"path": "/api/slow"}), store, handle))
    assert time.monotonic() - started < 1.5
    assert slow.value.code == "http_error" and slow.value.retryable

    with pytest.raises(NodeFailure) as expired:
        executor.run(make_call(make_node("api.get", {"path": "/api/x"}), store, handle,
                               deadline=time.monotonic() - 1))
    assert expired.value.code == "timeout"

    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(NodeFailure) as cancelled:
        executor.run(make_call(make_node("api.get", {"path": "/api/slow"}), store, handle, cancel=cancel))
    assert time.monotonic() - started < 1.5
    assert cancelled.value.code == "cancelled"


def test_shared_client_is_created_once(server: tuple[str, Seen], store: DaemonArtifacts) -> None:
    url, seen = server
    shared = ApiExecutor(PersonaAuthRegistry())
    handle = backend_handle(url)
    outcomes: list[JsonValue] = []

    def call_once() -> None:
        outcomes.append(shared.run(make_call(make_node("api.get", {"path": "/api/x"}), store, handle)).value)

    threads = [threading.Thread(target=call_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == [{"ok": True, "route": "/api/x"}] * 4
    assert len(seen.requests) == 4
    with pytest.raises(NodeFailure) as redirected:
        shared.run(make_call(make_node("api.get", {"path": "/api/redirect"}), store, handle))
    assert redirected.value.detail["status"] == 302


def test_backend_info_is_redacted(store: DaemonArtifacts) -> None:
    handle = backend_handle("http://127.0.0.1:9/root/")
    info = BackendInfoExecutor()
    value = info.run(make_call(make_node(INFO_OP, {}), store, handle)).value
    assert isinstance(value, dict)
    expected = redact_info(secrets_for("http://127.0.0.1:9/root/"))
    assert {key: item for key, item in value.items() if key != "backend"} == expected
    assert value["backend"] == {"id": "testapi@tree", "definition": "testapi", "generation": 3,
                                "plugin": "acme-backends", "version": "2.0.1"}
    shown = json.dumps(value)
    for secret in (PASSWORD, TOKEN, ENV_SECRET):
        assert secret not in shown
    assert value["env"] == {"API_KEY": REDACTED, "PLAIN": "visible"}

    bare = handle.model_copy(update={"info": {}})
    with pytest.raises(NodeFailure) as missing:
        info.run(make_call(make_node(INFO_OP, {}), store, bare))
    assert missing.value.code == "resource_error"
    assert "testapi@tree" in missing.value.message
    assert missing.value.detail == {"backend": "testapi@tree", "resource": BACKEND_ID}


def test_register_builtin_executors() -> None:
    executors = ExecutorRegistry()
    assert register_builtin_executors(executors, PersonaAuthRegistry(), api_timeout=5.0) is executors
    specs = executors.specs()
    assert [(spec.name, spec.ops) for spec in specs[:6]] == [
        (API_EXECUTOR, API_OPS),
        (API_EXECUTOR, (INFO_OP,)),
        (SESSION_EXECUTOR, ("session.as",)),
        (EXPECT_EXECUTOR, ("expect.value",)),
        (WORKER_EXECUTOR, ("*",)),
        (WORKER_EXECUTOR + ".container", ("*",)),
    ]
    assert [spec.name for spec in specs[6:]] == ["eks.media.source", "eks.media.markers", "eks.media.tts",
                                                 "eks.media.stems", "eks.audio", "eks.gfx", "eks.edit",
                                                 "eks.check.media", "eks.overlay"]
    for spec in specs:
        assert spec.version == BUILTIN_EXECUTOR_VERSION
        assert spec.provenance.plugin == "eks"
        assert spec.description
    assert specs[4].locations == frozenset({"worker"}) and specs[4].run is not None
    assert specs[5].locations == frozenset({"worker"}) and specs[5].container is not None
    catalog = default_registry()

    def chosen(op: str) -> str | None:
        choice = executors.choose(catalog.op(op))
        return choice.executor.name if choice is not None else None

    assert chosen("api.get") == API_EXECUTOR
    assert chosen("api.delete") == API_EXECUTOR
    assert chosen("backend.info") == API_EXECUTOR
    assert chosen("session.as") == SESSION_EXECUTOR
    assert chosen("expect.value") == EXPECT_EXECUTOR
    assert chosen("ui.tap") == WORKER_EXECUTOR
    assert chosen("check.checkpoint") == WORKER_EXECUTOR
    assert chosen("capture.record") == WORKER_EXECUTOR + ".container"
    assert chosen("edit.render") == "eks.edit"
    assert chosen("gfx.render") == "eks.gfx"
    with pytest.raises(ValueError):
        register_builtin_executors(executors, PersonaAuthRegistry())


def marked_handle(url: str) -> BackendHandle:
    stored: dict[str, Any] = {
        "url": url,
        "credentials": {"personas": {"admin": {"username": "apiuser", "password": {SECRET_MARK: PASSWORD}}}},
        "extra": {"apiKey": {SECRET_MARK: EXTRA_SECRET}, "nested": {SECRET_MARK: {"inner": NESTED_SECRET}},
                  "tenant": "harman"},
        "env": {"DB_PASS": {SECRET_MARK: ENV_MARKED}, "PLAIN": "visible"},
    }
    identity = BackendIdentity(definition="testapi", backend_id="testapi@tree", lease_id="bl_1", source="request",
                               plugin="acme-backends", plugin_version="2.0.1", generation=1)
    return BackendHandle(resource_id=BACKEND_ID, session_key="main", url=url, identity=identity,
                         info=redact_info(stored), secrets=reveal_info(stored))


def test_cookies_never_cross_requests(server: tuple[str, Seen], store: DaemonArtifacts,
                                      executor: ApiExecutor) -> None:
    url, seen = server
    handle = backend_handle(url)
    for chosen in (ApiExecutor(PersonaAuthRegistry()), executor):
        chosen.run(make_call(make_node("api.get", {"path": "/api/login", "persona": "admin"}), store, handle))
        chosen.run(make_call(make_node("api.get", {"path": "/api/other", "persona": "admin"}), store, handle))
        assert "cookie" not in seen.last()["headers"]
        chosen.run(make_call(make_node("api.get", {"path": "/api/other"}), store, handle, persona="viewer"))
        assert "cookie" not in seen.last()["headers"]
        assert seen.last()["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert all("cookie" not in request["headers"] for request in seen.requests)


def test_marked_extra_and_env_secrets_are_redacted(server: tuple[str, Seen], store: DaemonArtifacts,
                                                    executor: ApiExecutor) -> None:
    url, seen = server
    seen.config = {"apiKey": EXTRA_SECRET, "inner": NESTED_SECRET, "db": f"Server=sql;Password={ENV_MARKED}",
                   "tenant": "harman"}
    handle = marked_handle(url)
    sink = RecordingSink()
    result = executor.run(make_call(make_node("api.get", {"path": "/api/config"}), store, handle, sink=sink))
    assert result.value == seen.config
    [http] = sink.http()
    excerpt = http["response"]
    assert isinstance(excerpt, str)
    assert json.loads(excerpt) == {"apiKey": REDACTED, "inner": REDACTED, "db": f"Server=sql;Password={REDACTED}",
                                   "tenant": "harman"}
    with pytest.raises(NodeFailure) as failed:
        executor.run(make_call(make_node("api.get", {"path": "/api/config", "expect_status": [404]}), store,
                               handle, sink=sink))
    journaled = json.dumps([sink.events, failed.value.message, failed.value.detail])
    for secret in (EXTRA_SECRET, NESTED_SECRET, ENV_MARKED, PASSWORD):
        assert secret not in journaled

    value = BackendInfoExecutor().run(make_call(make_node(INFO_OP, {}), store, handle)).value
    assert isinstance(value, dict)
    assert value["extra"] == {"apiKey": REDACTED, "nested": REDACTED, "tenant": "harman"}
    assert value["env"] == {"DB_PASS": REDACTED, "PLAIN": "visible"}
    shown = json.dumps(value)
    for secret in (EXTRA_SECRET, NESTED_SECRET, ENV_MARKED, PASSWORD):
        assert secret not in shown


def test_encoded_and_escaped_secrets_are_redacted(server: tuple[str, Seen], store: DaemonArtifacts,
                                                  executor: ApiExecutor) -> None:
    url, seen = server
    handle = backend_handle(url)
    sink = RecordingSink()
    node = make_node("api.post", {"path": "/api/reflect", "persona": "special", "query": {"k": SPECIAL},
                                  "json": {"password": SPECIAL}})
    result = executor.run(make_call(node, store, handle, sink=sink))
    assert result.value == {"q": SPECIAL, "body": {"password": SPECIAL}, "stj": SPECIAL}
    request = seen.last()
    assert request["query"] == [("k", SPECIAL)]
    assert json.loads(request["body"]) == {"password": SPECIAL}
    [http] = sink.http()
    assert http["url"] == f"{url}api/reflect?k={REDACTED}"
    request_excerpt, response_excerpt = http["request"], http["response"]
    assert isinstance(request_excerpt, str) and isinstance(response_excerpt, str)
    assert json.loads(request_excerpt) == {"password": REDACTED}
    assert json.loads(response_excerpt) == {"q": REDACTED, "body": {"password": REDACTED}, "stj": REDACTED}

    failing = make_node("api.post", {"path": "/api/reflect", "persona": "special",
                                     "query": {"k": SPECIAL, "status": 418}, "json": {"password": SPECIAL}})
    with pytest.raises(NodeFailure) as failed:
        executor.run(make_call(failing, store, handle, sink=sink))
    assert failed.value.code == "http_status"
    assert failed.value.detail["url"] == f"{url}api/reflect?k={REDACTED}&status=418"
    body = failed.value.detail["body"]
    assert isinstance(body, str) and json.loads(body)["q"] == REDACTED
    journaled = [json.dumps(sink.events), json.dumps(sink.events, ensure_ascii=False), failed.value.message,
                 json.dumps(failed.value.detail, ensure_ascii=False)]
    forms = (SPECIAL, "p%40ss", "p@ss w0rd", SPECIAL_STJ)
    for text in journaled:
        for form in forms:
            assert form not in text
