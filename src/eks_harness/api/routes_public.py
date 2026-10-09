from __future__ import annotations

import html
import re
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from eks_harness.api.deps import get_ctx, resolve_principal
from eks_harness.api import unfurl
from eks_harness.api.errors import ApiError, not_found, unauthorized
from eks_harness.api.routes_shares import shared_artifact_out
from eks_harness.auth.core import Principal
from eks_harness.daemon.app import spa_headers, web_dist_dir
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import users as users_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.shares import Share
from eks_harness.ids import is_ulid
from eks_harness.store import access as store_access
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import layout, media, sites, sitetokens
from eks_harness.store import shares as store_shares
from eks_harness.store.serving import RAW_CSP, content_disposition, etag_for, file_response, raw_csp, site_headers

router = APIRouter(include_in_schema=False)
METHODS = ["GET", "HEAD"]
SITE_EXPIRED = "The site link expired or is invalid; open the artifact again from the UI."


def _principal(request: Request) -> Principal:
    principal = resolve_principal(request)
    if principal is None:
        raise unauthorized()
    return principal


def _artifact(ctx: AppContext, principal: Principal, artifact_id: str) -> Artifact:
    value = (artifact_id or "").upper()
    if not is_ulid(value):
        raise not_found(f"No artifact {artifact_id}.", error="artifact_not_found")
    return store_access.visible_artifact(ctx.db, principal, value, "viewer")


def raw_response(request: Request, ctx: AppContext, artifact: Artifact, download: bool,
                 extra_headers: dict[str, str] | None = None) -> Response:
    try:
        path = layout.file_path(ctx.paths, artifact.rel_path)
    except layout.UnsafePath:
        raise not_found("The file is gone from the store.", error="file_missing") from None
    inline = media.is_inline(artifact.kind, artifact.mime) and not download
    if inline:
        content_type = media.served_content_type(artifact.mime)
    elif artifact.kind in media.HTMLISH_KINDS or artifact.mime in media.DANGEROUS_MIMES:
        content_type = "application/octet-stream"
    else:
        content_type = artifact.mime or "application/octet-stream"
    headers = {"Content-Security-Policy": raw_csp(artifact.mime, inline), "Referrer-Policy": "no-referrer"}
    headers.update(extra_headers or {})
    return file_response(request, path, content_type=content_type,
                         etag=etag_for(artifact.sha256, path, artifact.size),
                         disposition=content_disposition("inline" if inline else "attachment", artifact.filename),
                         headers=headers)


@router.api_route("/raw/{artifact_id}/{filename:path}", methods=METHODS)
def raw_file(artifact_id: str, filename: str, request: Request, download: bool = Query(False)) -> Response:
    ctx = get_ctx(request)
    artifact = _artifact(ctx, _principal(request), artifact_id)
    return raw_response(request, ctx, artifact, download)


@router.api_route("/raw/{artifact_id}", methods=METHODS)
def raw_file_bare(artifact_id: str, request: Request, download: bool = Query(False)) -> Response:
    ctx = get_ctx(request)
    artifact = _artifact(ctx, _principal(request), artifact_id)
    return raw_response(request, ctx, artifact, download)


@router.api_route("/thumb/{name}", methods=METHODS)
def thumbnail(name: str, request: Request) -> Response:
    ctx = get_ctx(request)
    artifact = _artifact(ctx, _principal(request), name.removesuffix(".jpg"))
    path = layout.thumb_path(ctx.paths, artifact.rel_path)
    if not path.is_file():
        raise not_found(f"The artifact {artifact.id} has no thumbnail.", error="no_thumbnail")
    return file_response(request, path, content_type="image/jpeg", etag=etag_for(None, path, path.stat().st_size),
                         headers={"Content-Security-Policy": RAW_CSP})


def _redirect(location: str, request: Request, kind: str | None = None) -> RedirectResponse:
    query = request.url.query
    return RedirectResponse(location + (f"?{query}" if query else ""), status_code=302,
                            headers={"Cache-Control": "no-store", **site_headers(kind)})


