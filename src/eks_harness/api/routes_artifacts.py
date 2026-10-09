from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import access_scope, current_principal, get_ctx, get_scope
from eks_harness.api.errors import bad_request, forbidden, not_found
from eks_harness.api.schemas import (
    ArtifactBulkDeleteRequest,
    ArtifactBulkRetentionRequest,
    ArtifactBulkTagRequest,
    ArtifactList,
    ArtifactOut,
    ArtifactUpdate,
    DeleteSummary,
    SearchResponse,
    SeenBulkRequest,
    SeenResponse,
    TagCatalog,
    TagColorUpdate,
    TagCount,
    TagInfo,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon import events as ev
from eks_harness.daemon.context import AppContext
from eks_harness.db.common import UNSET
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos import sessions as sessions_repo
from eks_harness.db.repos import tags as tags_repo
from eks_harness.db.repos.artifacts import Artifact, ArtifactQuery
from eks_harness.db.repos.grants import AccessScope
from eks_harness.store import access as store_access
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import search as store_search
from eks_harness.store import seen as store_seen
from eks_harness.store import deletion, serving, zipstream
from eks_harness.store.layout import PROJECT_LEVEL_SLUG
from eks_harness.store.multipart import ParsedForm, parse_multipart
from eks_harness.store.retention import default_retention_days
from eks_harness.store.serving import content_disposition

router = APIRouter(tags=["artifacts"])
MAX_PAGE = 500
MAX_ZIP_ITEMS = 5000


def _split(values: Sequence[str] | None) -> list[str]:
    found: list[str] = []
    for value in values or []:
        found.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(found))


def _parse_meta(raw: str | None) -> dict | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as problem:
        raise bad_request(f"meta is not valid JSON: {problem}", error="invalid_meta") from None
    if not isinstance(value, dict):
        raise bad_request("meta must be a JSON object.", error="invalid_meta")
    return value


def _source(form: ParsedForm, principal: Principal) -> str:
    value = (form.get("source") or "").strip().lower()
    if value:
        if value not in artifacts_repo.SOURCES:
            raise bad_request(f"source must be one of {', '.join(artifacts_repo.SOURCES)}.", error="invalid_source")
        return value
    return "ui" if principal.via == "cookie" else "cli"


def _tags(form: ParsedForm) -> list[str]:
    return _split(form.get_all("tags") + form.get_all("tag"))


def _visible(ctx: AppContext, principal: Principal, ids: Sequence[str], required: str) -> list[Artifact]:
    conn = ctx.db.conn()
    wanted = list(dict.fromkeys(ids))
    found = {a.id: a for a in artifacts_repo.get_many(conn, wanted)}
    allowed, missing = [], []
    for artifact_id in wanted:
        artifact = found.get(artifact_id)
        if artifact is None:
            missing.append(artifact_id)
            continue
        store_access.authorize_artifact(conn, principal, artifact, required)
        allowed.append(artifact)
    if missing:
        raise not_found(f"No artifact {', '.join(missing[:10])}.", error="artifact_not_found", missing=missing)
    return allowed


def _out(ctx: AppContext, principal: Principal, artifact: Artifact, with_neighbours: bool = False) -> ArtifactOut:
    return store_artifacts.to_out(ctx.db.conn(), ctx.links, ctx.paths, artifact, user_id=principal.user_id,
                                  with_neighbours=with_neighbours,
                                  default_retention_days=default_retention_days(ctx.config) if with_neighbours
                                  else UNSET)


