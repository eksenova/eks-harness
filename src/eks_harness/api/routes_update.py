from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from eks_harness import selfupdate
from eks_harness.api.deps import current_principal, get_ctx, require_admin
from eks_harness.api.errors import ApiError
from eks_harness.auth.core import Principal
from eks_harness.daemon.updater import updater

router = APIRouter(tags=["system"])


@router.get("/api/update")
def update_status(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return updater(get_ctx(request)).snapshot()


@router.post("/api/update/check")
async def update_check(request: Request, _: Principal = Depends(require_admin)) -> dict[str, Any]:
    service = updater(get_ctx(request))
    try:
        await run_in_threadpool(service.check)
    except selfupdate.UpdateError as problem:
        raise ApiError(502, "update_check_failed", str(problem)) from None
    return service.snapshot()


@router.post("/api/update/apply")
def update_apply(request: Request, body: dict = Body(default_factory=dict),
                 _: Principal = Depends(require_admin)) -> dict[str, Any]:
    service = updater(get_ctx(request))
    service.request_apply(force=bool(body.get("force")))
    service.start()
    return service.snapshot()
