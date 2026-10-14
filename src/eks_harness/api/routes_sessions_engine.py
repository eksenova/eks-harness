from __future__ import annotations

import json
import keyword
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, JsonValue
from starlette.concurrency import run_in_threadpool

from eks.session import coerce_target_args
from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import bad_request
from eks_harness.api.routes_plans import authorize_tree
from eks_harness.api.routes_runs import authorize_run
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.engine.build import BuildRequest
from eks_harness.engine.errors import BuildError
from eks_harness.engine.model import (
    ArtifactRef,
    BackendRef,
    ControlRequest,
    EngineModel,
    FakeProfile,
    IssueOut,
    NodeError,
    NodeRecord,
    PauseInfo,
    Platform,
    RunOptions,
    RunRequest,
    RunStatus,
    RunSummary,
)
from eks_harness.engine.runs import RunManager, engine
from eks_harness.ir.registry import Registry
from eks_harness.ir.surface import SURFACE, call_text, op_text
from eks_harness.ir.target import Target

router = APIRouter(tags=["sessions"])
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

SESSIONS_PATH = "/api/engine/sessions"
SESSION_EVENT = "session.opened"
SESSION_LABEL = "session"
SESSION_ENTRY = "session"
ADHOC_PREFIX = "adhoc:"
WAIT_LIMIT = 3600.0
CLOSE_WAIT = 120.0
DO_SLACK = 30.0

_ENDED: frozenset[RunStatus] = frozenset({"passed", "failed", "aborted", "error"})
_KEPT = frozenset({"passed", "soft_failed"})
_EVENT_PAGE = 200
_EXPORT_ROOTS = ("s.", "expect")
_NAME_CHARS = re.compile(r"[^0-9A-Za-z_]+")
_IMPORTS = (("by", re.compile(r"\bby\.")), ("expect", re.compile(r"\bexpect\b")), ("t", re.compile(r"\bt\(")))
_PLATFORMS: dict[str, Platform] = {"web": "web", "ios": "ios", "android": "android"}


class SessionOpen(EngineModel):
    app: str = Field(min_length=1, description="App profile of the session")
    platform: Platform | None = Field(default=None, description="Platform; the app's first platform when unset")
    seed: str | None = Field(default=None, description="Seed the backend is restored to")
    persona: str | None = Field(default=None, description="Persona the session acts as")
    backend: BackendRef | None = Field(default=None, description="Backend lease id, backend id or definition")
    viewport: str | None = Field(default=None, description="Viewport preset")
    tree: str | None = Field(default=None, description="Absolute work tree path on the hub")
    project: str | None = Field(default=None, description="Project owner/name; the tree's when unset")
    session: str | None = Field(default=None, description="Store session; the tree's branch when unset")
    instance: str | None = Field(default=None, description="Harness instance opening the session")
    fakes: FakeProfile | None = Field(default=None, description="Simulate ops without real executors")
    wait: float = Field(default=120.0, ge=0, le=WAIT_LIMIT, description="Seconds to wait for the session to be ready")


class SessionOpened(EngineModel):
    session: str = Field(description="Session id, the id of its run")
    run: RunSummary = Field(description="The session's run")
    pause: PauseInfo | None = Field(default=None, description="The pause the session waits in")
    issues: list[IssueOut] = Field(default_factory=list, description="Admission warnings")


class SessionDo(EngineModel):
    op: str = Field(min_length=1, description="Op name such as ui.tap")
    args: dict[str, JsonValue] = Field(default_factory=dict, description="Op arguments; target fields take shorthand strings")


class SessionDone(EngineModel):
    ok: bool = Field(description="Whether the op ran and passed")
    value: JsonValue = Field(default=None, description="Value of the op")
    message: str = Field(default="", description="What happened")
    status: RunStatus = Field(description="Status of the session's run")
    node: str | None = Field(default=None, description="Journal node of the op, adhoc:<n>")
    error: NodeError | None = Field(default=None, description="Failure of the op")
    artifacts: list[ArtifactRef] = Field(default_factory=list, description="Artifacts the op produced")