def _query(ctx: AppContext, principal: Principal, scope: AccessScope, *, project: str | None, session: str | None,
           kinds: list[str], tags: list[str], q: str | None, unseen: bool | None, pinned: bool | None,
           sid: str | None, cursor: str | None, limit: int, sort: str = "created",
           ascending: bool = False) -> ArtifactQuery:
    conn = ctx.db.conn()
    project_id = session_id = None
    project_level_only = False
    if session and not project:
        if session == PROJECT_LEVEL_SLUG:
            raise bad_request("_project needs project (owner/name).", error="session_without_project")
        found = sessions_repo.find(conn, session)
        visible_projects = sessions_repo.project_ids(conn, found.id) if found else []
        if found is None or not any(scope.can(p, found.id, "viewer") for p in visible_projects):
            raise not_found(f"No session {session}.", error="session_not_found")
        session_id = found.id
    if project:
        project_id = store_access.parse_project(project)
        if projects_repo.get(conn, project_id) is None or not scope.can_see_project(project_id):
            raise not_found(f"No project {project_id}.", error="project_not_found")
        if session == PROJECT_LEVEL_SLUG:
            project_level_only = True
            store_access.require_level(conn, principal, project_id, None, "viewer")
        elif session:
            found = sessions_repo.find_in_project(conn, project_id, session)
            if found is None:
                raise not_found(f"No session {session} in {project_id}.", error="session_not_found")
            store_access.require_level(conn, principal, project_id, found.id, "viewer",
                                       missing_error="session_not_found",
                                       missing_message=f"No session {session} in {project_id}.")
            session_id = found.id
    lease_sid = None
    if sid:
        lease = store_access.lease_for_sid(conn, sid)
        lease_sid = lease.sid
    try:
        clean_tags = tuple(artifacts_repo.normalize_tag(t) for t in tags)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_tag") from None
    return ArtifactQuery(
        project_id=project_id, session_id=session_id, project_level_only=project_level_only,
        kinds=tuple(k.lower() for k in kinds), tags=clean_tags, fts_match=store_search.fts_query(q or "") or None,
        unseen_for_user=principal.user_id if unseen is True else None,
        seen_for_user=principal.user_id if unseen is False else None, pinned=pinned, lease_sid=lease_sid,
        scope_sql=None if scope.everything else scope.sql("a.project_id", "a.session_id"),
        cursor=cursor or None, limit=limit, sort=sort, ascending=ascending)


@router.get("/api/artifacts", response_model=ArtifactList)
def list_artifacts(request: Request, principal: Principal = Depends(current_principal),
                   scope: AccessScope = Depends(get_scope), project: str | None = None, session: str | None = None,
                   kind: list[str] = Query(default_factory=list), tag: list[str] = Query(default_factory=list),
                   q: str | None = None, unseen: bool | None = None, pinned: bool | None = None,
                   sid: str | None = None, cursor: str | None = None,
                   sort: Literal["created", "name", "size", "kind"] = "created",
                   dir: Literal["asc", "desc"] | None = None, facets: bool = False,
                   limit: int = Query(50, ge=1, le=MAX_PAGE)) -> ArtifactList:
    ctx = get_ctx(request)
    direction = dir or ("desc" if sort in ("created", "size") else "asc")
    query = _query(ctx, principal, scope, project=project, session=session, kinds=_split(kind), tags=_split(tag),
                   q=q, unseen=unseen, pinned=pinned, sid=sid, cursor=cursor, limit=limit, sort=sort,
                   ascending=direction == "asc")
    conn = ctx.db.conn()
    try:
        items, next_cursor, prev_cursor = artifacts_repo.query_page(conn, query)
    except artifacts_repo.CursorError as problem:
        raise bad_request(str(problem), error="invalid_cursor") from None
    return ArtifactList(items=store_artifacts.to_out_many(conn, ctx.links, ctx.paths, items, principal.user_id),
                        next_cursor=next_cursor, prev_cursor=prev_cursor, total=artifacts_repo.count(conn, query),
                        facets=artifacts_repo.facets(conn, query) if facets else None)


def _ingest(ctx: AppContext, principal: Principal, form: ParsedForm) -> ArtifactOut:
    files = form.files_for("file", "files")
    if len(files) != 1:
        raise bad_request("Send exactly one file in the 'file' field (use POST /api/sites for a site).",
                          error="one_file_per_upload", received=len(files))
    with ctx.db.transaction() as conn:
        target = store_access.resolve_target(conn, project=form.get("project"), session=form.get("session"),
                                             sid=form.get("sid"), principal=principal, required="editor")
    artifact = store_artifacts.ingest_upload(
        ctx.db, ctx.config, files[0], target=target, kind=form.get("kind"), caption=form.get("caption") or "",
        tags=_tags(form), meta=_parse_meta(form.get("meta")), source=_source(form, principal), user=principal,
        filename=form.get("filename"), events=ctx.events)
    return _out(ctx, principal, artifact)


@router.post("/api/artifacts", response_model=ArtifactOut, status_code=201)
async def upload_artifact(request: Request, principal: Principal = Depends(current_principal)) -> ArtifactOut:
    ctx = get_ctx(request)
    form = await parse_multipart(request.headers, request.stream(), ctx.paths.tmp_dir,
                                 store_artifacts.max_upload_bytes(ctx.config))
    try:
        return await run_in_threadpool(_ingest, ctx, principal, form)
    finally:
        form.cleanup()


