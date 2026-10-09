"""Subprocess wrapper for ffmpeg invocations."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    """Raised when ffmpeg exits non-zero."""

    def __init__(self, returncode: int, command: list[str], stderr: str) -> None:
        self.returncode = returncode
        self.command = command
        self.stderr = stderr
        super().__init__(self._format())

    def _format(self) -> str:
        cmd = " ".join(self.command)
        tail = "\n".join(self.stderr.splitlines()[-20:])
        return f"ffmpeg failed with code {self.returncode}\n  cmd: {cmd}\n  stderr (tail):\n{tail}"


def run_ffmpeg(
    args: list[str],
    *,
    binary: str = "ffmpeg",
    timeout: float | None = None,
    cwd: Path | None = None,
) -> str:
    """Run ffmpeg with ``args`` (excluding the binary itself) and return stderr text.

    Raises :class:`FFmpegError` with structured details when the process exits
    non-zero or :class:`subprocess.TimeoutExpired` on timeout.
    """

    full = [binary, "-hide_banner", "-y", *args]
    _LOG.debug("ffmpeg: %s", " ".join(full))
    try:
        proc = subprocess.run(
            full,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(returncode=-1, command=full, stderr=str(exc)) from exc

    if proc.returncode != 0:
        raise FFmpegError(returncode=proc.returncode, command=full, stderr=proc.stderr or "")
    return proc.stderr or ""


__all__ = ["FFmpegError", "run_ffmpeg"]
