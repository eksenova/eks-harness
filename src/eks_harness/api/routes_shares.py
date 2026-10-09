from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import not_found
from eks_harness.api.links import Links
from eks_harness.api.schemas import SharedArtifact, SharedArtifactOut, ShareCreate, ShareList, ShareOut, ts_to_datetime
from eks_harness.auth.core import Principal
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import shares as shares_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.shares import Share
from eks_harness.ids import is_share_token
from eks_harness.paths import Paths
from eks_harness.store import access as store_access
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import shares as store_shares

router = APIRouter(tags=["shares"])


@router.get("/api/artifacts/{artifact_id}/shares", response_model=ShareList)
def list_shares(artifact_id: str, request: Request, principal: Principal = Depends(current_principal)) -> ShareList:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "editor")
    items = shares_repo.list_for_artifact(ctx.db.conn(), artifact.id)
    return ShareList(items=[store_shares.share_out(ctx.links, s, artifact) for s in items])


@router.post("/api/artifacts/{artifact_id}/shares", response_model=ShareOut, status_code=201)
def create_share(artifact_id: str, body: ShareCreate, request: Request,
                 principal: Principal = Depends(current_principal)) -> ShareOut:
    ctx = get_ctx(request)
    artifact = store_access.visible_artifact(ctx.db, principal, artifact_id.upper(), "editor")
    expires_at = store_shares.resolve_expiry(body.expires, body.expires_at)
    share = store_shares.create(ctx.db, artifact, created_by=principal.username, expires_at=expires_at,
                                events=ctx.events)
    return store_shares.share_out(ctx.links, share, artifact)


@router.delete("/api/shares/{token}", response_model=ShareOut)
def revoke_share(token: str, request: Request, principal: Principal = Depends(current_principal)) -> ShareOut:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    share = shares_repo.get(conn, token) if is_share_token(token) else None
    if share is None:
        raise not_found("No such share link.", error="share_not_found")
    artifact = artifacts_repo.get(conn, share.artifact_id)
    if artifact is not None:
        store_access.authorize_artifact(conn, principal, artifact, "editor")
    elif not principal.is_admin:
        raise not_found("No such share link.", error="share_not_found")
    updated = store_shares.revoke(ctx.db, share, artifact, events=ctx.events, actor=principal.username)
    return store_shares.share_out(ctx.links, updated, artifact)


SHARED_SITE_KEYS = ("entry", "files", "fileCount", "truncated")


def shared_artifact_out(conn, links: Links, paths: Paths, share: Share, artifact: Artifact) -> SharedArtifactOut:
    entry = store_artifacts.site_entry(artifact)
    site = artifact.meta.get("site") if isinstance(artifact.meta, dict) else None
    meta = {"site": {k: site[k] for k in SHARED_SITE_KEYS if k in site}} if isinstance(site, dict) else {}
    return SharedArtifactOut(
        token=share.token,
        artifact=SharedArtifact(
            kind=artifact.kind, mime=artifact.mime, filename=artifact.filename, size=artifact.size,
            width=artifact.width, height=artifact.height, duration_ms=artifact.duration_ms,
            caption=artifact.caption or "", created_at=ts_to_datetime(artifact.created_at),
            url=links.share(share.token), raw_url=links.share_raw(share.token),
            direct_url=store_shares.direct_url(links, share, artifact),
            download_url=links.share_raw(share.token) + "?download=1",
            site_url=links.absolute(f"/s/{share.token}/{entry}") if entry else None, meta=meta),
        expires_at=ts_to_datetime(share.expires_at))


@router.get("/api/shared/{token}", response_model=SharedArtifactOut)
def shared_info(token: str, request: Request) -> SharedArtifactOut:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    share, artifact = store_shares.resolve(conn, token)
    return shared_artifact_out(conn, ctx.links, ctx.paths, share, artifact)
