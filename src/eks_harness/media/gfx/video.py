from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import IO, Any

import numpy as np

from eks_harness.media.gfx.job import DECODE_THREADS


class VideoDecodeError(RuntimeError):
    pass


class VideoStream:
    def __init__(self, path: Path, *, width: int, height: int, start: float, length: float | None, speed: float,
                 fps: Fraction, threads: int = DECODE_THREADS) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise VideoDecodeError("ffmpeg is not installed on this machine; install ffmpeg")
        self.width = width
        self.height = height
        self.size = width * height * 4
        self.index = -1
        self.last: np.ndarray[Any, Any] | None = None
        self.ended = False
        span = ["-t", f"{length:.6f}"] if length is not None else []
        rate = f"{fps.numerator}/{fps.denominator}"
        argv = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", str(threads),
                "-flags", "+bitexact", "-ss", f"{max(start, 0.0):.6f}", *span, "-i", str(path), "-an", "-sn", "-dn",
                "-vf", f"setpts=(PTS-STARTPTS)/{speed!r},fps={rate}", "-threads", str(threads),
                "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
        self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _read(self) -> np.ndarray[Any, Any] | None:
        stream: IO[bytes] | None = self.process.stdout
        if stream is None:
            return None
        chunks: list[bytes] = []
        remaining = self.size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            return None
        return np.frombuffer(b"".join(chunks), dtype=np.uint8).reshape(self.height, self.width, 4)

    def frame(self, index: int) -> np.ndarray[Any, Any] | None:
        while self.index < index and not self.ended:
            found = self._read()
            if found is None:
                self.ended = True
                if self.last is None:
                    self._check()
                break
            self.last = found
            self.index += 1
        return self.last

    def _check(self) -> None:
        code = self.process.wait()
        if code != 0:
            stderr = self.process.stderr.read().decode("utf-8", "replace")[-800:] if self.process.stderr else ""
            raise VideoDecodeError(f"ffmpeg could not decode the video layer ({code}): {stderr}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


__all__ = ["VideoDecodeError", "VideoStream"]
