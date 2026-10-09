from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from eks_harness.api.errors import ApiError, not_found

CHUNK = 256 * 1024
SITE_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
RAW_CSP = "sandbox"
MEDIA_CSP = "default-src 'none'; media-src 'self'; img-src 'self'; style-src 'unsafe-inline'"
SNAPSHOT_KINDS = frozenset({"dom", "mhtml"})
SNAPSHOT_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; frame-src 'none'; worker-src 'none'; "
                "object-src 'none'; form-action 'none'; img-src * data: blob:; media-src * data: blob:; "
                "style-src * 'unsafe-inline'; font-src * data:")
_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


def content_disposition(disposition: str, filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "") or "download"
    ascii_name = re.sub(r"[\x00-\x1f\x7f;]", "_", ascii_name)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def etag_for(sha256: str | None, path: Path, size: int) -> str:
    if sha256:
        return f'"{sha256[:40]}"'
    stat = path.stat()
    return f'"{int(stat.st_mtime_ns):x}-{size:x}"'


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    candidates = [part.strip().removeprefix("W/") for part in header.split(",")]
    return etag in candidates


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    if not header:
        return None
    if "," in header:
        return None
    match = _RANGE.match(header)
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        length = int(last)
        if length == 0:
            return "unsatisfiable"
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(first)
        if start >= size:
            return "unsatisfiable"
        end = int(last) if last else size - 1
        if end < start:
            return None
        end = min(end, size - 1)
    if size == 0:
        return "unsatisfiable"
    return start, end


def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


async def closing_stream(iterator: Iterator[bytes]) -> AsyncIterator[bytes]:
    done = object()
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(next, iterator, done)
            if chunk is done:
                return
            yield chunk
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def file_response(request: Request, path: Path, *, content_type: str, etag: str | None = None,
                  disposition: str | None = None, cache_control: str = "private, no-cache",
                  headers: dict[str, str] | None = None) -> Response:
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        raise not_found("The file is gone from the store.", error="file_missing") from None
    size = stat.st_size
    base = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
    }
    if etag:
        base["ETag"] = etag
    if disposition:
        base["Content-Disposition"] = disposition
    base.update(headers or {})
    if etag and _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={k: v for k, v in base.items()
                                                  if k in ("ETag", "Cache-Control", "Accept-Ranges")})
    wanted = parse_range(request.headers.get("range"), size)
    if_range = request.headers.get("if-range")
    if wanted is not None and if_range and etag and if_range.strip() != etag:
        wanted = None
    if wanted == "unsatisfiable":
        raise ApiError(416, "range_not_satisfiable", f"The requested range is outside the file ({size} bytes).",
                       headers={"Content-Range": f"bytes */{size}", **base})
    if isinstance(wanted, tuple):
        start, end = wanted
        length = end - start + 1
        status = 206
        base["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        start, length, status = 0, size, 200
    base["Content-Length"] = str(length)
    base["Content-Type"] = content_type
    if request.method == "HEAD":
        return Response(status_code=status, headers=base)
    return StreamingResponse(closing_stream(_iter_file(path, start, length)), status_code=status, headers=base)


def raw_csp(mime: str | None, inline: bool) -> str:
    if inline and (mime or "").startswith(("video/", "audio/")):
        return MEDIA_CSP
    return RAW_CSP


def site_csp(kind: str | None = None) -> str:
    if kind in SNAPSHOT_KINDS:
        return f"{SITE_CSP}; {SNAPSHOT_CSP}"
    return SITE_CSP


def site_headers(kind: str | None = None) -> dict[str, str]:
    return {
        "Content-Security-Policy": site_csp(kind),
        "Access-Control-Allow-Origin": "*",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
