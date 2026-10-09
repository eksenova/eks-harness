from __future__ import annotations

import base64
import io
import quopri
import shutil
import struct
import zipfile
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness.db import Database
from eks_harness.db.repos import leases
from eks_harness.pools.fake_media import TINY_JPEG, TINY_MP4

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not on PATH")
needs_ffprobe = pytest.mark.skipif(not HAS_FFPROBE, reason="ffprobe is not on PATH")

__all__ = ["TINY_JPEG", "TINY_MP4", "HAS_FFMPEG", "HAS_FFPROBE", "needs_ffmpeg", "needs_ffprobe", "png_bytes",
           "upload", "local_path", "make_zip", "add_lease", "end_lease", "chrome_mhtml"]


def png_bytes(width: int = 40, height: int = 30, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def upload(client: TestClient, content: bytes, filename: str, mime: str = "application/octet-stream",
           headers: dict | None = None, **fields) -> object:
    data = {k: v for k, v in fields.items() if v is not None}
    return client.post("/api/artifacts", data=data, files={"file": (filename, content, mime)}, headers=headers or {})


def local_path(url: str) -> str:
    return "/" + url.split("://", 1)[1].split("/", 1)[1]


def make_zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, target)
    return buffer.getvalue()


def add_lease(db: Database, session_id: int, kind: str = "ios", resource: str = "ios-1",
              instance: str = "agent-1") -> leases.Lease:
    with db.transaction() as conn:
        return leases.insert(conn, kind=kind, owner_instance=instance, state="active", resource=resource,
                             session_id=session_id, phase="ready")


def end_lease(db: Database, lease: leases.Lease, state: str = "released") -> leases.Lease:
    with db.transaction() as conn:
        return leases.end(conn, lease.id, state, reason="test")


def chrome_mhtml() -> bytes:
    boundary = "----MultipartBoundary--TestBoundary123----"
    html_body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="https://app.example.test/assets/style.css">'
        '<base href="https://app.example.test/dashboard/">'
        "</head><body><h1>Dashboard \u00e7</h1>"
        '<img src="../img/logo.png" srcset="../img/logo.png 1x, https://app.example.test/img/logo.png 2x">'
        '<div style="background: url(\'https://app.example.test/img/bg.png\')">x</div>'
        '<a href="https://elsewhere.test/page">out</a>'
        "</body></html>"
    )
    css_body = "body{background:url(../img/bg.png)} h1{color:#000}"
    logo = png_bytes(4, 4)
    background = png_bytes(2, 2, (0, 0, 0))
    def b64(data: bytes) -> str:
        return base64.encodebytes(data).decode("ascii")

    def qp(text: str) -> str:
        return quopri.encodestring(text.encode("utf-8")).decode("ascii")

    parts = [
        ("text/html", "quoted-printable", "https://app.example.test/dashboard/", qp(html_body),
         "<frame-main@mhtml.blink>"),
        ("text/css", "quoted-printable", "https://app.example.test/assets/style.css", qp(css_body), None),
        ("image/png", "base64", "https://app.example.test/img/logo.png", b64(logo), None),
        ("image/png", "base64", "https://app.example.test/img/bg.png", b64(background), None),
    ]
    lines = [
        "From: <Saved by Blink>",
        "Snapshot-Content-Location: https://app.example.test/dashboard/",
        "Subject: Dashboard",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; type="text/html"; boundary="{boundary}"',
        "",
        "",
    ]
    for mime, encoding, location, body, content_id in parts:
        lines.append(f"--{boundary}")
        lines.append(f"Content-Type: {mime}" + ("; charset=utf-8" if mime.startswith("text/") else ""))
        if content_id:
            lines.append(f"Content-ID: {content_id}")
        lines.append(f"Content-Transfer-Encoding: {encoding}")
        lines.append(f"Content-Location: {location}")
        lines.append("")
        lines.append(body)
        lines.append("")
    lines.append(f"--{boundary}--")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


def fixture_path(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
