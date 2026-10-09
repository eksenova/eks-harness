"""yt-dlp media provider.

Handles ``yt-dlp://`` URIs and the common YouTube hostnames. Downloads are
cached under ``cache/downloads/<sha>.<ext>`` so repeated runs of the same
project never re-download. ``yt_dlp`` is a soft dependency in the
``[download]`` extra; if it is not installed, :meth:`resolve` raises a
clear, install-hinted error rather than a generic ImportError.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from eks_harness.video.plugins.base import MediaProvider

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["YtDlpProvider"]

_DEFAULT_HOSTS: frozenset[str] = frozenset(
    {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"}
)


class YtDlpProvider(MediaProvider):
    name: ClassVar[str] = "yt_dlp"

    def __init__(self, blocklist: frozenset[str] | None = None) -> None:
        self._blocklist = blocklist or frozenset()

    def can_handle(self, uri: str) -> bool:
        if uri.startswith("yt-dlp://"):
            return True
        if uri.startswith(("http://", "https://")):
            return urlparse(uri).hostname in _DEFAULT_HOSTS
        return False

    def resolve(self, uri: str, ctx: RenderContext) -> Path:
        download_url = _normalize_uri(uri)
        host = urlparse(download_url).hostname or ""
        if host in self._blocklist:
            raise PermissionError(f"yt_dlp provider refuses host {host!r} (blocklist)")

        cache_dir = ctx.cache_dir / "downloads"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(download_url.encode("utf-8")).hexdigest()[:32]
        for existing in cache_dir.glob(f"{digest}.*"):
            return existing

        ydl = _new_yt_dlp_or_raise(cache_dir, digest)
        info = ydl.extract_info(download_url, download=True)
        downloaded = ydl.prepare_filename(info)
        return Path(downloaded)


def _normalize_uri(uri: str) -> str:
    if uri.startswith("yt-dlp://"):
        return uri[len("yt-dlp://") :]
    return uri


def _new_yt_dlp_or_raise(cache_dir: Path, digest: str) -> Any:
    try:
        import yt_dlp  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError as exc:
        raise RuntimeError(
            "yt_dlp provider requires yt-dlp; install with `pip install eks-harness[download]`"
        ) from exc
    return yt_dlp.YoutubeDL({
        "outtmpl": str(cache_dir / f"{digest}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    })
