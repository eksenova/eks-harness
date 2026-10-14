from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ExecContext
from eks_harness.ir.time import TICKS_PER_SECOND, Time, format_time
from eks_harness.ir.types import MediaInfo

BITEXACT_OUTPUT = ("-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-map_metadata", "-1",
                   "-map_chapters", "-1")
FFMPEG_TIMEOUT = 3600.0
IMAGE_CODECS = frozenset({"png", "mjpeg", "webp", "gif", "bmp", "tiff", "jpegls", "svg"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"})


class MediaToolError(NodeFailure):
    pass


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise MediaToolError("resolution_unavailable", f"{name} is not installed on this machine; install ffmpeg")
    return found


@functools.cache
def ffmpeg_version() -> str:
    done = subprocess.run([tool("ffmpeg"), "-version"], capture_output=True, text=True, check=False, timeout=30)
    first = (done.stdout or "").splitlines()[0] if done.stdout else ""
    return first.split(" Copyright")[0].removeprefix("ffmpeg version ").strip() or "unknown"


def ffmpeg(inputs: Sequence[str], outputs: Sequence[str], *, threads: int = 1, cwd: Path | None = None,
           timeout: float = FFMPEG_TIMEOUT, stdin: bytes | None = None) -> None:
    argv = [tool("ffmpeg"), *([] if stdin is not None else ["-nostdin"]), "-y", "-hide_banner", "-loglevel", "error",
            "-threads", str(threads), *inputs, *BITEXACT_OUTPUT, "-threads", str(threads), *outputs]
    try:
        done = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise MediaToolError("timeout", f"ffmpeg did not finish in {timeout:.0f}s") from error
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace")[-1500:]
        raise MediaToolError("executor_error", f"ffmpeg failed ({done.returncode}): {tail}",
                             detail={"argv": [str(item) for item in argv[:40]]})


def ffprobe(path: Path) -> dict[str, Any]:
    argv = [tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        raise MediaToolError("content_unavailable", f"ffprobe cannot read {path.name}: {done.stderr[-500:]}")
    return json.loads(done.stdout or "{}")


def _rate(text: str | None) -> Fraction | None:
    if not text or text in ("0/0", "0"):
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _duration(seconds: object) -> Time | None:
    try:
        value = Fraction(str(seconds))
    except (ValueError, TypeError):
        return None
    if value <= 0:
        return None
    return Time(ticks=round(value * TICKS_PER_SECOND))


def probe(path: Path, uri: str, *, kind: str | None = None, artifact: str | None = None) -> MediaInfo:
    data = ffprobe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    still = video is not None and (video.get("codec_name") in IMAGE_CODECS or path.suffix.lower() in IMAGE_SUFFIXES) \
        and int(video.get("nb_frames") or 1) <= 1
    found = kind or ("image" if still else "video" if video is not None else "audio" if audio is not None else "file")
    duration = None if found == "image" else _duration((data.get("format") or {}).get("duration"))
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video is not None and not still else None
    return MediaInfo(uri=uri, kind=found,  # type: ignore[arg-type]
                     duration=duration,
                     width=int(video["width"]) if video is not None and video.get("width") else None,
                     height=int(video["height"]) if video is not None and video.get("height") else None,
                     fps=fps, artifact=artifact)


@contextmanager
def scratch(call: ExecContext) -> Iterator[Path]:
    directory = call.artifacts.store.scratch()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def ingest(call: ExecContext, path: Path, *, kind: str, name: str, caption: str = "",
           meta: dict[str, JsonValue] | None = None) -> JsonValue:
    artifact = call.artifacts.ingest(path, kind=kind, name=name, caption=caption, meta=meta or {}, move=True)
    info = probe_artifact(call, artifact.id)
    return info.model_dump(mode="json", by_alias=True, exclude_none=True)


def probe_artifact(call: ExecContext, artifact_id: str) -> MediaInfo:
    path = call.artifacts.file(artifact_id)
    if path is None:
        raise MediaToolError("executor_error", f"artifact {artifact_id} has no stored file")
    return probe(path, f"artifact:{artifact_id}", artifact=artifact_id)


def seconds(ticks: int) -> float:
    return ticks / TICKS_PER_SECOND


def time_text(ticks: int) -> str:
    return format_time(Time(ticks=ticks))


def temp_file(directory: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, suffix=suffix)
    os.close(descriptor)
    return Path(name)


__all__ = [
    "BITEXACT_OUTPUT",
    "MediaToolError",
    "ffmpeg",
    "ffmpeg_version",
    "ffprobe",
    "ingest",
    "probe",
    "probe_artifact",
    "scratch",
    "seconds",
    "temp_file",
    "time_text",
    "tool",
]