def serve_site(request: Request, ctx: AppContext, artifact: Artifact, rel: str, base: str) -> Response:
    entry = store_artifacts.site_entry(artifact)
    if entry is None:
        raise not_found(f"The artifact {artifact.id} is not a site.", error="not_a_site")
    normalized = layout.request_site_path(rel)
    if normalized is None:
        raise ApiError(404, "site_file_missing", "No such file in the site.",
                       headers={**site_headers(artifact.kind), "Cache-Control": "no-store"})
    if normalized == "":
        return _redirect(base + quote(entry, safe="/"), request, artifact.kind)
    target = layout.site_file(ctx.paths, artifact.rel_path, normalized)
    if target is not None and target.is_dir():
        index = target / "index.html"
        if index.is_file():
            if not rel.endswith("/"):
                return _redirect(base + quote(normalized, safe="/") + "/", request, artifact.kind)
            target = index
    if target is None or not target.is_file():
        raise ApiError(404, "site_file_missing", f"No file {normalized} in the site.",
                       headers={**site_headers(artifact.kind), "Cache-Control": "no-store"})
    mime = sites.site_file_mime(target.name)
    content_type = mime if mime in ("text/html", "application/xhtml+xml") else media.served_content_type(mime)
    return file_response(request, target, content_type=content_type,
                         etag=etag_for(None, target, target.stat().st_size), headers=site_headers(artifact.kind))


@router.api_route("/site/{artifact_id}", methods=METHODS)
def site_root(artifact_id: str, request: Request) -> Response:
    return site_file(artifact_id, "", request)


@router.api_route("/site/{artifact_id}/{path:path}", methods=METHODS)
def site_file(artifact_id: str, path: str, request: Request) -> Response:
    ctx = get_ctx(request)
    artifact_id = artifact_id.upper()
    base = f"/site/{artifact_id}/"
    if not ctx.auth_enabled:
        artifact = _artifact(ctx, _principal(request), artifact_id)
        return serve_site(request, ctx, artifact, path, base)
    first, _, rest = path.partition("/")
    if sitetokens.is_token_segment(first):
        grant = sitetokens.verify(ctx.db, artifact_id, first)
        user = users_repo.get(ctx.db.conn(), grant.user_id) if grant else None
        if grant is None or user is None or user.disabled:
            raise ApiError(401, "site_token_invalid", SITE_EXPIRED, headers=site_headers())
        principal = Principal(user_id=user.id, username=user.username, role=user.role, via="site")
        artifact = _artifact(ctx, principal, artifact_id)
        return serve_site(request, ctx, artifact, rest if path != first else "", f"{base}{first}/")
    principal = _principal(request)
    artifact = _artifact(ctx, principal, artifact_id)
    if principal.via == "cookie":
        token = sitetokens.issue(ctx.db, artifact.id, principal.user_id)
        return _redirect(f"{base}{token}/{quote(path, safe='/')}", request, artifact.kind)
    return serve_site(request, ctx, artifact, path, base)


def _counts_as_view(request: Request) -> bool:
    if request.method != "GET" or unfurl.is_preview_bot(request):
        return False
    if request.headers.get("sec-fetch-dest", "document") not in ("document", ""):
        return False
    requested = (request.headers.get("range") or "").replace(" ", "").lower()
    return not requested or requested.startswith("bytes=0-")


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _share_error_page(problem: ApiError) -> HTMLResponse:
    body = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" content=\"noindex\">"
            f"<title>Share link unavailable</title><style>{_FALLBACK_CSS}</style></head><body><main>"
            f"<h1>Share link unavailable</h1><p>{html.escape(problem.message)}</p></main></body></html>")
    return HTMLResponse(body, status_code=problem.status, headers=_share_headers())


def _share_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow"}


