from __future__ import annotations

import math
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import IO

import numpy as np
from numpy.typing import NDArray

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.types import MediaInfo
from eks_harness.media.files import tool

SWS_FLAGS = "lanczos+accurate_rnd+full_chroma_int+bitexact"
DEFAULT_IMAGE_RATE = Fraction(1)
Image = NDArray[np.uint8]


def fit_box(fit: str, source: tuple[int, int], frame: tuple[int, int]) -> tuple[tuple[int, int], str]:
    width, height = source
    frame_w, frame_h = frame
    if fit == "fill":
        return (frame_w, frame_h), f"scale={frame_w}:{frame_h}:flags={SWS_FLAGS}"
    factor = (max if fit == "cover" else min)(frame_w / width, frame_h / height)
    scaled_w = max(1, round(width * factor))
    scaled_h = max(1, round(height * factor))
    chain = f"scale={scaled_w}:{scaled_h}:flags={SWS_FLAGS}"
    if fit == "cover":
        crop_w, crop_h = min(scaled_w, frame_w), min(scaled_h, frame_h)
        chain += f",crop={crop_w}:{crop_h}:{(scaled_w - crop_w) // 2}:{(scaled_h - crop_h) // 2}"
        return (crop_w, crop_h), chain
    return (scaled_w, scaled_h), chain


class ClipSource:
    def __init__(self, path: Path, info: MediaInfo, *, fit: str, frame: tuple[int, int], clip: str) -> None:
        if info.width is None or info.height is None:
            raise NodeFailure("args_invalid", f"clip {clip}: {info.uri} has no picture to show")
        self.path = path
        self.info = info
        self.clip = clip
        self.box, self.filters = fit_box(fit, (info.width, info.height), frame)
        self.still = info.kind == "image"
        self.rate = Fraction(info.fps) if info.fps is not None and not self.still else DEFAULT_IMAGE_RATE
        self._frame_bytes = self.box[0] * self.box[1] * 4
        self._process: subprocess.Popen[bytes] | None = None
        self._origin = 0.0
        self._position = -1
        self._last: Image | None = None
        self._ended = False

    def _start(self, seconds: float) -> None:
        self.close()
        index = max(0, math.floor(seconds * float(self.rate) + 1e-6))
        self._origin = index / float(self.rate)
        argv = [tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1"]
        if not self.still and self._origin > 0:
            argv += ["-ss", f"{self._origin:.6f}"]
        argv += ["-i", str(self.path), "-an", "-sn", "-threads", "1",
                 "-vf", (f"fps={self.rate}," if not self.still else "") + self.filters,
                 "-sws_flags", SWS_FLAGS, "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"]
        self._process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        self._position = -1
        self._last = None
        self._ended = False

    def _read(self) -> Image | None:
        process = self._process
        if process is None or process.stdout is None or self._ended:
            return None
        data = _read_exact(process.stdout, self._frame_bytes)
        if data is None:
            self._ended = True
            return None
        return np.frombuffer(data, dtype=np.uint8).reshape(self.box[1], self.box[0], 4)

    def frame(self, seconds: float) -> Image:
        if self.still:
            if self._last is None:
                self._start(0.0)
                found = self._read()
                if found is None:
                    raise NodeFailure("content_unavailable", f"clip {self.clip}: {self.info.uri} decoded no image")
                self._last = found
                self.close_process()
            return self._last
        wanted = max(0, math.floor((seconds - self._origin) * float(self.rate) + 1e-6))
        if self._process is None or wanted < self._position:
            self._start(seconds)
            wanted = max(0, math.floor((seconds - self._origin) * float(self.rate) + 1e-6))
        while self._position < wanted:
            found = self._read()
            if found is None:
                break
            self._last = found
            self._position += 1
        if self._last is None:
            raise NodeFailure("content_unavailable",
                              f"clip {self.clip}: {self.info.uri} has no frame at {seconds:.3f}s")
        return self._last

    def close_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdout is not None:
            process.stdout.close()
        process.kill()
        process.wait()

    def close(self) -> None:
        self.close_process()


def _read_exact(stream: IO[bytes], size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = ["SWS_FLAGS", "ClipSource", "fit_box"]
