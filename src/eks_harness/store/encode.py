from __future__ import annotations

import json
import math
import os
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from eks_harness.capture.trim import TrimConfig, filter_graph, plan
from eks_harness.store.media import ffmpeg_binary, ffprobe_binary

MAX_MACROBLOCKS = 8192
FPS = 30
TRIM_PAD_ENV = "EKS_TRIM_PAD"
TRIM_FRAC_ENV = "EKS_TRIM_FRAC"
DEFAULT_TRIM_PAD = 2.0
DEFAULT_TRIM_FRAC = 0.33
ENCODE_TIMEOUT = 1800
ENCODE_ARGS = ["-an", "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
               "-r", str(FPS), "-g", str(FPS * 2), "-crf", "23", "-preset", "veryfast", "-movflags", "+faststart"]


class EncodeError(RuntimeError):
    def __init__(self, message: str, missing_tool: bool = False) -> None:
        super().__init__(message)
        self.missing_tool = missing_tool


@dataclass
class EncodeResult:
    full: Path
    trimmed: Path | None = None
    report: list[str] = field(default_factory=list)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _tools() -> tuple[str, str]:
    ffmpeg, ffprobe = ffmpeg_binary(), ffprobe_binary()
    if not ffmpeg or not ffprobe:
        raise EncodeError("ffmpeg and ffprobe are needed to encode recordings (brew install ffmpeg)",
                          missing_tool=True)
    return ffmpeg, ffprobe


def _run(command: list[str], timeout: float = ENCODE_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise EncodeError(f"{Path(command[0]).name} took longer than {timeout:.0f}s") from error


def _probe(ffprobe: str, path: Path) -> dict:
    result = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], 120)
    if result.returncode != 0:
        raise EncodeError(f"ffprobe could not read {path.name}: "
                          f"{result.stderr.decode('utf-8', 'replace')[-300:]}")
    try:
        return json.loads(result.stdout or b"{}")
    except json.JSONDecodeError as error:
        raise EncodeError(f"ffprobe returned no JSON for {path.name}") from error


def _duration(info: dict) -> float:
    try:
        value = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def target_size(width: int, height: int) -> tuple[int, int]:
    limit = MAX_MACROBLOCKS * 256
    factor = 1.0
    if width * height > limit:
        factor = math.sqrt(limit / (width * height))
    target_w = int(width * factor / 2) * 2
    target_h = int(height * factor / 2) * 2
    while ((target_w + 15) // 16) * ((target_h + 15) // 16) > MAX_MACROBLOCKS:
        target_w -= 2
        target_h = int(target_w * height / width / 2) * 2
    return max(2, target_w), max(2, target_h)


def top_level_atoms(path: Path) -> list[str]:
    atoms: list[str] = []
    with open(path, "rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            name = kind.decode("latin-1", "replace")
            if size == 1:
                extended = handle.read(8)
                if len(extended) < 8:
                    atoms.append(name)
                    break
                size = struct.unpack(">Q", extended)[0]
                handle.seek(size - 16, 1)
            elif size == 0:
                atoms.append(name)
                break
            elif size < 8:
                atoms.append(name)
                break
            else:
                handle.seek(size - 8, 1)
            atoms.append(name)
    return atoms


def verify(ffprobe: str, path: Path, expected: float, label: str) -> str:
    info = _probe(ffprobe, path)
    streams = info.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    problems: list[str] = []
    if len(video) != 1:
        problems.append(f"{len(video)} video streams")
    else:
        stream = video[0]
        if stream.get("codec_name") != "h264":
            problems.append(f"codec {stream.get('codec_name')}")
        if stream.get("profile") != "High":
            problems.append(f"profile {stream.get('profile')}")
        if int(stream.get("level", 99)) > 41:
            problems.append(f"level {stream.get('level')}")
        if stream.get("pix_fmt") != "yuv420p":
            problems.append(f"pix_fmt {stream.get('pix_fmt')}")
        if int(stream.get("width", 1)) % 2 or int(stream.get("height", 1)) % 2:
            problems.append(f"odd size {stream.get('width')}x{stream.get('height')}")
        if stream.get("r_frame_rate") != f"{FPS}/1" or stream.get("avg_frame_rate") != f"{FPS}/1":
            problems.append(f"frame rate {stream.get('r_frame_rate')} / {stream.get('avg_frame_rate')}")
    if audio:
        problems.append("has an audio track")
    duration = _duration(info)
    if label == "full" and expected > 0 and abs(duration - expected) > max(0.25, expected * 0.02):
        problems.append(f"duration {duration:.1f}s vs source {expected:.1f}s")
    if duration <= 0:
        problems.append("zero duration")
    atoms = top_level_atoms(path)
    if "moov" not in atoms or "mdat" not in atoms or atoms.index("moov") > atoms.index("mdat"):
        problems.append(f"moov is not before mdat ({' '.join(atoms)})")
    if problems:
        raise EncodeError(f"{label} video failed verification: {path.name}: " + "; ".join(problems))
    stream = video[0]
    return (f"{label}: {duration:.1f}s {stream['width']}x{stream['height']} h264 High L{stream['level']} yuv420p "
            f"{FPS}fps faststart")


def _ffmpeg(ffmpeg: str, args: list[str], output: Path) -> None:
    result = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args, *ENCODE_ARGS, str(output)])
    if result.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise EncodeError(f"ffmpeg failed: {result.stderr.decode('utf-8', 'replace')[-600:]}")