class SessionClosed(EngineModel):
    ok: bool = Field(description="Whether the session ended")
    message: str = Field(default="", description="What happened")
    run: RunSummary = Field(description="The session's run")


class ExportedOp(EngineModel):
    node: str = Field(description="Journal node of the op")
    op: str = Field(description="Op name")
    call: str = Field(description="Python line written for it")
    surface: bool = Field(description="Whether the line is the op's authoring call instead of s.op(...)")


class SkippedOp(EngineModel):
    node: str = Field(description="Journal node of the op")
    op: str = Field(description="Op name")
    status: str = Field(description="Its status; only passed and soft-failed ops are exported")


class SessionExport(EngineModel):
    name: str = Field(description="Flow name")
    source: str = Field(description="Python source of the flow")
    validation: dict[str, Any] = Field(description="Plan validation: ok, hash and issues")
    ops: list[ExportedOp] = Field(default_factory=list, description="Exported ops in order")
    skipped: list[SkippedOp] = Field(default_factory=list, description="Ops left out")


def _decorator(options: Mapping[str, JsonValue]) -> str:
    parts = [f"app={json.dumps(options['app'], ensure_ascii=False)}"]
    for key, name in (("platform", "platform"), ("seed", "seed"), ("persona", "as_"), ("viewport", "viewport")):
        value = options.get(key)
        if isinstance(value, str):
            parts.append(f"{name}={json.dumps(value, ensure_ascii=False)}")
    return f"@eks.flow({', '.join(parts)})"


def flow_source(options: Mapping[str, JsonValue], body: Sequence[str], name: str = SESSION_ENTRY) -> str:
    text = "\n".join(body)
    imports = [name for name, pattern in _IMPORTS if pattern.search(text)]
    lines = ["import eks"]
    if imports:
        lines.append(f"from eks import {', '.join(imports)}")
    lines += ["", "", _decorator(options), f"def {name}(s: eks.Session):"]
    lines += [f"    {line}" for line in body] or ["    pass"]
    return "\n".join(lines) + "\n"


def flow_name(name: str | None) -> str:
    folded = unicodedata.normalize("NFKD", (name or SESSION_ENTRY).strip().replace("ı", "i"))
    plain = folded.encode("ascii", "ignore").decode("ascii")
    cleaned = _NAME_CHARS.sub("_", plain).strip("_").lower()
    if not cleaned:
        cleaned = SESSION_ENTRY
    if not cleaned.isidentifier() or keyword.iskeyword(cleaned):
        cleaned = f"flow_{cleaned}"
    return cleaned


def _options(body: SessionOpen) -> dict[str, JsonValue]:
    return {"app": body.app, "platform": body.platform, "seed": body.seed, "persona": body.persona,
            "viewport": body.viewport, "backend": body.backend.ref if body.backend is not None else None}


def open_body(options: Mapping[str, JsonValue]) -> list[str]:
    needs_backend = options.get("backend") is not None and not options.get("seed") and not options.get("persona")
    return [*(["s.backend.info"] if needs_backend else []), f"s.debug.break_({json.dumps(SESSION_LABEL)})"]


def _tree(text: str | None) -> Path | None:
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise bad_request(f"tree {text} must be an absolute path on the hub", error="bad_tree")
    return path.resolve()


def _session_options(manager: RunManager, run_id: str) -> dict[str, JsonValue]:
    after: int | None = None
    while True:
        events = manager.journal.events(run_id, after=after, limit=_EVENT_PAGE)
        for event in events:
            if event.type == SESSION_EVENT:
                return dict(event.detail)
        if len(events) < _EVENT_PAGE:
            raise bad_request(f"run {run_id} is not a session; open one with POST {SESSIONS_PATH}",
                              error="not_a_session", run=run_id)
        after = events[-1].id


