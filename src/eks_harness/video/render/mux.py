"""Final mux step: concatenate per-segment outputs into the deliverable file."""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .subprocess_runner import run_ffmpeg

__all__ = ["mux_segments"]


def mux_segments(
    segment_paths: Sequence[Path],
    output: Path,
    *,
    audio_path: Path | None = None,
    binary: str = "ffmpeg",
) -> Path:
    """Concatenate ``segment_paths`` into ``output`` using the concat demuxer.

    If ``audio_path`` is supplied it is muxed alongside the concatenated
    video. Single-segment inputs are concatenated through the demuxer too;
    the cost is negligible and the code path stays uniform.
    """

    if not segment_paths:
        raise ValueError("mux_segments requires at least one segment")

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as listfile:
        list_path = Path(listfile.name)
        for segment in segment_paths:
            escaped = str(Path(segment).resolve()).replace("'", r"\'")
            listfile.write(f"file '{escaped}'\n")

    try:
        if audio_path is None:
            args = [
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(output),
            ]
        else:
            args = [
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_path),
                "-i", str(audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                str(output),
            ]
        run_ffmpeg(args, binary=binary)
    finally:
        with contextlib.suppress(OSError):
            list_path.unlink()
    return output
