from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, get_ctx, require_admin
from eks_harness.api.errors import bad_request, not_found
from eks_harness.api.schemas import (
    BackendDefinitionList,
    BackendEnsureRequest,
    BackendHoldRequest,
    BackendList,
    BackendOut,
    BackendStopRequest,
    LogResponse,
)
from eks_harness.api.routes_logs import compile_grep, log_file, read_log
from eks_harness.auth.core import Principal
from eks_harness.backends import choose_backend, personas_for, seeders_for
from eks_harness.plugins import PluginError
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import leases as leases_repo
from eks_harness.pools.base import BackendError

router = APIRouter(tags=["backends"])


PRIVATE_FIELDS = ("captured", "exports", "logs", "logFile", "tree", "definitionPath", "step", "logOffsets",
                  "stopReason")


def backend_out(record: dict, ctx: AppContext | None = None, principal: Principal | None = None) -> dict:
    out = {k: v for k, v in record.items() if k not in ("step", "logOffsets", "adopt", "stopFinal")}
    out["ports"] = {str(k): int(v) for k, v in (record.get("ports") or {}).items()}
    out["captured"] = {str(k): str(v) for k, v in (record.get("captured") or {}).items()}
    out["exports"] = {str(k): str(v) for k, v in (record.get("exports") or {}).items()}
    out["logs"] = {str(k): str(v) for k, v in (record.get("logs") or {}).items()}
    if ctx is not None and principal is not None and not may_see_secrets(ctx, principal, record):
        for key in PRIVATE_FIELDS:
            out.pop(key, None)
        out["captured"], out["exports"], out["logs"] = {}, {}, {}
        if out.get("error"):
            out["error"] = str(out["error"]).splitlines()[0][:300]
        out["redacted"] = True
    return out


def may_see_secrets(ctx: AppContext, principal: Principal, record: dict) -> bool:
    if principal.is_admin:
        return True
    scope = access_scope(ctx.db, principal)
    bound = leases_repo.list_leases(ctx.db.conn(), states=leases_repo.LIVE_STATES, backend_id=record.get("id"))
    return any(lease.project_id and scope.can(lease.project_id, lease.session_id, "editor") for lease in bound)


def public(ctx: AppContext, backend_id: str, principal: Principal) -> dict:
    record = ctx.pools.backends.get(backend_id)
    if record is None:
        raise not_found(f"No backend {backend_id}.", error="backend_not_found")
    return backend_out(ctx.pools.backends.public(record), ctx, principal)


