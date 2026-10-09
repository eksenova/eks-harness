from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx, get_scope
from eks_harness.api.errors import bad_request, conflict
from eks_harness.api.schemas import ApiModel
from eks_harness.auth.core import Principal
from eks_harness.db.repos.grants import AccessScope
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import cleanup

router = APIRouter(tags=["cleanup"])


class CleanupFilterIn(ApiModel):
    projects: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    session_pattern: str | None = None
    exclude_sessions: list[str] = Field(default_factory=list)
    project_level: Literal["include", "exclude", "only"] = "include"
    session_idle_days: float | None = Field(None, ge=0)
    older_than_days: float | None = Field(None, ge=0)
    created_before: float | None = None
    created_after: float | None = None
    kinds: list[str] = Field(default_factory=list)
    tags_any: list[str] = Field(default_factory=list)
    tags_all: list[str] = Field(default_factory=list)
    tags_none: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    min_size: int | None = Field(None, ge=0)
    max_size: int | None = Field(None, ge=0)
    q: str | None = None
    seen: Literal["unseen", "seen", "never"] | None = None
    include_pinned: bool = False
    include_shared: bool = False
    include_live: bool = False

    def spec(self) -> cleanup.CleanupFilter:
        return cleanup.CleanupFilter(
            projects=tuple(self.projects), sessions=tuple(self.sessions), session_pattern=self.session_pattern,
            exclude_sessions=tuple(self.exclude_sessions), project_level=self.project_level,
            session_idle_days=self.session_idle_days, older_than_days=self.older_than_days,
            created_before=self.created_before, created_after=self.created_after, kinds=tuple(self.kinds),
            tags_any=tuple(self.tags_any), tags_all=tuple(self.tags_all), tags_none=tuple(self.tags_none),
            sources=tuple(self.sources), min_size=self.min_size, max_size=self.max_size, q=self.q, seen=self.seen,
            include_pinned=self.include_pinned, include_shared=self.include_shared, include_live=self.include_live)


class CleanupPreviewIn(ApiModel):
    filter: CleanupFilterIn
    sample: int = Field(50, ge=0, le=cleanup.SAMPLE_MAX)
    order: Literal["oldest", "newest", "largest"] = "oldest"


class CleanupApplyIn(ApiModel):
    filter: CleanupFilterIn
    expect_count: int = Field(ge=0)
    remove_empty_sessions: bool = False


def _preview(request: Request, principal: Principal, scope: AccessScope, body: CleanupPreviewIn) -> dict:
    ctx = get_ctx(request)
    spec = body.filter.spec()
    try:
        data = cleanup.preview(ctx.db, scope, principal.user_id, spec, sample=body.sample, order=body.order)
    except cleanup.CleanupError as problem:
        raise bad_request(str(problem), error="bad_filter") from None
    sample = store_artifacts.to_out_many(ctx.db.conn(), ctx.links, ctx.paths, data["sample"], principal.user_id)
    data["sample"] = [item.model_dump(by_alias=True, mode="json") for item in sample]
    data["empty"] = spec.is_empty()
    return data


def _apply(request: Request, principal: Principal, scope: AccessScope, body: CleanupApplyIn) -> dict:
    ctx = get_ctx(request)
    spec = body.filter.spec()
    try:
        current = cleanup.preview(ctx.db, scope, principal.user_id, spec, sample=0)
        if current["count"] != body.expect_count:
            raise conflict(f"The filter now matches {current['count']} artifacts, not {body.expect_count}. "
                           f"Preview again before deleting.", error="cleanup_changed", count=current["count"],
                           bytes=current["bytes"])
        result = cleanup.apply(ctx.db, ctx.paths, scope, principal.user_id, spec,
                               remove_empty_sessions=body.remove_empty_sessions, events=ctx.events,
                               actor=principal.username)
    except cleanup.CleanupError as problem:
        raise bad_request(str(problem), error="bad_filter") from None
    return result.as_dict()


@router.post("/api/cleanup/preview")
async def cleanup_preview(body: CleanupPreviewIn, request: Request, principal: Principal = Depends(current_principal),
                          scope: AccessScope = Depends(get_scope)) -> dict:
    return await run_in_threadpool(_preview, request, principal, scope, body)


@router.post("/api/cleanup/apply")
async def cleanup_apply(body: CleanupApplyIn, request: Request, principal: Principal = Depends(current_principal),
                        scope: AccessScope = Depends(get_scope)) -> dict:
    return await run_in_threadpool(_apply, request, principal, scope, body)
