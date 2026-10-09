from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from eks_harness.api.deps import get_ctx, require_admin
from eks_harness.api.errors import bad_request, not_found
from eks_harness.api.schemas import LogResourceList, LogResponse
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.pools.backends import log_tail

router = APIRouter(tags=["logs"])

MAX_CHUNK = 2_000_000
FOLLOW_INTERVAL = 1.0
RESOURCE_HELP = "resource: daemon | browser:<n> | ios:<n> | android:<n> | backend:<id>[:<process>]"


def log_file(ctx: AppContext, resource: str) -> Path:
    pools = ctx.pools
    if resource == "daemon":
        return ctx.paths.daemon_log
    if resource.startswith("browser:"):
        index = resource.split(":")[1]
        if not index.isdigit():
            raise bad_request(RESOURCE_HELP, error="invalid_resource")
        return pools.browsers.log_path(int(index))
    if resource.startswith(("ios:", "android:")):
        if resource not in pools.host.devices:
            raise not_found(f"No device {resource} in the pool.", error="device_not_found")
        return pools.devices.log_path(resource)
    if resource.startswith("backend:"):
        rest = resource[len("backend:"):]
        record = pools.backends.get(rest)
        process = None
        if record is None and ":" in rest:
            backend_id, _, process = rest.rpartition(":")
            record = pools.backends.get(backend_id)
        if record is None:
            raise not_found(f"No backend {rest}.", error="backend_not_found")
        if process:
            path = (record.get("logs") or {}).get(process)
            if not path:
                known = ", ".join(record.get("logs") or {}) or "none"
                raise not_found(f"Backend {record['id']} has no process {process}; known: {known}.",
                                error="process_not_found")
            return Path(path)
        return pools.backends.log_path(record)
    raise bad_request(RESOURCE_HELP, error="invalid_resource")


def filtered(text: str, pattern: re.Pattern | None) -> str:
    if pattern is None or not text:
        return text
    lines = [line for line in text.splitlines() if pattern.search(line)]
    return "\n".join(lines) + ("\n" if lines else "")


def compile_grep(grep: str | None) -> re.Pattern | None:
    if not grep:
        return None
    try:
        return re.compile(grep)
    except re.error as error:
        raise bad_request(f"grep is not a valid regular expression: {error}", error="invalid_grep") from error


def read_log(path: Path, resource: str, lines: int, offset: int | None, pattern: re.Pattern | None) -> dict:
    if not path.exists():
        return {"resource": resource, "path": str(path), "offset": 0, "text": ""}
    size = path.stat().st_size
    if offset is not None:
        start = size if offset < 0 else min(offset, size)
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(MAX_CHUNK)
        return {"resource": resource, "path": str(path), "offset": start + len(data),
                "text": filtered(data.decode("utf-8", "replace"), pattern)}
    if pattern is not None:
        text = path.read_text(encoding="utf-8", errors="replace")
        matching = [line for line in text.splitlines() if pattern.search(line)][-lines:]
        return {"resource": resource, "path": str(path), "offset": size, "text": "\n".join(matching)}
    return {"resource": resource, "path": str(path), "offset": size, "text": log_tail(path, lines)}


async def follow(request: Request, path: Path, lines: int, pattern: re.Pattern | None) -> AsyncIterator[bytes]:
    first = read_log(path, "", lines, None, pattern)
    if first["text"]:
        yield (first["text"].rstrip("\n") + "\n").encode("utf-8")
    offset = first["offset"]
    pending = ""
    while not await request.is_disconnected():
        await asyncio.sleep(FOLLOW_INTERVAL)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            continue
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_CHUNK)
        offset += len(data)
        text = pending + data.decode("utf-8", "replace")
        complete, _, pending = text.rpartition("\n")
        if complete:
            chunk = filtered(complete + "\n", pattern)
            if chunk:
                yield chunk.encode("utf-8")


@router.get("/api/logs", response_model=LogResponse)
def get_log(request: Request, resource: str = Query("daemon"), lines: int = Query(200, ge=1, le=100_000),
            offset: int | None = Query(None), grep: str | None = Query(None), follow_: bool = Query(False, alias="follow"),
            _: Principal = Depends(require_admin)):
    ctx = get_ctx(request)
    path = log_file(ctx, resource)
    pattern = compile_grep(grep)
    if follow_:
        return StreamingResponse(follow(request, path, lines, pattern), media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                          "X-Log-Path": str(path)})
    return read_log(path, resource, lines, offset, pattern)


@router.get("/api/logs/resources", response_model=LogResourceList)
def resources(request: Request, _: Principal = Depends(require_admin)) -> dict:
    ctx = get_ctx(request)
    pools = ctx.pools
    items = [{"resource": "daemon", "label": "Daemon", "path": str(ctx.paths.daemon_log)}]
    for index in pools.browsers.indices():
        items.append({"resource": f"browser:{index}", "label": f"Browser {index}",
                      "path": str(pools.browsers.log_path(index))})
    for key, device in sorted(pools.host.devices.items()):
        items.append({"resource": key, "label": str(device.get("name") or key), "path": str(pools.devices.log_path(key))})
    for record in list(pools.host.backends.values()):
        items.append({"resource": f"backend:{record['id']}", "label": f"Backend {record['id']}",
                      "path": str(pools.backends.log_path(record))})
        for name, path in (record.get("logs") or {}).items():
            items.append({"resource": f"backend:{record['id']}:{name}", "label": f"Backend {record['id']} {name}",
                          "path": str(path)})
    return {"items": items}