def encode_recording(source: Path, out: Path, *, pad_to: float | None = None, trim: bool = False,
                     crop: str | None = None, events: list[float] | None = None,
                     lease: str | None = None) -> EncodeResult:
    from eks_harness.renderq import render_slot

    with render_slot("encode", Path(out).name, lease=lease, session=lease):
        return _encode_recording(source, out, pad_to=pad_to, trim=trim, crop=crop, events=events)


def _encode_recording(source: Path, out: Path, *, pad_to: float | None = None, trim: bool = False,
                      crop: str | None = None, events: list[float] | None = None) -> EncodeResult:
    ffmpeg, ffprobe = _tools()
    source = Path(source)
    out = Path(out)
    if not source.is_file() or source.stat().st_size == 0:
        raise EncodeError(f"no recording to encode at {source}")
    info = _probe(ffprobe, source)
    video = [s for s in info.get("streams") or [] if s.get("codec_type") == "video"]
    if not video or not video[0].get("width") or not video[0].get("height"):
        raise EncodeError(f"{source.name} has no video stream")
    expected = _duration(info)
    if crop:
        parts = crop.split(":")
        width, height = int(parts[0]), int(parts[1])
    else:
        width, height = int(video[0]["width"]), int(video[0]["height"])
    target_w, target_h = target_size(width, height)
    filters = [f"crop={crop}"] if crop else []
    filters += [f"scale={target_w}:{target_h}:flags=lanczos:out_range=tv", "format=yuv420p", "setsar=1"]
    length: list[str] = []
    if pad_to is not None:
        if not pad_to > 0:
            raise EncodeError(f"the recording length must be a positive number of seconds, not {pad_to}")
        filters.append(f"tpad=stop_mode=clone:stop_duration={pad_to:.3f}")
        length = ["-t", f"{pad_to:.3f}"]
        expected = float(pad_to)
    out.parent.mkdir(parents=True, exist_ok=True)
    encoding = out.with_name(out.stem + ".encoding.mp4")
    _ffmpeg(ffmpeg, ["-i", str(source), "-vf", ",".join(filters) + f",fps={FPS}", *length], encoding)
    report: list[str] = []
    try:
        report.append(verify(ffprobe, encoding, expected, "full"))
    except EncodeError:
        failed = out.with_name(out.stem + ".failed-encode.mp4")
        os.replace(encoding, failed)
        raise
    os.replace(encoding, out)
    result = EncodeResult(full=out, report=report)
    if trim:
        steps = [at for at in events or [] if 0 <= at <= expected]
        if steps:
            result.trimmed = trim_steps(ffmpeg, ffprobe, out, steps, report)
        else:
            result.trimmed = trim_idle(ffmpeg, ffprobe, out, report)
    return result


def trim_steps(ffmpeg: str, ffprobe: str, full: Path, events: list[float], report: list[str]) -> Path:
    try:
        config = TrimConfig.from_env()
    except ValueError as error:
        raise EncodeError(str(error)) from error
    duration = _duration(_probe(ffprobe, full))
    segments = plan(duration, events, config)
    trimmed = full.with_name(full.stem + "-trimmed.mp4")
    encoding = trimmed.with_name(trimmed.stem + ".encoding.mp4")
    _ffmpeg(ffmpeg, ["-i", str(full), "-filter_complex", filter_graph(segments, FPS), "-map", "[outv]"], encoding)
    os.replace(encoding, trimmed)
    report.append(verify(ffprobe, trimmed, 0, "trimmed"))
    return trimmed


def trim_idle(ffmpeg: str, ffprobe: str, full: Path, report: list[str]) -> Path:
    pad = _env_float(TRIM_PAD_ENV, DEFAULT_TRIM_PAD)
    frac = _env_float(TRIM_FRAC_ENV, DEFAULT_TRIM_FRAC)
    duration = _duration(_probe(ffprobe, full))
    decimate = f"mpdecimate=hi=64*12:lo=64*5:frac={frac}:max=15,setpts=N/{FPS}/TB"
    trimmed = full.with_name(full.stem + "-trimmed.mp4")
    encoding = trimmed.with_name(trimmed.stem + ".encoding.mp4")
    if pad > 0 and duration > pad * 2 + 1:
        mid_end = f"{duration - pad:.3f}"
        graph = (f"[0:v]trim=start=0:end={pad},setpts=PTS-STARTPTS[head];"
                 f"[0:v]trim=start={pad}:end={mid_end},setpts=PTS-STARTPTS,{decimate}[mid];"
                 f"[0:v]trim=start={mid_end},setpts=PTS-STARTPTS[tail];"
                 f"[head][mid][tail]concat=n=3:v=1:a=0,fps={FPS}[outv]")
        _ffmpeg(ffmpeg, ["-i", str(full), "-filter_complex", graph, "-map", "[outv]"], encoding)
    else:
        _ffmpeg(ffmpeg, ["-i", str(full), "-vf", f"{decimate},fps={FPS}"], encoding)
    os.replace(encoding, trimmed)
    report.append(verify(ffprobe, trimmed, 0, "trimmed"))
    return trimmed


__all__ = ["EncodeError", "EncodeResult", "encode_recording", "target_size", "top_level_atoms", "trim_idle",
           "trim_steps", "verify"]