_FALLBACK_CSS = (
    ":root{color-scheme:light dark;--bg:#fff;--fg:#000;--muted:#595959;--rule:#d9d9d9;--box:#f2f2f2}"
    "@media (prefers-color-scheme:dark){:root{--bg:#000;--fg:#ededed;--muted:#a3a3a3;--rule:#333;--box:#111}}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 'IBM Plex Sans',system-ui,sans-serif}"
    "main{padding:16px;max-width:1200px;margin:0 auto}h1{font-size:20px;font-weight:600;line-height:1.15;margin:0 0 8px}"
    "p{margin:0 0 12px;color:var(--muted)}.view{background:var(--box);border:1px solid var(--rule);margin:0 0 12px}"
    ".view img,.view video{display:block;max-width:100%;max-height:80vh;margin:0 auto}"
    ".view iframe{display:block;width:100%;height:80vh;border:0;background:#fff}"
    "a{color:var(--fg)}@media (min-width:768px){main{padding:24px}}"
)


def _fallback_share_page(ctx: AppContext, share: Share, artifact: Artifact) -> HTMLResponse:
    token = share.token
    raw = f"/s/{token}/raw"
    name = html.escape(artifact.filename)
    entry = store_artifacts.site_entry(artifact)
    if entry:
        viewer = (f'<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" '
                  f'src="/s/{token}/{html.escape(quote(entry, safe="/"))}" title="{name}"></iframe>')
    elif artifact.mime.startswith("image/") and media.is_inline(artifact.kind, artifact.mime):
        viewer = f'<img src="{raw}" alt="{html.escape(artifact.caption or artifact.filename)}">'
    elif artifact.mime.startswith("video/"):
        viewer = f'<video src="{raw}" controls preload="metadata"></video>'
    elif media.is_inline(artifact.kind, artifact.mime):
        viewer = f'<iframe sandbox src="{raw}" title="{name}"></iframe>'
    else:
        viewer = ""
    caption = f"<p>{html.escape(artifact.caption)}</p>" if artifact.caption else ""
    view = f'<div class="view">{viewer}</div>' if viewer else ""
    body = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" content=\"noindex\">"
            f"<title>{name}</title>{unfurl.meta_tags(ctx.links, share, artifact, ctx.links.share(share.token))}"
            f"<style>{_FALLBACK_CSS}</style></head><body><main><h1>{name}</h1>{caption}"
            f"{view}"
            f"<p><a href=\"{raw}?download=1\">Download {name}</a> ({html.escape(_size(artifact.size))})</p>"
            f"</main></body></html>")
    return HTMLResponse(body, headers=_share_headers())


def _size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _resolve_uncached(ctx: AppContext, token: str) -> tuple[Share, Artifact]:
    try:
        return store_shares.resolve(ctx.db.conn(), token)
    except ApiError as problem:
        problem.headers = {**problem.headers, **_share_headers()}
        raise


@router.api_route("/s/{token}", methods=METHODS)
def share_page(token: str, request: Request) -> Response:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    wants_json = _wants_json(request)
    try:
        share, artifact = _resolve_uncached(ctx, token)
    except ApiError as problem:
        if wants_json:
            raise
        return _share_error_page(problem)
    if wants_json:
        payload = shared_artifact_out(conn, ctx.links, ctx.paths, share, artifact)
        return JSONResponse(payload.model_dump(mode="json", by_alias=True), headers=_share_headers())
    if request.method == "GET" and not unfurl.is_preview_bot(request):
        store_shares.record_view(ctx.db, share.token)
    dist = web_dist_dir()
    if dist is not None:
        index = dist / "index.html"
        page = _with_share_head(index.read_text(encoding="utf-8"), ctx, share, artifact)
        return HTMLResponse(page, headers={**spa_headers(index), **_share_headers()})
    return _fallback_share_page(ctx, share, artifact)


_TITLE = re.compile(r"<title>.*?</title>", re.DOTALL)


def _with_share_head(page: str, ctx: AppContext, share: Share, artifact: Artifact) -> str:
    head = (f"<title>{html.escape(unfurl.title_of(artifact))}</title>"
            f"{unfurl.meta_tags(ctx.links, share, artifact, ctx.links.share(share.token))}")
    if _TITLE.search(page):
        return _TITLE.sub(lambda _: head, page, count=1)
    return page.replace("</head>", head + "</head>", 1)


