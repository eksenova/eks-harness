from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, get_ctx
from eks_harness.api.errors import bad_request, forbidden, not_found
from eks_harness.api.routes_plans import (
    _authorize_planning,
    _parse_plan,
    authorize_tree,
)
from eks_harness.api.schemas_engine import RunCancel, RunCreate, RunCreated
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.engine.errors import AdmissionError
from eks_harness.engine.model import (
    ControlRequest,
    ControlResponse,
    IssueOut,
    JournalPage,
    RunComparison,
    RunPage,
    RunQuery,
    RunReport,
    RunRequest,
    RunStatus,
    RunSummary,
)
from eks_harness.engine.runs import RunManager, engine
from eks_harness.ir.plan import Plan

router = APIRouter(tags=["runs"])

WAIT_LIMIT = 3600.0
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

_WARNING = "run.warning"
_WARNING_PAGE = 1000


def authorize_run(ctx: AppContext, principal: Principal, run_id: str, level: str = "viewer") -> RunSummary:
    manager = engine(ctx)
    row = manager.journal.run(run_id)
    missing = not_found(f"No run {run_id}.", error="run_not_found", run=run_id)
    if row is None:
        raise missing
    if not principal.is_admin:
        scope = access_scope(ctx.db, principal)
        project = row.info.project or None
        session_id = row.info.session_id
        if not scope.can(project, session_id, level):
            if level != "viewer" and scope.can(project, session_id, "viewer"):
                raise forbidden(f"You need {level} access to the run {run_id}.", error="insufficient_grant",
                                required=level)
            raise missing
    return manager.status(run_id)


def _warnings(manager: RunManager, run_id: str) -> list[IssueOut]:
    return [IssueOut.model_validate(event.detail)
            for event in manager.journal.events(run_id, limit=_WARNING_PAGE) if event.type == _WARNING]


def _request_of(body: RunCreate, plan: Plan | None) -> RunRequest:
    return RunRequest(plan=plan, spec=body.spec, source=body.source, entry=body.entry, folder=body.folder,
                      tree=body.tree, cwd=body.cwd, params=dict(body.params), platform=body.platform,
                      project=body.project, session=body.session, instance=body.instance, options=body.options,
                      backend=body.backend, backends=dict(body.backends), clock=body.clock, fakes=body.fakes,
                      repeat_of=body.repeat_of)


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not text.isdigit():
        raise bad_request(f"Last-Event-ID {raw!r} is not a journal event id", error="bad_event_id")
    return int(text)


@router.post("/api/runs", response_model=RunCreated, status_code=201)
async def create_run(body: RunCreate, request: Request, principal: CurrentPrincipal) -> RunCreated:
    ctx = get_ctx(request)
    manager = engine(ctx)
    plan: Plan | None = None
    if body.plan is not None:
        parsed = _parse_plan(body.plan)
        if isinstance(parsed, IssueOut):
            raise AdmissionError(parsed.message, [parsed])
        plan = parsed
    if body.spec is not None or body.source is not None or body.tree is not None:
        await run_in_threadpool(_authorize_planning, ctx, principal, body.spec, body.source, body.tree, body.cwd,
                                "editor")
    started = await run_in_threadpool(manager.start, _request_of(body, plan), principal)
    if body.wait > 0:
        started = await run_in_threadpool(manager.wait, started.id, body.wait)
    issues = await run_in_threadpool(_warnings, manager, started.id)
    return RunCreated(run=started, issues=issues)


@router.get("/api/runs", response_model=RunPage)
def list_runs(request: Request, principal: CurrentPrincipal,
              project: Annotated[str | None, Query()] = None,
              session: Annotated[str | None, Query()] = None,
              status: Annotated[list[RunStatus] | None, Query()] = None,
              plan: Annotated[str | None, Query()] = None,
              name: Annotated[str | None, Query()] = None,
              limit: Annotated[int, Query(ge=1, le=500)] = 50,
              cursor: Annotated[str | None, Query()] = None) -> RunPage:
    query = RunQuery(project=project, session=session, status=list(status or []), plan_hash=plan, name=name,
                     limit=limit, cursor=cursor)
    try:
        return engine(get_ctx(request)).list(query, principal)
    except ValueError as error:
        raise bad_request(str(error), error="bad_cursor") from error


