from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request

from eks_harness.api.deps import ProjectAccess, current_principal, get_ctx, get_scope, require_admin, require_project
from eks_harness.api.errors import bad_request, conflict
from eks_harness import project_settings
from eks_harness.api.schemas import (DeleteSummary, ProjectCreate, ProjectList, ProjectOut, ProjectSettingOut,
                                     ProjectSettingsOut, ProjectUpdate)
from eks_harness.auth.core import Principal
from eks_harness.daemon import events as ev
from eks_harness.db.common import UNSET
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos.grants import AccessScope
from eks_harness.ids import parse_project_id
from eks_harness.store import deletion, views
from eks_harness.store.retention import default_retention_days

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _out(request: Request, access: ProjectAccess, scope: AccessScope) -> ProjectOut:
    ctx = get_ctx(request)
    return views.projects_for(ctx.db.conn(), ctx.links, scope, access.principal.user_id, [access.project],
                              default_retention_days(ctx.config))[0]


@router.get("", response_model=ProjectList)
def list_projects(request: Request, principal: Principal = Depends(current_principal),
                  scope: AccessScope = Depends(get_scope)) -> ProjectList:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    items = projects_repo.list_projects(conn, None if scope.everything else scope.project_sql("id"))
    return ProjectList(items=views.projects_for(conn, ctx.links, scope, principal.user_id, items,
                                                 default_retention_days(ctx.config)))


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, request: Request, principal: Principal = Depends(require_admin),
                   scope: AccessScope = Depends(get_scope)) -> ProjectOut:
    ctx = get_ctx(request)
    try:
        project_id = body.project_id().strip().lower()
        owner, name = parse_project_id(project_id)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_project") from None
    project_id = f"{owner}/{name}"
    with ctx.db.transaction() as conn:
        if projects_repo.get(conn, project_id) is not None:
            raise conflict(f"The project {project_id} already exists.", error="project_exists", project=project_id)
        project = projects_repo.create(conn, project_id, title=body.title.strip(), description=body.description,
                                       implicit=False, retention_days=body.retention_days)
    ctx.events.publish(ev.PROJECT_UPDATED, project_id=project.id, actor=principal.username,
                       detail={"project": project.id, "created": True})
    return views.projects_for(ctx.db.conn(), ctx.links, scope, principal.user_id, [project],
                              default_retention_days(ctx.config))[0]


@router.post("/merge")
def merge_projects(request: Request, body: dict = Body(...), principal: Principal = Depends(require_admin)) -> dict:
    from eks_harness.store import merge

    ctx = get_ctx(request)
    sources = [str(s) for s in body.get("sources") or []]
    into = str(body.get("into") or "").strip().lower()
    tags = {str(k).lower(): str(v) for k, v in (body.get("tags") or {}).items()}
    try:
        if body.get("dryRun"):
            return merge.plan(ctx.db.conn(), sources, into, tags).as_dict()
        result = merge.merge(ctx.db, sources, into, tags=tags, title=str(body.get("title") or ""),
                             description=str(body.get("description") or ""), events=ctx.events,
                             actor=principal.username)
    except merge.MergeError as error:
        raise bad_request(str(error), error="bad_merge") from None
    return result.as_dict()


@router.get("/{owner}/{name}", response_model=ProjectOut)
def get_project(request: Request, access: ProjectAccess = Depends(require_project("viewer")),
                scope: AccessScope = Depends(get_scope)) -> ProjectOut:
    return _out(request, access, scope)


@router.patch("/{owner}/{name}", response_model=ProjectOut)
def update_project(body: ProjectUpdate, request: Request, access: ProjectAccess = Depends(require_project("editor")),
                   scope: AccessScope = Depends(get_scope)) -> ProjectOut:
    ctx = get_ctx(request)
    sent = body.model_fields_set
    fields = {
        "title": body.title.strip() if "title" in sent and body.title is not None else UNSET,
        "description": body.description if "description" in sent and body.description is not None else UNSET,
        "retention_days": body.retention_days if "retention_days" in sent else UNSET,
        "settings": UNSET,
    }
    with ctx.db.transaction() as conn:
        if "settings" in sent and body.settings is not None:
            current = projects_repo.get(conn, access.project.id)
            try:
                fields["settings"] = project_settings.merge(current.settings if current else {}, body.settings)
            except project_settings.ProjectSettingError as problem:
                raise bad_request(str(problem), error="invalid_project_setting") from None
        project = projects_repo.update(conn, access.project.id, **fields)
    changed = sorted(k for k, v in fields.items() if v is not UNSET)
    if changed:
        ctx.events.publish(ev.PROJECT_UPDATED, project_id=project.id, actor=access.principal.username,
                           detail={"project": project.id, "changed": changed})
    return views.projects_for(ctx.db.conn(), ctx.links, scope, access.principal.user_id, [project],
                              default_retention_days(ctx.config))[0]


@router.get("/{owner}/{name}/settings", response_model=ProjectSettingsOut)
def get_project_settings(access: ProjectAccess = Depends(require_project("viewer"))) -> ProjectSettingsOut:
    items = project_settings.describe(access.project.settings)
    return ProjectSettingsOut(items=[ProjectSettingOut(**item) for item in items])


@router.delete("/{owner}/{name}", response_model=DeleteSummary)
def delete_project(request: Request, access: ProjectAccess = Depends(require_project("editor")),
                   dry_run: bool = Query(False, alias="dryRun"), force: bool = Query(False)) -> DeleteSummary:
    ctx = get_ctx(request)
    if dry_run:
        plan = deletion.plan_project(ctx.db.conn(), access.project)
        return DeleteSummary(**plan.summary(False))
    plan = deletion.delete_project(ctx.db, ctx.paths, access.project, force=force, events=ctx.events,
                                   actor=access.principal.username)
    return DeleteSummary(**plan.summary(True))
