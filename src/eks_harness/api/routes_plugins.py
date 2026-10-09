from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx, require_admin
from eks_harness.api.errors import bad_request, not_found
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import kv
from eks_harness.plugins import PluginError, PluginRecord, find_tree, parse_git_spec
from eks_harness.plugins.discovery import fetch_git
from eks_harness.plugins.manifest import CONTRIBUTION_TYPES, UI_SLOTS

router = APIRouter(tags=["plugins"])
log = logging.getLogger("eks_harness.plugins")

TREE_KEY = "project-tree:"


def remember_tree(ctx: AppContext, tree: Path) -> None:
    project = ctx.plugins.project(tree)
    if project.project_id:
        with ctx.db.transaction() as conn:
            kv.put(conn, TREE_KEY + project.project_id, str(tree))


def tree_for(ctx: AppContext, tree: str | None, project: str | None) -> Path | None:
    if tree:
        path = Path(tree).expanduser()
        if not path.is_dir():
            raise bad_request(f"No directory {tree}.", error="no_tree")
        resolved = find_tree(path) or path.resolve()
        remember_tree(ctx, resolved)
        return resolved
    if project:
        stored = kv.get(ctx.db.conn(), TREE_KEY + project)
        if stored and Path(stored).is_dir():
            return Path(stored)
    return None


def record_out(record: PluginRecord, principal: Principal) -> dict[str, Any]:
    out = record.as_dict()
    if not principal.is_admin:
        out.pop("root", None)
        out.pop("tree", None)
    return out


