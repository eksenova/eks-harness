from __future__ import annotations

import difflib
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import not_found
from eks_harness.api.routes_plans import _hub_tree, authorize_tree
from eks_harness.api.schemas_engine import OpExecutorOut, OpList, OpOut
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.engine.errors import BuildError
from eks_harness.engine.model import DoRequest, DoResult
from eks_harness.engine.runs import RunManager, engine
from eks_harness.ir.registry import OpSpec, Registry, unknown_name
from eks_harness.ir.schema import op_schema, op_summary

router = APIRouter(tags=["ops"])

CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

_SUGGESTIONS = 3


def _registry(ctx: AppContext, principal: Principal, tree: str | None, plugins: bool, level: str) -> Registry:
    path = _hub_tree(tree) if tree else None
    if path is not None:
        authorize_tree(ctx, principal, path, level)
    try:
        return engine(ctx).registries.for_tree(path, plugins=plugins).registry
    except (Exception, SystemExit) as error:
        where = f"the tree {path}" if path is not None else "the hub plugins"
        raise BuildError(f"{type(error).__name__}: {error} (raised while loading {where})") from error


def _executor(manager: RunManager, spec: OpSpec) -> OpExecutorOut:
    try:
        choice = manager.executors.choose(spec)
    except ValueError:
        choice = None
    if choice is None:
        return OpExecutorOut(location=spec.location, available=False)
    return OpExecutorOut(name=choice.executor.name, version=choice.executor.version, location=spec.location,
                         available=True, native=choice.native)


def _op(registry: Registry, name: str) -> OpSpec:
    if registry.has_op(name):
        return registry.op(name)
    names = [spec.name for spec in registry.ops()]
    close = difflib.get_close_matches(name, names, n=_SUGGESTIONS, cutoff=0.6)
    raise not_found(str(unknown_name("op", name, names)), error="op_not_found", op=name, suggestions=close)


@router.get("/api/ops", response_model=OpList)
def list_ops(request: Request, principal: CurrentPrincipal,
             family: Annotated[str | None, Query()] = None,
             tree: Annotated[str | None, Query()] = None,
             plugins: Annotated[bool, Query()] = True) -> OpList:
    ctx = get_ctx(request)
    manager = engine(ctx)
    registry = _registry(ctx, principal, tree, plugins, "viewer")
    specs = registry.ops(family) if family else registry.ops()
    return OpList(items=[OpOut(summary=op_summary(spec), executor=_executor(manager, spec)) for spec in specs])


@router.get("/api/ops/{name}", response_model=OpOut)
def get_op(name: str, request: Request, principal: CurrentPrincipal,
           tree: Annotated[str | None, Query()] = None,
           plugins: Annotated[bool, Query()] = True) -> OpOut:
    ctx = get_ctx(request)
    registry = _registry(ctx, principal, tree, plugins, "viewer")
    spec = _op(registry, name)
    return OpOut(summary=op_summary(spec), executor=_executor(engine(ctx), spec),
                 schema=op_schema(spec, registry=registry))


@router.post("/api/ops/{name}/do", response_model=DoResult)
async def do_op(name: str, body: DoRequest, request: Request, principal: CurrentPrincipal) -> DoResult:
    ctx = get_ctx(request)
    registry = await run_in_threadpool(_registry, ctx, principal, body.tree, True, "editor")
    _op(registry, name)
    return await run_in_threadpool(engine(ctx).do, name, body, principal)


__all__ = ["CurrentPrincipal", "do_op", "get_op", "list_ops", "router"]