def _ingest_site(ctx: AppContext, principal: Principal, form: ParsedForm) -> ArtifactOut:
    zips = form.files_for("file", "zip")
    files = form.files_for("files", "files[]")
    if zips and files or len(zips) > 1:
        raise bad_request("Send one zip in 'file', or the site files in 'files' with relative paths.",
                          error="bad_site_upload")
    if not zips and not files:
        raise bad_request("The upload has no files.", error="empty_site")
    paths = form.get_all("paths") or form.get_all("paths[]")
    if paths and len(paths) != len(files):
        raise bad_request("paths must list one relative path per file.", error="bad_site_upload")
    with ctx.db.transaction() as conn:
        target = store_access.resolve_target(conn, project=form.get("project"), session=form.get("session"),
                                             sid=form.get("sid"), principal=principal, required="editor")
    common = dict(target=target, entry=form.get("entry") or "index.html", name=form.get("name"),
                  caption=form.get("caption") or "", tags=_tags(form), meta=_parse_meta(form.get("meta")),
                  source=_source(form, principal), user=principal, events=ctx.events)
    if zips:
        artifact = store_artifacts.ingest_site(ctx.db, ctx.config, zip_path=zips[0].path,
                                               name=form.get("name") or zips[0].filename, **{
                                                   k: v for k, v in common.items() if k != "name"})
    else:
        pairs = [(paths[i] if paths else f.filename, f.path) for i, f in enumerate(files)]
        artifact = store_artifacts.ingest_site(ctx.db, ctx.config, files=pairs, **common)
    return _out(ctx, principal, artifact)


@router.post("/api/sites", response_model=ArtifactOut, status_code=201)
async def upload_site(request: Request, principal: Principal = Depends(current_principal)) -> ArtifactOut:
    ctx = get_ctx(request)
    form = await parse_multipart(request.headers, request.stream(), ctx.paths.tmp_dir,
                                 store_artifacts.max_upload_bytes(ctx.config))
    try:
        return await run_in_threadpool(_ingest_site, ctx, principal, form)
    finally:
        form.cleanup()


@router.post("/api/artifacts/seen", response_model=SeenResponse)
def mark_seen_bulk(body: SeenBulkRequest, request: Request,
                   principal: Principal = Depends(current_principal)) -> SeenResponse:
    ctx = get_ctx(request)
    items = _visible(ctx, principal, body.ids, "viewer")
    updated = store_seen.set_seen(ctx.db, principal.user_id, [a.id for a in items], body.seen)
    return SeenResponse(updated=updated, seen=body.seen)


@router.post("/api/artifacts/tags", response_model=ArtifactList)
def tag_bulk(body: ArtifactBulkTagRequest, request: Request,
             principal: Principal = Depends(current_principal)) -> ArtifactList:
    ctx = get_ctx(request)
    if not body.add and not body.remove:
        raise bad_request("Send tags to add or remove.", error="no_tags")
    items = _visible(ctx, principal, body.ids, "editor")
    store_artifacts.bulk_tag(ctx.db, items, body.add, body.remove, ctx.events, principal.username)
    conn = ctx.db.conn()
    refreshed = artifacts_repo.get_many(conn, [a.id for a in items])
    return ArtifactList(items=store_artifacts.to_out_many(conn, ctx.links, ctx.paths, refreshed, principal.user_id))


@router.post("/api/artifacts/delete", response_model=DeleteSummary)
def delete_bulk(body: ArtifactBulkDeleteRequest, request: Request, principal: Principal = Depends(current_principal),
                dry_run: bool = Query(False, alias="dryRun")) -> DeleteSummary:
    ctx = get_ctx(request)
    items = _visible(ctx, principal, body.ids, "editor")
    ids = [a.id for a in items]
    if dry_run:
        return DeleteSummary(**deletion.plan_artifacts(ctx.db.conn(), ids).summary(False))
    plan = deletion.delete_artifacts(ctx.db, ctx.paths, ids, events=ctx.events, actor=principal.username)
    return DeleteSummary(**plan.summary(True))


def _zip_items(ctx: AppContext, principal: Principal, scope: AccessScope, ids: list[str], project: str | None,
               session: str | None) -> list[Artifact]:
    if ids:
        items = _visible(ctx, principal, ids, "viewer")
    elif project or session:
        query = _query(ctx, principal, scope, project=project, session=session, kinds=[], tags=[], q=None,
                       unseen=None, pinned=None, sid=None, cursor=None, limit=MAX_ZIP_ITEMS)
        items, _ = artifacts_repo.query(ctx.db.conn(), query)
        items.reverse()
    else:
        raise bad_request("Send ids, a project with an optional session, or a session.",
                          error="nothing_selected")
    if len(items) > MAX_ZIP_ITEMS:
        raise bad_request(f"At most {MAX_ZIP_ITEMS} artifacts per zip.", error="too_many_items")
    items = zipstream.existing(ctx.paths, items)
    if not items:
        raise not_found("None of the selected files are in the store.", error="artifact_not_found")
    return items