@router.get("/api/runs/{run_id}", response_model=RunSummary)
async def get_run(run_id: str, request: Request, principal: CurrentPrincipal,
                  wait: Annotated[float, Query(ge=0, le=WAIT_LIMIT)] = 0.0) -> RunSummary:
    ctx = get_ctx(request)
    current = await run_in_threadpool(authorize_run, ctx, principal, run_id)
    if wait <= 0:
        return current
    return await run_in_threadpool(engine(ctx).wait, run_id, wait)


@router.get("/api/runs/{run_id}/report", response_model=RunReport)
def get_report(run_id: str, request: Request, principal: CurrentPrincipal) -> RunReport:
    ctx = get_ctx(request)
    authorize_run(ctx, principal, run_id)
    return engine(ctx).report(run_id)


@router.get("/api/runs/{run_id}/plan")
def get_plan(run_id: str, request: Request, principal: CurrentPrincipal) -> dict[str, object]:
    ctx = get_ctx(request)
    authorize_run(ctx, principal, run_id)
    row = engine(ctx).journal.run(run_id)
    if row is None:
        raise not_found(f"No run {run_id}.", error="run_not_found", run=run_id)
    return row.plan.model_dump(mode="json", by_alias=True, exclude_none=True)


@router.get("/api/runs/{run_id}/journal", response_model=JournalPage)
def get_journal(run_id: str, request: Request, principal: CurrentPrincipal,
                events: Annotated[bool, Query()] = False,
                after: Annotated[int | None, Query()] = None,
                limit: Annotated[int, Query(ge=1, le=5000)] = 500) -> JournalPage:
    ctx = get_ctx(request)
    authorize_run(ctx, principal, run_id)
    return engine(ctx).journal_page(run_id, events=events, after=after, limit=limit)


@router.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request, principal: CurrentPrincipal) -> StreamingResponse:
    ctx = get_ctx(request)
    last_event_id = _last_event_id(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id)
    manager = engine(ctx)
    frames = manager.events.stream(manager.journal, run_id, last_event_id=last_event_id,
                                   is_disconnected=request.is_disconnected)
    return StreamingResponse(frames, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/runs/{run_id}/control", response_model=ControlResponse)
async def control_run(run_id: str, body: ControlRequest, request: Request,
                      principal: CurrentPrincipal) -> ControlResponse:
    ctx = get_ctx(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id, "editor")
    return await run_in_threadpool(engine(ctx).control, run_id, body, principal.username)


@router.post("/api/runs/{run_id}/cancel", response_model=RunSummary)
async def cancel_run(run_id: str, request: Request, principal: CurrentPrincipal,
                     body: RunCancel | None = None) -> RunSummary:
    ctx = get_ctx(request)
    await run_in_threadpool(authorize_run, ctx, principal, run_id, "editor")
    reason = body.reason if body is not None and body.reason else f"cancelled by {principal.username}"
    return await run_in_threadpool(engine(ctx).cancel, run_id, reason, principal.username)


@router.post("/api/runs/{run_id}/replay", response_model=RunCreated, status_code=201)
async def replay_run(run_id: str, request: Request, principal: CurrentPrincipal) -> RunCreated:
    ctx = get_ctx(request)
    source = await run_in_threadpool(authorize_run, ctx, principal, run_id, "editor")
    if source.tree:
        await run_in_threadpool(authorize_tree, ctx, principal, Path(source.tree), "editor")
    manager = engine(ctx)
    started = await run_in_threadpool(manager.replay, run_id, principal)
    issues = await run_in_threadpool(_warnings, manager, started.id)
    return RunCreated(run=started, issues=issues)


@router.get("/api/runs/{run_id}/compare/{other_id}", response_model=RunComparison)
def compare_runs(run_id: str, other_id: str, request: Request, principal: CurrentPrincipal) -> RunComparison:
    ctx = get_ctx(request)
    authorize_run(ctx, principal, run_id)
    authorize_run(ctx, principal, other_id)
    return engine(ctx).compare(run_id, other_id)


__all__ = [
    "WAIT_LIMIT",
    "CurrentPrincipal",
    "authorize_run",
    "cancel_run",
    "compare_runs",
    "control_run",
    "create_run",
    "get_journal",
    "get_plan",
    "get_report",
    "get_run",
    "list_runs",
    "replay_run",
    "router",
    "stream_events",
]
