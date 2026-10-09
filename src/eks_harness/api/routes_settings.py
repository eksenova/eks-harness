from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from eks_harness.api.deps import get_ctx, require_admin
from eks_harness.api.errors import ApiError, conflict
from eks_harness.api.schemas import (
    SettingChange,
    SettingOut,
    SettingsAuditOut,
    SettingsPatch,
    SettingsPatchResponse,
    SettingsResponse,
    ts_to_datetime,
)
from eks_harness.auth import local as auth_local
from eks_harness.auth.core import Principal
from eks_harness.auth.exposure import exposure_problem
from eks_harness.config import DEFAULTS, RESTART_REQUIRED, SETTINGS, ConfigError, coerce, describe, env_name
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.events import SETTINGS_CHANGED
from eks_harness.db.repos import settings_audit

router = APIRouter(tags=["settings"])


def settings_response(ctx: AppContext) -> SettingsResponse:
    pending = ctx.restart_pending_keys()
    rows = []
    for row in describe(ctx.config):
        rows.append(SettingOut(**row, pending_restart=row["key"] in pending))
    return SettingsResponse(settings=rows, config_file=str(ctx.config.file), restart_pending=bool(pending),
                            restart_pending_keys=pending, errors=ctx.config.errors())


@router.get("/api/settings", response_model=SettingsResponse)
def get_settings(request: Request, _: Principal = Depends(require_admin)) -> SettingsResponse:
    return settings_response(get_ctx(request))


@router.get("/api/settings/audit", response_model=list[SettingsAuditOut])
def get_settings_audit(request: Request, key: str | None = Query(None), limit: int = Query(200, ge=1, le=1000),
                       _: Principal = Depends(require_admin)) -> list[SettingsAuditOut]:
    entries = settings_audit.list_entries(get_ctx(request).db.conn(), key=key, limit=limit)
    return [SettingsAuditOut(id=e.id, ts=ts_to_datetime(e.ts), user=e.user, key=e.key, old=e.old, new=e.new)
            for e in entries]


def _validate(ctx: AppContext, body: SettingsPatch) -> tuple[dict[str, Any], list[str]]:
    problems: list[dict[str, str]] = []
    values: dict[str, Any] = {}
    unset: list[str] = []
    overlap = set(body.values) & set(body.unset)
    for key in sorted(overlap):
        problems.append({"field": key, "message": "cannot be set and unset in the same request", "type": "conflict"})
    for key, raw in body.values.items():
        if key in overlap:
            continue
        if key not in SETTINGS:
            problems.append({"field": key, "message": "unknown setting", "type": "unknown_key"})
            continue
        try:
            values[key] = coerce(key, raw)
        except ConfigError as error:
            problems.append({"field": key, "message": str(error), "type": "invalid_value"})
            continue
        if ctx.config.source(key) == "env":
            problems.append({"field": key, "message": f"set by the environment variable {env_name(key)}; change it "
                                                      f"there", "type": "env_override"})
    for key in body.unset:
        if key in overlap:
            continue
        if key not in SETTINGS:
            problems.append({"field": key, "message": "unknown setting", "type": "unknown_key"})
            continue
        if ctx.config.source(key) == "env":
            problems.append({"field": key, "message": f"set by the environment variable {env_name(key)}; change it "
                                                      f"there", "type": "env_override"})
            continue
        unset.append(key)
    if problems:
        summary = "; ".join(f"{p['field']}: {p['message']}" for p in problems[:5])
        raise ApiError(422, "validation_failed", summary, problems=problems)
    return values, unset


def _prospective(ctx: AppContext, values: dict[str, Any], unset: list[str], key: str) -> Any:
    if key in values:
        return values[key]
    if key in unset:
        return DEFAULTS[key]
    return ctx.config[key]


@router.patch("/api/settings", response_model=SettingsPatchResponse)
def patch_settings(body: SettingsPatch, request: Request, force: bool = Query(False),
                   principal: Principal = Depends(require_admin)) -> SettingsPatchResponse:
    ctx = get_ctx(request)
    values, unset = _validate(ctx, body)
    host = _prospective(ctx, values, unset, "server.host")
    auth_enabled = bool(_prospective(ctx, values, unset, "auth.enabled"))
    start = _prospective(ctx, values, unset, "backend.portRangeStart")
    end = _prospective(ctx, values, unset, "backend.portRangeEnd")
    if start > end:
        raise ApiError(422, "validation_failed", "backend.portRangeStart must not be above backend.portRangeEnd.",
                       problems=[{"field": "backend.portRangeStart", "message": "above backend.portRangeEnd",
                                  "type": "invalid_value"}])
    problem = exposure_problem(str(host), auth_enabled)
    if problem and not force:
        raise conflict(problem.replace("pass --force", "repeat with force=true"), error="exposure_refused")
    if auth_enabled and not ctx.config["auth.enabled"] and not force and not auth_local.usable_admin_exists(ctx.db):
        raise conflict("Enabling auth now would lock everyone out: no enabled admin has a password or an API key. "
                       "Create an admin first (POST /api/users, then a key), or repeat with force=true.",
                       error="no_admin")
    before = ctx.config.as_dict()
    with ctx.lock:
        if values:
            ctx.config.set_many(values)
        for key in unset:
            ctx.config.unset(key)
    after = ctx.config.as_dict()
    touched = list(values) + unset
    changed_keys = [key for key in touched if before.get(key) != after.get(key)]
    if changed_keys:
        with ctx.db.transaction() as conn:
            for key in changed_keys:
                settings_audit.insert(conn, principal.username, key, before.get(key), after.get(key))
        ctx.events.publish(SETTINGS_CHANGED, actor=principal.username,
                           detail={"keys": changed_keys,
                                   "restartRequired": [k for k in changed_keys if k in RESTART_REQUIRED]})
    pending = set(ctx.restart_pending_keys())
    changes = [SettingChange(key=key, old=before.get(key), new=after.get(key), restart_required=key in RESTART_REQUIRED)
               for key in changed_keys]
    return SettingsPatchResponse(changed=changes, restart_required=bool(pending & set(changed_keys)),
                                 settings=settings_response(ctx))