def _zip_response(ctx: AppContext, items: list[Artifact]) -> StreamingResponse:
    body = serving.closing_stream(zipstream.stream_zip(ctx.paths, items))
    return StreamingResponse(body, media_type="application/zip", headers={
        "Content-Disposition": content_disposition("attachment", zipstream.zip_filename(items)),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/artifacts/zip")
def download_zip(request: Request, principal: Principal = Depends(current_principal),
                 scope: AccessScope = Depends(get_scope), ids: list[str] = Query(default_factory=list),
                 project: str | None = None, session: str | None = None) -> StreamingResponse:
    ctx = get_ctx(request)
    return _zip_response(ctx, _zip_items(ctx, principal, scope, _split(ids), project, session))


@router.post("/api/artifacts/zip")
def download_zip_post(body: ArtifactBulkDeleteRequest, request: Request,
                      principal: Principal = Depends(current_principal),
                      scope: AccessScope = Depends(get_scope)) -> StreamingResponse:
    ctx = get_ctx(request)
    return _zip_response(ctx, _zip_items(ctx, principal, scope, list(body.ids), None, None))


@router.get("/api/search", response_model=SearchResponse)
def search(request: Request, q: str = Query(..., min_length=1, max_length=500),
           principal: Principal = Depends(current_principal), scope: AccessScope = Depends(get_scope),
           project: str | None = None, limit: int = Query(50, ge=1, le=MAX_PAGE)) -> SearchResponse:
    ctx = get_ctx(request)
    project_id = None
    if project:
        project_id = store_access.parse_project(project)
        if not scope.can_see_project(project_id):
            raise not_found(f"No project {project_id}.", error="project_not_found")
    return store_search.search_artifacts(ctx.db, ctx.links, ctx.paths, principal.user_id, scope, q,
                                         project_id=project_id, limit=limit)


@router.get("/api/tags", response_model=list[TagCount])
def list_tags(request: Request, principal: Principal = Depends(current_principal),
              scope: AccessScope = Depends(get_scope), project: str | None = None,
              session: str | None = None) -> list[TagCount]:
    ctx = get_ctx(request)
    query = _query(ctx, principal, scope, project=project, session=session, kinds=[], tags=[], q=None, unseen=None,
                   pinned=None, sid=None, cursor=None, limit=1)
    clauses, params = ["1 = 1"], []
    if query.project_id is not None:
        clauses.append("a.project_id = ?")
        params.append(query.project_id)
    if query.session_id is not None:
        clauses.append("a.session_id = ?")
        params.append(query.session_id)
    if query.project_level_only:
        clauses.append("a.session_id IS NULL")
    if query.scope_sql is not None:
        clauses.append(query.scope_sql[0])
        params.extend(query.scope_sql[1])
    conn = ctx.db.conn()
    rows = conn.execute(
        f"SELECT t.tag, COUNT(*) FROM artifact_tags t JOIN artifacts a ON a.id = t.artifact_id "
        f"WHERE {' AND '.join(clauses)} GROUP BY t.tag ORDER BY t.tag", params).fetchall()
    colors = tags_repo.colors(conn)
    return [_tag_count(r[0], r[1], colors) for r in rows]


def _tag_count(tag: str, count: int, colors: dict[str, str]) -> TagCount:
    builtin = tags_repo.BUILTIN_TAGS.get(tag)
    return TagCount(tag=tag, count=count, color=colors.get(tag) or (builtin.color if builtin else None),
                    builtin=builtin is not None, label=builtin.label if builtin else None)


def _tag_info(tag: str, count: int, colors: dict[str, str]) -> TagInfo:
    builtin = tags_repo.BUILTIN_TAGS.get(tag)
    default = builtin.color if builtin else None
    return TagInfo(tag=tag, label=builtin.label if builtin else tag, color=colors.get(tag) or default,
                   default_color=default, builtin=builtin is not None,
                   description=builtin.description if builtin else "", count=count)


@router.get("/api/tags/catalog", response_model=TagCatalog)
def tag_catalog(request: Request, principal: Principal = Depends(current_principal),
                scope: AccessScope = Depends(get_scope)) -> TagCatalog:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    where, params = "", []
    if not scope.everything:
        clause, params = scope.sql("a.project_id", "a.session_id")
        where = f" WHERE {clause}"
    counts = {r[0]: r[1] for r in conn.execute(
        f"SELECT t.tag, COUNT(*) FROM artifact_tags t JOIN artifacts a ON a.id = t.artifact_id{where} "
        f"GROUP BY t.tag", params)}
    colors = tags_repo.colors(conn)
    names = sorted(set(tags_repo.BUILTIN_TAGS) | set(counts) | set(colors))
    items = [_tag_info(tag, counts.get(tag, 0), colors) for tag in names]
    items.sort(key=lambda info: (not info.builtin, info.tag))
    return TagCatalog(items=items)


@router.put("/api/tags/{tag}/color", response_model=TagInfo)
def set_tag_color(tag: str, body: TagColorUpdate, request: Request,
                  principal: Principal = Depends(current_principal)) -> TagInfo:
    ctx = get_ctx(request)
    if not access_scope(ctx.db, principal).has_level_anywhere("editor"):
        raise forbidden("You need editor access to a project to change tag colors.", error="insufficient_grant")
    try:
        name = artifacts_repo.normalize_tag(tag)
        with ctx.db.transaction() as conn:
            if body.color:
                tags_repo.set_color(conn, name, body.color, principal.username)
            else:
                tags_repo.reset_color(conn, name)
            colors = tags_repo.colors(conn)
    except ValueError as problem:
        raise bad_request(str(problem), error="invalid_tag_color") from None
    info = _tag_info(name, 0, colors)
    ctx.events.publish(ev.TAG_UPDATED, actor=principal.username, detail={"tag": name, "color": info.color})
    return info


@router.post("/api/artifacts/retention", response_model=ArtifactList)
def retention_bulk(body: ArtifactBulkRetentionRequest, request: Request,
                   principal: Principal = Depends(current_principal)) -> ArtifactList:
    ctx = get_ctx(request)
    items = _visible(ctx, principal, body.ids, "editor")
    store_artifacts.bulk_retention(ctx.db, items, body.retention_days, ctx.events, principal.username)
    refreshed = artifacts_repo.get_many(ctx.db.conn(), [a.id for a in items])
    return ArtifactList(items=store_artifacts.to_out_many(ctx.db.conn(), ctx.links, ctx.paths, refreshed,
                                                          principal.user_id))


@router.get("/api/artifacts/{artifact_id}", response_model=ArtifactOut)
def get_artifact(artifact_id: str, request: Request, principal: Principal = Depends(current_principal),
                 mark_seen: bool = Query(False, alias="markSeen")) -> ArtifactOut:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "viewer")
    if mark_seen:
        store_seen.set_seen(ctx.db, principal.user_id, [artifact.id])
    return _out(ctx, principal, artifact, with_neighbours=True)


@router.patch("/api/artifacts/{artifact_id}", response_model=ArtifactOut)
def update_artifact(artifact_id: str, body: ArtifactUpdate, request: Request,
                    principal: Principal = Depends(current_principal)) -> ArtifactOut:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "editor")
    updated = store_artifacts.update(
        ctx.db, artifact, caption=body.caption if body.caption is not None else UNSET,
        pinned=body.pinned if body.pinned is not None else UNSET, tags=body.tags, add_tags=body.add_tags,
        remove_tags=body.remove_tags, meta=body.meta,
        retention_days=body.retention_days if "retention_days" in body.model_fields_set else UNSET,
        events=ctx.events, actor=principal.username)
    return _out(ctx, principal, updated, with_neighbours=True)