def ui_modules(ctx: AppContext, tree: Path | None, project: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in ctx.plugins.active(tree):
        for contribution in record.manifest.contributions:
            if contribution.type not in ("ui", "viewer"):
                continue
            module = contribution.get("module")
            params = f"?project={project}" if project and record.kind == "repo" else ""
            items.append({**contribution.as_dict(), "pluginName": record.manifest.name,
                          "url": f"/api/plugins/{record.id}/files/{module}{params}"})
    return items


@router.get("/api/plugins")
def list_plugins(request: Request, tree: str | None = Query(None), project: str | None = Query(None),
                 principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    resolved = tree_for(ctx, tree, project)
    records = ctx.plugins.records(resolved)
    return {"items": [record_out(r, principal) for r in records], "tree": str(resolved) if resolved else None,
            "types": CONTRIBUTION_TYPES, "slots": sorted(UI_SLOTS)}


@router.get("/api/plugins/ui")
def list_ui(request: Request, tree: str | None = Query(None), project: str | None = Query(None),
            _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    return {"items": ui_modules(ctx, tree_for(ctx, tree, project), project)}


@router.post("/api/plugins/refresh")
def refresh(request: Request, _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    ctx.plugins.refresh()
    return {"items": [r.as_dict() for r in ctx.plugins.records()]}


@router.post("/api/plugins/sync")
async def sync(request: Request, body: dict = Body(default_factory=dict), _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    tree = tree_for(ctx, body.get("tree"), body.get("project"))
    specs = list(ctx.config["plugins.git"] or []) + list(ctx.plugins.project(tree).git if tree else [])
    results = []
    for text in specs:
        try:
            spec = parse_git_spec(text)
            path = await run_in_threadpool(fetch_git, ctx.paths.cache_dir, spec, update=bool(body.get("update")))
            results.append({"spec": text, "path": str(path), "ok": True})
        except (ValueError, RuntimeError, OSError) as error:
            results.append({"spec": text, "ok": False, "error": str(error)})
    ctx.plugins.refresh()
    return {"items": results}


@router.post("/api/plugins/install")
async def install(request: Request, body: dict = Body(...), principal: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    source = str(body.get("source") or "").strip()
    if not source:
        raise bad_request("Send the plugin source: a folder path or a git spec.", error="no_source")
    local = Path(source).expanduser()
    if local.exists():
        key, value = "plugins.paths", str(local.resolve())
    else:
        try:
            spec = parse_git_spec(source)
        except ValueError as error:
            raise bad_request(str(error), error="bad_source") from error
        try:
            await run_in_threadpool(fetch_git, ctx.paths.cache_dir, spec, update=True)
        except (RuntimeError, OSError) as error:
            raise bad_request(str(error), error="fetch_failed") from error
        key, value = "plugins.git", source
    current = list(ctx.config[key] or [])
    if value not in current:
        ctx.config.set(key, current + [value])
    ctx.plugins.refresh()
    installed = [r for r in ctx.plugins.records() if r.candidate.origin.endswith(value) or str(r.root).startswith(value)]
    if body.get("trust"):
        for record in installed:
            if record.manifest:
                ctx.plugins.trust(record.id, by=principal.username)
    return {"key": key, "value": value, "items": [r.as_dict() for r in ctx.plugins.records()
                                                  if r.id in {i.id for i in installed}]}


def _record(ctx: AppContext, plugin_id: str, tree: Path | None) -> PluginRecord:
    record = ctx.plugins.get(plugin_id, tree)
    if record is None:
        raise not_found(f"No plugin {plugin_id}.", error="plugin_not_found")
    return record


@router.get("/api/plugins/{plugin_id}")
def get_plugin(plugin_id: str, request: Request, tree: str | None = Query(None), project: str | None = Query(None),
               principal: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    resolved = tree_for(ctx, tree, project)
    record = _record(ctx, plugin_id, resolved)
    out = record_out(record, principal)
    if record.manifest is not None:
        try:
            out["values"] = ctx.plugins.settings(plugin_id, resolved)
        except PluginError as error:
            out["settingsError"] = str(error)
        if not principal.is_admin and record.manifest is not None:
            secrets = {s.key for s in record.manifest.settings if s.type == "secret"}
            out["values"] = {k: ("" if k in secrets else v) for k, v in out.get("values", {}).items()}
    return out


@router.post("/api/plugins/{plugin_id}/trust")
def trust(plugin_id: str, request: Request, body: dict = Body(default_factory=dict),
          principal: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    resolved = tree_for(ctx, body.get("tree"), body.get("project"))
    try:
        entry = ctx.plugins.trust(plugin_id, resolved, by=principal.username)
    except PluginError as error:
        raise not_found(str(error), error="plugin_not_found") from error
    ctx.events.publish("plugin.trusted", detail={"plugin": plugin_id})
    return {"trust": entry, "plugin": record_out(_record(ctx, plugin_id, resolved), principal)}


@router.delete("/api/plugins/{plugin_id}/trust")
def untrust(plugin_id: str, request: Request, tree: str | None = Query(None), project: str | None = Query(None),
            principal: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    resolved = tree_for(ctx, tree, project)
    try:
        removed = ctx.plugins.untrust(plugin_id, resolved)
    except PluginError as error:
        raise not_found(str(error), error="plugin_not_found") from error
    return {"removed": removed, "plugin": record_out(_record(ctx, plugin_id, resolved), principal)}


@router.put("/api/plugins/{plugin_id}/settings")
def put_settings(plugin_id: str, request: Request, body: dict = Body(...), _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    record = _record(ctx, plugin_id, None)
    values = body.get("values")
    if not isinstance(values, dict):
        raise bad_request("Send {\"values\": {...}}.", error="bad_settings")
    for key, value in values.items():
        spec = record.manifest.setting(key) if record.manifest else None
        if spec is None:
            raise bad_request(f"{plugin_id} has no setting {key}.", error="unknown_setting")
        try:
            spec.coerce(value)
        except (ValueError, TypeError) as error:
            raise bad_request(str(error), error="bad_setting") from error
    all_settings = dict(ctx.config["plugins.settings"] or {})
    merged = dict(all_settings.get(plugin_id) or {})
    merged.update(values)
    all_settings[plugin_id] = {k: v for k, v in merged.items() if v is not None}
    ctx.config.set("plugins.settings", all_settings)
    ctx.plugins.refresh()
    return {"values": ctx.plugins.settings(plugin_id)}


@router.get("/api/plugins/{plugin_id}/files/{path:path}")
def plugin_file(plugin_id: str, path: str, request: Request, tree: str | None = Query(None),
                project: str | None = Query(None), _: Principal = Depends(current_principal)) -> FileResponse:
    ctx = get_ctx(request)
    resolved = tree_for(ctx, tree, project)
    record = _record(ctx, plugin_id, resolved)
    if not record.active or record.manifest is None:
        raise not_found(f"Plugin {plugin_id} is not active.", error="plugin_inactive")
    root = record.manifest.root.resolve()
    allowed = {(root / c.get("module")).resolve().parent for c in record.manifest.contributions
               if c.type in ("ui", "viewer") and c.get("module")}
    target = (root / path).resolve()
    if not target.is_file() or not any(target.is_relative_to(base) for base in allowed):
        raise not_found(f"No file {path} in {plugin_id}.", error="not_found")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache",
                                                           "X-Content-Type-Options": "nosniff"})


def mount_plugin_routes(app: FastAPI, ctx: AppContext) -> list[str]:
    mounted: list[str] = []
    for contribution in ctx.plugins.contributions("api"):
        try:
            target = ctx.plugins.load(contribution)
            plugin_router = target.router(ctx.plugins.context(contribution.plugin_id)) if hasattr(target, "router") \
                else target if isinstance(target, APIRouter) else target(ctx.plugins.context(contribution.plugin_id))
            if not isinstance(plugin_router, APIRouter):
                raise PluginError(f"{contribution.key} did not return an APIRouter")
        except Exception as error:
            log.warning("plugin routes %s not mounted: %s", contribution.key, error)
            continue
        prefix = f"/api/plugins/{contribution.plugin_id}/x"
        app.include_router(plugin_router, prefix=prefix, dependencies=[Depends(current_principal)])
        mounted.append(prefix)
    return mounted
