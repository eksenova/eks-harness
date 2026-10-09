"""Built-in media providers (filesystem, yt-dlp)."""

from __future__ import annotations

from .filesystem import FilesystemProvider
from .yt_dlp import YtDlpProvider

__all__ = ["FilesystemProvider", "YtDlpProvider"]