@router.delete("/api/artifacts/{artifact_id}", response_model=DeleteSummary)
def delete_artifact(artifact_id: str, request: Request, principal: Principal = Depends(current_principal),
                    dry_run: bool = Query(False, alias="dryRun")) -> DeleteSummary:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "editor")
    if dry_run:
        return DeleteSummary(**deletion.plan_artifacts(ctx.db.conn(), [artifact.id]).summary(False))
    plan = deletion.delete_artifacts(ctx.db, ctx.paths, [artifact.id], events=ctx.events, actor=principal.username)
    return DeleteSummary(**plan.summary(True))


@router.put("/api/artifacts/{artifact_id}/seen", response_model=SeenResponse)
def mark_seen(artifact_id: str, request: Request, principal: Principal = Depends(current_principal)) -> SeenResponse:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "viewer")
    store_seen.set_seen(ctx.db, principal.user_id, [artifact.id])
    return SeenResponse(updated=1, seen=True)


@router.delete("/api/artifacts/{artifact_id}/seen", response_model=SeenResponse)
def mark_unseen(artifact_id: str, request: Request,
                principal: Principal = Depends(current_principal)) -> SeenResponse:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "viewer")
    updated = store_seen.set_seen(ctx.db, principal.user_id, [artifact.id], False)
    return SeenResponse(updated=updated, seen=False)


from eks_harness.api import routes_annotate as _annotate_routes

router.include_router(_annotate_routes.router)
