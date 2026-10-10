from __future__ import annotations

import tempfile
import time

from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx, require_admin, resolve_principal
from eks_harness.api.errors import bad_request, conflict, not_found, unauthorized
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.daemon.hooks import on_shutdown, on_startup
from eks_harness.db.repos import nodes as repo
from eks_harness.nodes.blobs import BlobError
from eks_harness.nodes.host_node import HOST_FLAG, SERVICE as HOST_SERVICE, HostNode
from eks_harness.nodes.hub import NodeError, hub

router = APIRouter(tags=["nodes"])
NODE_PREFIX = "Bearer ehn_"


def _node_token(headers) -> str | None:
    value = headers.get("authorization") or ""
    return value[len("Bearer "):].strip() if value.startswith(NODE_PREFIX) else None


def blob_access(request: Request) -> str:
    ctx = get_ctx(request)
    token = _node_token(request.headers)
    if token is not None:
        node = hub(ctx).authenticate(token)
        if node is None:
            raise unauthorized("The node token is unknown or revoked.", error="invalid_node_token")
        return f"node:{node.id}"
    principal = resolve_principal(request)
    if principal is None:
        raise unauthorized()
    return f"user:{principal.username}"


@router.websocket("/api/nodes/connect")
async def connect(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx
    node = hub(ctx).authenticate(_node_token(websocket.headers))
    if node is None:
        await websocket.close(code=4401, reason="invalid node token")
        return
    await websocket.accept()
    await hub(ctx).serve(websocket, node)


@router.get("/api/nodes")
def list_nodes(request: Request, _: Principal = Depends(current_principal)) -> dict:
    return {"items": hub(get_ctx(request)).status()}


@router.post("/api/nodes")
def create_node(request: Request, body: dict = Body(...), _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    try:
        node, token = hub(ctx).create_node(str(body.get("id") or ""), label=str(body.get("label") or ""),
                                           config=body.get("config") or {})
    except NodeError as error:
        raise conflict(str(error), error="node_exists") if "exists" in str(error) else bad_request(str(error)) from error
    return {"node": node.public(), "token": token, "hubUrl": hub_url(ctx)}


def hub_url(ctx: AppContext) -> str:
    base = ctx.config["nodes.hubUrl"] or ctx.config["server.publicUrl"] or ctx.config.local_url()
    return base.rstrip("/")


def _one(ctx: AppContext, node_id: str) -> dict:
    for item in hub(ctx).status():
        if item["id"] == node_id:
            return item
    raise not_found(f"No node {node_id}.", error="node_not_found")


@router.get("/api/nodes/{node_id}")
def get_node(node_id: str, request: Request, _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    item = _one(ctx, node_id)
    item["jobs"] = [j.public() for j in repo.list_jobs(ctx.db.conn(), node_id=node_id, limit=50)]
    return item


@router.patch("/api/nodes/{node_id}")
def patch_node(node_id: str, request: Request, body: dict = Body(...), _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    nodes = hub(ctx)
    try:
        if "config" in body:
            if not isinstance(body["config"], dict):
                raise bad_request("config must be an object")
            config = dict(body["config"])
            if _is_host_node(ctx, node_id):
                config[HOST_FLAG] = True
            nodes.update_config(node_id, config)
        if "disabled" in body:
            nodes.set_disabled(node_id, bool(body["disabled"]))
        if "label" in body:
            with ctx.db.transaction() as conn:
                repo.update_node(conn, node_id, label=str(body["label"]))
    except NodeError as error:
        raise not_found(str(error), error="node_not_found") from error
    return _one(ctx, node_id)


def _is_host_node(ctx: AppContext, node_id: str) -> bool:
    node = repo.get_node(ctx.db.conn(), node_id)
    return bool(node and node.config.get(HOST_FLAG))


def _refuse_host_node(ctx: AppContext, node_id: str, action: str) -> None:
    if _is_host_node(ctx, node_id) and ctx.config["nodes.hostNode"]:
        raise bad_request(f"{node_id} is this hub machine's own node; {action} is not possible while it runs. "
                          f"Disable it, or turn off nodes.hostNode and restart the daemon.", error="host_node")


@router.delete("/api/nodes/{node_id}")
def delete_node(node_id: str, request: Request, _: Principal = Depends(require_admin)) -> dict:
    _refuse_host_node(get_ctx(request), node_id, "removing it")
    if not hub(get_ctx(request)).remove_node(node_id):
        raise not_found(f"No node {node_id}.", error="node_not_found")
    return {"removed": node_id}


@router.post("/api/nodes/{node_id}/token")
def rotate(node_id: str, request: Request, _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    _refuse_host_node(ctx, node_id, "a new token")
    try:
        token = hub(ctx).rotate_token(node_id)
    except NodeError as error:
        raise not_found(str(error), error="node_not_found") from error
    return {"token": token, "hubUrl": hub_url(ctx)}


@router.put("/api/blobs/{digest}")
async def put_blob(digest: str, request: Request, who: str = Depends(blob_access)) -> dict:
    blobs = hub(get_ctx(request)).blobs
    if blobs.has(digest):
        blobs.touch(digest)
        return {"hash": digest, "existed": True}
    spill = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    async for chunk in request.stream():
        spill.write(chunk)
    spill.seek(0)
    try:
        value, size = await run_in_threadpool(blobs.put_stream, iter(lambda: spill.read(1 << 20), b""), digest)
    except BlobError as error:
        raise bad_request(str(error), error="hash_mismatch") from error
    finally:
        spill.close()
    return {"hash": value, "size": size, "by": who}


@router.post("/api/blobs")
async def post_blob(request: Request, who: str = Depends(blob_access)) -> dict:
    blobs = hub(get_ctx(request)).blobs
    spill = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    async for chunk in request.stream():
        spill.write(chunk)
    spill.seek(0)
    try:
        value, size = await run_in_threadpool(blobs.put_stream, iter(lambda: spill.read(1 << 20), b""))
    finally:
        spill.close()
    return {"hash": value, "size": size, "by": who}


@router.head("/api/blobs/{digest}")
def head_blob(digest: str, request: Request, _: str = Depends(blob_access)) -> dict:
    if not hub(get_ctx(request)).blobs.has(digest):
        raise not_found(f"No blob {digest}.", error="blob_not_found")
    return {}


@router.get("/api/blobs/{digest}")
def get_blob(digest: str, request: Request, _: str = Depends(blob_access)) -> StreamingResponse:
    blobs = hub(get_ctx(request)).blobs
    if not blobs.has(digest):
        raise not_found(f"No blob {digest}.", error="blob_not_found")
    blobs.touch(digest)
    size = blobs.path(digest).stat().st_size
    return StreamingResponse(blobs.iter_chunks(digest), media_type="application/octet-stream",
                             headers={"Content-Length": str(size), "Cache-Control": "private, max-age=31536000"})


@router.post("/api/jobs")
def submit_job(request: Request, body: dict = Body(...), principal: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    kind = str(body.get("kind") or "")
    if not kind:
        raise bad_request("Send the job kind.", error="no_kind")
    try:
        job = hub(ctx).submit(kind, payload=body.get("payload") or {}, requirements=body.get("requirements") or {},
                              inputs=body.get("inputs") or [], priority=int(body.get("priority") or 0),
                              owner=principal.username)
    except NodeError as error:
        raise bad_request(str(error), error="bad_job") from error
    return job.public()


@router.get("/api/jobs")
def list_jobs(request: Request, state: str | None = Query(None), node: str | None = Query(None),
              kind: str | None = Query(None), limit: int = Query(100, ge=1, le=1000),
              _: Principal = Depends(current_principal)) -> dict:
    states = tuple(s for s in (state or "").split(",") if s) or None
    jobs = repo.list_jobs(get_ctx(request).db.conn(), states=states, node_id=node, kind=kind, limit=limit)
    return {"items": [j.public() for j in jobs]}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request, wait: float = Query(0, ge=0, le=600),
                  _: Principal = Depends(current_principal)) -> dict:
    ctx = get_ctx(request)
    if wait:
        try:
            job = await hub(ctx).wait_async(job_id, wait)
        except NodeError as error:
            raise not_found(str(error), error="job_not_found") from error
    else:
        job = repo.get_job(ctx.db.conn(), job_id)
    if job is None:
        raise not_found(f"No job {job_id}.", error="job_not_found")
    return job.public()


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, _: Principal = Depends(require_admin)) -> dict:
    job = hub(get_ctx(request)).cancel(job_id)
    if job is None:
        raise not_found(f"No job {job_id}.", error="job_not_found")
    return job.public()


@on_startup(order=40)
def _start(ctx: AppContext) -> None:
    nodes = hub(ctx)
    with ctx.db.transaction() as conn:
        for job in repo.list_jobs(conn, states=("assigned", "running")):
            repo.update_job(conn, job.id, state="queued", node_id=None, slot=None, message="hub restarted")
    nodes.blobs.prune(float(ctx.config["nodes.blobRetentionDays"]) * 86400)
    with ctx.db.transaction() as conn:
        repo.prune_jobs(conn, time.time() - 90 * 86400)
    if ctx.config["nodes.hostNode"] and not ctx.pools.fake:
        host_node = HostNode(ctx)
        ctx.register_service(HOST_SERVICE, host_node)
        host_node.start()


@on_shutdown(order=5)
def _stop(ctx: AppContext) -> None:
    if ctx.has_service(HOST_SERVICE):
        ctx.service(HOST_SERVICE).stop()
    if ctx.has_service("nodes"):
        for node_id in list(hub(ctx).connections):
            hub(ctx).disconnect(node_id, "hub stopping")