def _adhoc_nodes(manager: RunManager, run_id: str) -> list[NodeRecord]:
    return sorted((node for node in manager.journal.nodes(run_id) if node.node_id.startswith(ADHOC_PREFIX)),
                  key=lambda node: node.seq)


def _registry(manager: RunManager, tree: str | None) -> Registry:
    return manager.registries.for_tree(Path(tree) if tree else None).registry


def _jsonable(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value if value is None or isinstance(value, str | int | float | bool) else str(value)


def _coerced(registry: Registry, op: str, args: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    if not registry.has_op(op):
        return dict(args)
    spec = registry.op(op)
    coerced = coerce_target_args(op, spec.args, args, registry.target_kinds())
    return {key: value.model_dump(mode="json", by_alias=True, exclude_none=True) if isinstance(value, Target)
            else _jsonable(value) for key, value in coerced.items()}


def open_session(ctx: AppContext, body: SessionOpen, principal: Principal) -> SessionOpened:
    manager = engine(ctx)
    tree = _tree(body.tree)
    if not principal.is_admin:
        authorize_tree(ctx, principal, tree, "editor")
    options = _options(body)
    timeout = float(ctx.config["engine.sessionTimeoutSeconds"])
    request = RunRequest(source=flow_source(options, open_body(options)), entry=SESSION_ENTRY, folder="flows",
                         tree=str(tree) if tree is not None else None,
                         cwd=str(tree) if tree is not None else None, platform=body.platform,
                         project=body.project, session=body.session, instance=body.instance,
                         options=RunOptions(debug=True, pause_timeout=timeout), backend=body.backend,
                         fakes=body.fakes)
    started = manager.start(request, principal)
    manager.note(started.id, SESSION_EVENT, {**options, "tree": str(tree) if tree is not None else None})
    summary = manager.wait(started.id, body.wait)
    issues = [IssueOut.model_validate(event.detail) for event in manager.journal.events(started.id, limit=_EVENT_PAGE)
              if event.type == "run.warning"]
    return SessionOpened(session=started.id, run=summary, pause=summary.pause, issues=issues)


def session_do(ctx: AppContext, run_id: str, body: SessionDo, actor: str | None) -> SessionDone:
    manager = engine(ctx)
    options = _session_options(manager, run_id)
    tree = options.get("tree")
    registry = _registry(manager, tree if isinstance(tree, str) else None)
    try:
        args = _coerced(registry, body.op, body.args)
    except (TypeError, ValueError) as error:
        raise bad_request(str(error), error="bad_args") from error
    known = {node.node_id for node in _adhoc_nodes(manager, run_id)}
    timeout = float(ctx.config["engine.nodeTimeoutSeconds"]) + DO_SLACK
    response = manager.control(run_id, ControlRequest(action="do", op=body.op, args=args), actor, timeout=timeout)
    fresh = [node for node in _adhoc_nodes(manager, run_id) if node.node_id not in known]
    node = fresh[-1] if fresh else None
    artifacts: list[ArtifactRef] = []
    if node is not None and node.artifacts:
        artifacts = [artifact for artifact in manager.report(run_id).artifacts if artifact.node == node.node_id]
    return SessionDone(ok=response.ok and (node is None or node.status in _KEPT), value=response.result,
                       message=response.message, status=response.status,
                       node=node.node_id if node is not None else None,
                       error=node.error if node is not None else None, artifacts=artifacts)


def close_session(ctx: AppContext, run_id: str, actor: str | None) -> SessionClosed:
    manager = engine(ctx)
    _session_options(manager, run_id)
    current = manager.status(run_id)
    if current.status in _ENDED:
        return SessionClosed(ok=True, message=f"the session already ended {current.status}", run=current)
    response = manager.control(run_id, ControlRequest(action="continue"), actor)
    if not response.ok:
        ended = manager.cancel(run_id, f"session closed by {actor or 'its owner'}", actor)
        return SessionClosed(ok=ended.status in _ENDED, message=response.message, run=ended)
    ended = manager.wait(run_id, CLOSE_WAIT, until=_ENDED)
    return SessionClosed(ok=ended.status in _ENDED, message=response.message, run=ended)


def _line(op: str, args: Mapping[str, JsonValue], registry: Registry) -> tuple[str, bool]:
    entry = SURFACE.get(op)
    if entry is not None and entry.path.startswith(_EXPORT_ROOTS):
        rendered = call_text(op, args, registry=registry)
        if rendered is not None:
            return rendered, True
    return op_text(op, args), False


def export_session(ctx: AppContext, run_id: str, name: str | None, principal: Principal) -> SessionExport:
    manager = engine(ctx)
    options = _session_options(manager, run_id)
    tree_text = options.get("tree")
    tree = Path(tree_text) if isinstance(tree_text, str) else None
    if not principal.is_admin:
        authorize_tree(ctx, principal, tree, "viewer")
    registry = _registry(manager, str(tree) if tree is not None else None)
    ops: list[ExportedOp] = []
    skipped: list[SkippedOp] = []
    for node in _adhoc_nodes(manager, run_id):
        if node.status not in _KEPT:
            skipped.append(SkippedOp(node=node.node_id, op=node.op, status=node.status))
            continue
        args = node.args if isinstance(node.args, dict) else {}
        line, surface = _line(node.op, args, registry)
        ops.append(ExportedOp(node=node.node_id, op=node.op, call=line, surface=surface))
    flow = flow_name(name)
    source = flow_source(options, [item.call for item in ops], flow)
    platform = options.get("platform")
    try:
        built = manager.builder.build(BuildRequest(source=source, entry=flow, folder="flows", tree=tree, cwd=tree,
                                                   platform=_PLATFORMS.get(platform) if isinstance(platform, str)
                                                   else None))
        issues = [issue.model_dump(mode="json", by_alias=True) for issue in built.issues]
        validation: dict[str, Any] = {"ok": not any(issue.severity == "error" for issue in built.issues),
                                      "hash": built.plan.hash, "issues": issues}
    except BuildError as error:
        issue = IssueOut(severity="error", code=error.code, message=error.message)
        validation = {"ok": False, "hash": "", "issues": [issue.model_dump(mode="json", by_alias=True)],
                      "where": error.where}
    return SessionExport(name=flow, source=source, validation=validation, ops=ops, skipped=skipped)


@router.post(SESSIONS_PATH, response_model=SessionOpened, status_code=201)
async def create_session(body: SessionOpen, request: Request, principal: CurrentPrincipal) -> SessionOpened:
    return await run_in_threadpool(open_session, get_ctx(request), body, principal)


@router.post(SESSIONS_PATH + "/{run_id}/do", response_model=SessionDone)
async def do_in_session(run_id: str, body: SessionDo, request: Request, principal: CurrentPrincipal) -> SessionDone:
    ctx = get_ctx(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id, "editor")
    return await run_in_threadpool(session_do, ctx, run_id, body, principal.username)


@router.delete(SESSIONS_PATH + "/{run_id}", response_model=SessionClosed)
async def delete_session(run_id: str, request: Request, principal: CurrentPrincipal) -> SessionClosed:
    ctx = get_ctx(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id, "editor")
    return await run_in_threadpool(close_session, ctx, run_id, principal.username)


@router.get(SESSIONS_PATH + "/{run_id}/export", response_model=SessionExport)
async def get_export(run_id: str, request: Request, principal: CurrentPrincipal,
                     name: Annotated[str | None, Query()] = None) -> SessionExport:
    ctx = get_ctx(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id)
    return await run_in_threadpool(export_session, ctx, run_id, name, principal)


__all__ = [
    "SESSIONS_PATH",
    "SESSION_EVENT",
    "SessionClosed",
    "SessionDo",
    "SessionDone",
    "SessionExport",
    "SessionOpen",
    "SessionOpened",
    "close_session",
    "create_session",
    "delete_session",
    "do_in_session",
    "export_session",
    "flow_name",
    "flow_source",
    "get_export",
    "open_body",
    "open_session",
    "router",
    "session_do",
]