@router.get("/api/backends", response_model=BackendList)
async def list_backends(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    items = await run_in_threadpool(ctx.pools.backends.list)
    return {"items": [backend_out(item, ctx, principal) for item in items]}


@router.get("/api/backends/definitions", response_model=BackendDefinitionList)
def definitions(request: Request, tree: str | None = Query(None),
                _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    return {"items": ctx.pools.backends.definitions(tree)}


def _tree_path(tree: str | None) -> Path | None:
    if not tree:
        return None
    path = Path(tree).expanduser()
    if not path.is_dir():
        raise bad_request(f"No directory {tree}.", error="no_tree")
    return path.resolve()


@router.post("/api/backends/choose")
def choose(request: Request, body: dict = Body(...), _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    tree = _tree_path(body.get("tree"))
    if tree is None:
        raise bad_request("Send the work tree.", error="no_tree")
    return choose_backend(ctx.plugins, tree, body.get("requested"))


@router.get("/api/backends/personas")
def personas(request: Request, tree: str | None = Query(None), target: str = Query("local"),
             _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    try:
        return {"items": personas_for(ctx.plugins, _tree_path(tree), target)}
    except PluginError as error:
        raise bad_request(str(error), error="credentials_failed") from error


@router.get("/api/backends/seeders")
def seeders(request: Request, tree: str | None = Query(None), backend: str | None = Query(None),
            _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    items = []
    for contribution, seeder in seeders_for(ctx.plugins, _tree_path(tree), backend):
        try:
            scenarios = list(seeder.scenarios())
        except Exception as error:
            scenarios, problem = [], str(error)
        else:
            problem = None
        items.append({"id": contribution.id, "plugin": contribution.plugin_id, "scenarios": scenarios,
                      "backends": contribution.get("backends") or [], **({"error": problem} if problem else {})})
    return {"items": items}


@router.post("/api/backends/{backend_id:path}/seed")
async def seed(backend_id: str, request: Request, body: dict = Body(default_factory=dict),
               _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    record = ctx.pools.backends.get(backend_id)
    if record is None:
        raise not_found(f"No backend {backend_id}.", error="backend_not_found")
    if record.get("status") != "running":
        raise bad_request(f"Backend {backend_id} is {record.get('status')}, not running.", error="backend_not_running")
    scenario = body.get("scenario")
    tree = _tree_path(record.get("tree"))
    chosen = [(c, s) for c, s in seeders_for(ctx.plugins, tree, record.get("definition"))
              if not body.get("seeder") or c.id == body["seeder"]]
    context = ctx.pools.backends.context(record) if hasattr(ctx.pools.backends, "context") else {
        "backendId": record["id"], "definition": record.get("definition"), "tree": record.get("tree"),
        "ports": record.get("ports") or {}, "exports": record.get("exports") or {}}
    context = {**context, "exports": record.get("exports") or {}}
    results = []
    for contribution, seeder in chosen:
        try:
            outcome = await run_in_threadpool(seeder.seed, context, scenario or (seeder.scenarios() or ["standard"])[0])
        except Exception as error:
            raise bad_request(f"{contribution.key}: {error}", error="seed_failed") from error
        results.append({"seeder": contribution.key, "result": outcome})
    if not chosen:
        definition = str(record.get("definitionPath") or "")
        if not hasattr(ctx.pools.backends, "run") or not definition:
            raise bad_request(f"No seeder for {record.get('definition')}.", error="no_seeder")
        args = ["--scenario", scenario] if scenario else []
        try:
            out = await run_in_threadpool(ctx.pools.backends.run, record, "seed", *args, timeout=3600)
        except BackendError as error:
            raise bad_request(str(error), error="seed_failed") from error
        results.append({"seeder": "definition", "result": out.strip()})
    ctx.events.publish("backend.seeded", resource=f"backend:{backend_id}", detail={"scenario": scenario})
    return {"backend": backend_id, "items": results}


@router.post("/api/backends/ensure", response_model=BackendOut)
async def ensure(body: BackendEnsureRequest, request: Request, _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    payload = {"definition": body.definition, "instance": body.instance, "id": body.id, "tree": body.tree,
               "hold": body.hold, "holdId": body.hold_id, "restart": body.restart}
    try:
        record = await run_in_threadpool(ctx.pools.backends.ensure, payload)
    except BackendError as error:
        raise bad_request(str(error), error="backend_error") from error
    return backend_out(record)


@router.get("/api/backends/{backend_id:path}/status", response_model=BackendOut)
async def backend_status(backend_id: str, request: Request,
                         principal: Principal = Depends(current_principal)) -> dict:
    return await run_in_threadpool(public, get_ctx(request), backend_id, principal)


@router.post("/api/backends/{backend_id:path}/stop", response_model=BackendOut)
async def stop(backend_id: str, request: Request, body: BackendStopRequest | None = None,
               _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    body = body or BackendStopRequest()
    record = ctx.pools.backends.get(backend_id)
    if record is None:
        raise not_found(f"No backend {backend_id}.", error="backend_not_found")
    await run_in_threadpool(ctx.pools.backends.stop, backend_id, body.final, body.reason or "requested")
    current = ctx.pools.backends.get(backend_id)
    if current is None:
        final = dict(record)
        final.update({"status": "stopped", "bindings": [], "alive": {}})
        return backend_out(final)
    return backend_out(ctx.pools.backends.public(current))


@router.post("/api/backends/{backend_id:path}/hold", response_model=BackendOut)
def hold(backend_id: str, request: Request, body: BackendHoldRequest | None = None,
         _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    body = body or BackendHoldRequest()
    try:
        return backend_out(ctx.pools.backends.hold(backend_id, body.seconds, body.hold_id))
    except BackendError as error:
        raise not_found(str(error), error="backend_not_found") from error


@router.get("/api/backends/{backend_id:path}/logs", response_model=LogResponse)
def backend_logs(backend_id: str, request: Request, process: str | None = None,
                 lines: int = Query(200, ge=1, le=100_000), offset: int | None = None, grep: str | None = None,
                 _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    if ctx.pools.backends.get(backend_id) is None:
        raise not_found(f"No backend {backend_id}.", error="backend_not_found")
    resource = f"backend:{backend_id}" + (f":{process}" if process else "")
    return read_log(log_file(ctx, resource), resource, lines, offset, compile_grep(grep))


@router.get("/api/backends/{backend_id:path}", response_model=BackendOut)
async def backend_detail(backend_id: str, request: Request,
                         principal: Principal = Depends(current_principal)) -> dict:
    return await run_in_threadpool(public, get_ctx(request), backend_id, principal)

