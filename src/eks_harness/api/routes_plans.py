from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool

from eks.discovery import find_tree
from eks_harness.api.deps import check_level, current_principal, get_ctx
from eks_harness.api.errors import bad_request, forbidden
from eks_harness.api.schemas_engine import (
    PlanBuildIn,
    PlanOut,
    PlanValidateIn,
    PlanValidation,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.engine.build import BuildRequest, tree_of
from eks_harness.engine.model import IssueOut
from eks_harness.engine.runs import engine
from eks_harness.ir.plan import Plan
from eks_harness.plugins.project import ProjectConfigError, load_project_config

router = APIRouter(tags=["plans"])

CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

_PLAN_ERRORS = 5


def authorize_tree(ctx: AppContext, principal: Principal, tree: Path | None, level: str = "editor") -> None:
    if principal.is_admin:
        return
    if tree is None:
        raise forbidden("Planning without a tree on the hub runs hub code; only an admin can do it. Pass the tree "
                        "of your project.", error="admin_required")
    try:
        project_id = load_project_config(tree.expanduser()).project_id
    except (ProjectConfigError, OSError, ValueError):
        project_id = None
    if not isinstance(project_id, str) or not project_id:
        raise forbidden(f"The tree {tree} names no [project] id in .harness/project.toml; only an admin can plan "
                        "from it.", error="admin_required")
    check_level(ctx.db, principal, project_id, None, level, f"the project {project_id}")


def _require_admin_for_source(principal: Principal, source: str | None) -> None:
    if source is not None and not principal.is_admin:
        raise forbidden("Inline source runs arbitrary code on the hub; only an admin can plan it. Save it as a file "
                        "of your project's tree and plan that instead.", error="admin_required")


def _path(text: str | None) -> Path | None:
    return Path(text).expanduser() if text else None


def _planning_tree(spec: str | None, source: str | None, tree: str | None, cwd: str | None) -> Path | None:
    given = _path(tree)
    if spec is not None:
        return tree_of(spec, given, _path(cwd))
    if source is not None and given is None and cwd:
        found = find_tree(Path(cwd).expanduser().resolve())
        return found.resolve() if found is not None else None
    return given.resolve() if given is not None else None


def _hub_tree(text: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise bad_request(f"tree {text} must be an absolute path on the hub", error="bad_tree")
    if not path.is_dir():
        raise bad_request(f"tree {text} is not a directory on the hub", error="bad_tree")
    return path.resolve()


def _plan_problem(error: ValidationError) -> str:
    problems = []
    for item in error.errors()[:_PLAN_ERRORS]:
        where = ".".join(str(part) for part in item.get("loc", ())) or "plan"
        problems.append(f"{where}: {item.get('msg', '')}")
    more = error.error_count() - len(problems)
    tail = f" (and {more} more)" if more > 0 else ""
    return f"the plan JSON does not parse as an eks/plan@1 plan: {'; '.join(problems)}{tail}"


def _parse_plan(raw: dict[str, JsonValue]) -> Plan | IssueOut:
    try:
        return Plan.model_validate(raw)
    except ValidationError as error:
        return IssueOut(severity="error", code="plan.json", message=_plan_problem(error))


def _plan_json(plan: Plan) -> dict[str, JsonValue]:
    return plan.model_dump(mode="json", by_alias=True, exclude_none=True)


def _validation(plan: Plan, issues: Sequence[IssueOut]) -> PlanValidation:
    return PlanValidation(ok=not any(issue.severity == "error" for issue in issues), hash=plan.hash,
                          issues=list(issues))


def _authorize_planning(ctx: AppContext, principal: Principal, spec: str | None, source: str | None,
                        tree: str | None, cwd: str | None, level: str) -> None:
    _require_admin_for_source(principal, source)
    if principal.is_admin:
        return
    authorize_tree(ctx, principal, _planning_tree(spec, source, tree, cwd), level)


@router.post("/api/plans", response_model=PlanOut)
async def build_plan(body: PlanBuildIn, request: Request, principal: CurrentPrincipal) -> PlanOut:
    ctx = get_ctx(request)
    await run_in_threadpool(_authorize_planning, ctx, principal, body.spec, body.source, body.tree, body.cwd,
                            "viewer")
    built = await run_in_threadpool(engine(ctx).builder.build, BuildRequest(
        spec=body.spec, source=body.source, entry=body.entry, folder=body.folder, tree=_path(body.tree),
        cwd=_path(body.cwd), params=dict(body.params), platform=body.platform, plugins=body.plugins))
    return PlanOut(plan=_plan_json(built.plan), validation=_validation(built.plan, built.issues),
                   tree=str(built.tree) if built.tree is not None else None)


@router.post("/api/plans/validate", response_model=PlanValidation)
async def validate_plan(body: PlanValidateIn, request: Request, principal: CurrentPrincipal) -> PlanValidation:
    ctx = get_ctx(request)
    parsed = _parse_plan(body.plan)
    if isinstance(parsed, IssueOut):
        raw_hash = body.plan.get("hash")
        return PlanValidation(ok=False, hash=raw_hash if isinstance(raw_hash, str) else "", issues=[parsed])
    tree = _hub_tree(body.tree) if body.tree else None
    if tree is not None:
        await run_in_threadpool(authorize_tree, ctx, principal, tree, "viewer")

    def check() -> list[IssueOut]:
        _entry, issues = engine(ctx).builder.validate(parsed, tree=tree, plugins=body.plugins)
        return issues

    return _validation(parsed, await run_in_threadpool(check))


__all__ = ["CurrentPrincipal", "authorize_tree", "build_plan", "router", "validate_plan"]
