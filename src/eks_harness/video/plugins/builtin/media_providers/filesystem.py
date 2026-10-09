"""Local-filesystem media provider.

Handles bare paths and ``file://`` URIs. Validates existence on resolve so
broken project references fail fast with an explicit error rather than
deep inside ffmpeg.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import unquote, urlparse

from eks_harness.video.plugins.base import MediaProvider

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["FilesystemProvider"]


class FilesystemProvider(MediaProvider):
    name: ClassVar[str] = "filesystem"

    def can_handle(self, uri: str) -> bool:
        if uri.startswith("file://"):
            return True
        return "://" not in uri

    def resolve(self, uri: str, ctx: RenderContext) -> Path:
        path = _uri_to_path(uri)
        if not path.is_absolute():
            path = (ctx.workspace / path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"filesystem provider: {path} does not exist")
        return path


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        # urlparse returns netloc separately; on Windows the drive letter ends up there.
        raw = unquote(parsed.path)
        if parsed.netloc and len(parsed.netloc) == 2 and parsed.netloc.endswith(":"):
            raw = f"{parsed.netloc}{raw}"
        elif raw.startswith("/") and len(raw) > 3 and raw[2] == ":":
            raw = raw[1:]
        return Path(raw)
    return Path(uri)
