from __future__ import annotations

import bisect
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from PIL.Image import Image


class MediaError(RuntimeError):
    pass


def _need(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise MediaError(f"{binary} is not on PATH")
    return found


def _run(args: list[str], *, text: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=text, timeout=timeout)
    if result.returncode != 0:
        tail = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise MediaError(f"{Path(args[0]).name} failed: {tail.strip()[-600:]}")
    return result


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "0"):
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    frame_times: list[float] = field(default_factory=list)
    has_audio: bool = False
    sample_rate: int = 0
    seek_offset: float = 0.0

    @property
    def frames(self) -> int:
        return len(self.frame_times)

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    def frame_at(self, t: float) -> int:
        if not self.frame_times:
            return 0
        index = bisect.bisect_right(self.frame_times, t + 1e-6) - 1
        return max(0, min(index, len(self.frame_times) - 1))

    def nearest_frame(self, t: float) -> int:
        if not self.frame_times:
            return 0
        index = bisect.bisect_left(self.frame_times, t)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(self.frame_times)]
        return min(candidates, key=lambda i: abs(self.frame_times[i] - t))

    def time_of(self, frame: int) -> float:
        if not self.frame_times:
            return frame / self.fps if self.fps else 0.0
        return self.frame_times[max(0, min(frame, len(self.frame_times) - 1))]

    def frames_between(self, a: float, b: float) -> int:
        return round((b - a) * self.fps) if self.fps else 0

    def as_dict(self) -> dict:
        return {"path": str(self.path), "width": self.width, "height": self.height, "fps": round(self.fps, 3),
                "duration": round(self.duration, 3), "frames": self.frames, "hasAudio": self.has_audio}


def probe(path: Path) -> VideoInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"{path} is not a file")
    out = _run([_need("ffprobe"), "-v", "error", "-show_entries",
                "stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate,sample_rate:format=duration,start_time",
                "-of", "json", str(path)])
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"{path.name} has no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")) or 30.0
    times, video_start = _frame_times(path)
    format_start = float((data.get("format") or {}).get("start_time") or 0.0)
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    if times:
        duration = max(duration, times[-1] + 1.0 / fps) if duration <= 0 else duration
    return VideoInfo(path=path, width=int(video.get("width") or 0), height=int(video.get("height") or 0), fps=fps,
                     duration=duration, frame_times=times, has_audio=audio is not None,
                     sample_rate=int((audio or {}).get("sample_rate") or 0),
                     seek_offset=video_start - format_start)


def frame_times(path: Path) -> list[float]:
    return _frame_times(path)[0]


def _frame_times(path: Path) -> tuple[list[float], float]:
    out = _run([_need("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                "-of", "csv=p=0", str(path)])
    values = []
    for line in out.stdout.splitlines():
        line = line.strip().rstrip(",")
        if not line or line == "N/A":
            continue
        try:
            values.append(float(line))
        except ValueError:
            continue
    values.sort()
    if not values:
        return [], 0.0
    start = values[0]
    return [round(v - start, 6) for v in values], start


def scaled_size(info: VideoInfo, width: int) -> tuple[int, int]:
    width = max(2, int(width) // 2 * 2)
    height = max(2, round(width * info.height / max(1, info.width) / 2) * 2)
    return width, height


def gray_frames(info: VideoInfo, width: int = 64) -> np.ndarray:
    w, h = scaled_size(info, width)
    out = _run([_need("ffmpeg"), "-v", "error", "-i", str(info.path), "-map", "0:v:0", "-fps_mode", "passthrough",
                "-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"], text=False)
    data = np.frombuffer(out.stdout, dtype=np.uint8)
    count = data.size // (w * h)
    return data[: count * w * h].reshape(count, h, w)


def select_frames(info: VideoInfo, frames: list[int], width: int, height: int | None = None) -> dict[int, "Image"]:
    from PIL import Image as PILImage

    wanted = sorted({max(0, min(f, max(0, info.frames - 1))) for f in frames})
    if not wanted:
        return {}
    if height is None:
        width, height = scaled_size(info, width)
    found: dict[int, PILImage.Image] = {}
    for start in range(0, len(wanted), 120):
        chunk = wanted[start: start + 120]
        expression = "+".join(f"eq(n\\,{n})" for n in chunk)
        with tempfile.TemporaryDirectory(prefix="ehx-frames-") as tmp:
            _run([_need("ffmpeg"), "-v", "error", "-i", str(info.path), "-map", "0:v:0", "-vf",
                  f"select='{expression}',scale={width}:{height}:flags=lanczos", "-fps_mode", "passthrough",
                  str(Path(tmp) / "%05d.png")])
            files = sorted(Path(tmp).glob("*.png"))
            for number, file in zip(chunk, files):
                with PILImage.open(file) as image:
                    found[number] = image.convert("RGB")
    return found


def frame_image(info: VideoInfo, frame: int, width: int | None = None) -> "Image":
    from PIL import Image as PILImage

    w, h = scaled_size(info, width or info.width)
    seek = max(0.0, info.time_of(frame) + info.seek_offset - 0.25 / max(info.fps, 1.0))
    out = _run([_need("ffmpeg"), "-v", "error", "-ss", f"{seek:.6f}", "-i", str(info.path), "-map", "0:v:0",
                "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=area,format=rgb24", "-f", "rawvideo", "-"], text=False)
    if len(out.stdout) < w * h * 3:
        raise MediaError(f"could not decode frame {frame} of {info.path.name}")
    return PILImage.frombytes("RGB", (w, h), out.stdout[: w * h * 3])


def frames_at_rate(info: VideoInfo, rate: float, width: int) -> list[tuple[int, "Image"]]:
    if info.frames == 0:
        return []
    step = max(1, round(info.fps / max(rate, 0.01)))
    picks = list(range(0, info.frames, step))
    images = select_frames(info, picks, width)
    return [(n, images[n]) for n in picks if n in images]


def audio_samples(info: VideoInfo, rate: int = 16000) -> np.ndarray:
    if not info.has_audio:
        return np.zeros(0, dtype=np.float32)
    out = _run([_need("ffmpeg"), "-v", "error", "-i", str(info.path), "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
                "-f", "f32le", "-"], text=False)
    return np.frombuffer(out.stdout, dtype=np.float32).copy()


LOUDNESS_I = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
LOUDNESS_LRA = re.compile(r"LRA:\s+(-?[\d.]+)\s+LU")
TRUE_PEAK = re.compile(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf)\s+dBFS", re.S)


def loudness(info: VideoInfo) -> dict[str, float | None]:
    if not info.has_audio:
        return {"integrated": None, "truePeak": None, "range": None}
    result = subprocess.run([_need("ffmpeg"), "-nostats", "-hide_banner", "-i", str(info.path), "-map", "0:a:0",
                             "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True)
    summary = result.stderr.rsplit("Summary:", 1)[-1]

    def number(pattern: re.Pattern) -> float | None:
        match = pattern.search(summary)
        if not match:
            return None
        return float("-inf") if match.group(1) == "-inf" else float(match.group(1))

    return {"integrated": number(LOUDNESS_I), "truePeak": number(TRUE_PEAK), "range": number(LOUDNESS_LRA)}