@router.api_route("/t/{name}", methods=METHODS)
def share_poster(name: str, request: Request) -> Response:
    ctx = get_ctx(request)
    share, artifact = _resolve_uncached(ctx, name.removesuffix(".jpg"))
    path = None
    if unfurl.has_visual(artifact) and unfurl.ensure_poster(ctx.paths, artifact):
        path = layout.poster_path(ctx.paths, artifact.rel_path)
    else:
        thumb = layout.thumb_path(ctx.paths, artifact.rel_path)
        path = thumb if thumb.is_file() else None
    if path is None:
        raise ApiError(404, "no_preview", "The shared artifact has no preview image.", headers=_share_headers())
    return file_response(request, path, content_type="image/jpeg", etag=etag_for(None, path, path.stat().st_size),
                         headers={"Content-Security-Policy": RAW_CSP, **_direct_headers()})


@router.api_route("/s/{token}/raw", methods=METHODS)
def share_raw(token: str, request: Request, download: bool = Query(False)) -> Response:
    ctx = get_ctx(request)
    share, artifact = _resolve_uncached(ctx, token)
    if _counts_as_view(request):
        store_shares.record_view(ctx.db, share.token)
    return raw_response(request, ctx, artifact, download, {"X-Robots-Tag": "noindex, nofollow"})


@router.api_route("/s/{token}/{path:path}", methods=METHODS)
def share_site(token: str, path: str, request: Request) -> Response:
    ctx = get_ctx(request)
    share, artifact = _resolve_uncached(ctx, token)
    if store_artifacts.site_entry(artifact) is None:
        if not path:
            return RedirectResponse(f"/s/{share.token}", status_code=302, headers=_share_headers())
        raise not_found("A share link serves only its own artifact.", error="not_found")
    if _counts_as_view(request) and path == store_artifacts.site_entry(artifact):
        store_shares.record_view(ctx.db, share.token)
    return serve_site(request, ctx, artifact, path, f"/s/{share.token}/")


def _direct_headers() -> dict[str, str]:
    return {"Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "cross-origin"}


def _resolve_direct(ctx: AppContext, request: Request, token: str) -> tuple[Share, Artifact] | HTMLResponse:
    try:
        return _resolve_uncached(ctx, token)
    except ApiError as problem:
        if (request.headers.get("sec-fetch-dest") == "document"
                or "text/html" in request.headers.get("accept", "")):
            return _share_error_page(problem)
        raise


@router.api_route("/d/{token}", methods=METHODS)
def share_direct_bare(token: str, request: Request) -> Response:
    ctx = get_ctx(request)
    resolved = _resolve_direct(ctx, request, token)
    if isinstance(resolved, Response):
        return resolved
    share, artifact = resolved
    return _redirect(f"/d/{share.token}/{quote(store_shares.direct_path(artifact), safe='/')}", request)


@router.api_route("/d/{token}/{path:path}", methods=METHODS)
def share_direct(token: str, path: str, request: Request, download: bool = Query(False)) -> Response:
    ctx = get_ctx(request)
    resolved = _resolve_direct(ctx, request, token)
    if isinstance(resolved, Response):
        return resolved
    share, artifact = resolved
    if unfurl.is_preview_bot(request) and not download:
        page = unfurl.card_page(ctx.links, share, artifact, store_shares.direct_url(ctx.links, share, artifact))
        return HTMLResponse(page, headers={**_share_headers(), "Content-Security-Policy": "default-src 'none'"})
    entry = store_artifacts.site_entry(artifact)
    if entry is not None and not download:
        if _counts_as_view(request) and path == entry:
            store_shares.record_view(ctx.db, share.token)
        return serve_site(request, ctx, artifact, path, f"/d/{share.token}/")
    if not path:
        return _redirect(f"/d/{share.token}/{quote(artifact.filename, safe='')}", request)
    if _counts_as_view(request):
        store_shares.record_view(ctx.db, share.token)
    return raw_response(request, ctx, artifact, download, _direct_headers())
