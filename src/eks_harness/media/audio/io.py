from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, cast

import numpy as np

from eks_harness.engine.errors import NodeFailure
from eks_harness.media.audio.loudness import Signal
from eks_harness.media.files import ffmpeg, ffprobe, temp_file

FULL_SCALE = 8388608.0
SAMPLE_BYTES = 3


def has_audio(path: Path) -> bool:
    streams = cast(list[dict[str, Any]], ffprobe(path).get("streams") or [])
    return any(stream.get("codec_type") == "audio" for stream in streams)


def decode(path: Path, *, rate: int, channels: int, directory: Path, what: str, threads: int = 1) -> Signal:
    if not has_audio(path):
        raise NodeFailure("args_invalid", f"{what} ({path.name}) has no audio stream; give an audio file or a video "
                                          "with sound", detail={"file": path.name})
    raw = temp_file(directory, ".f64")
    try:
        ffmpeg(["-i", str(path)], ["-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(channels), "-ar", str(rate),
                                   "-c:a", "pcm_f64le", "-f", "f64le", str(raw)], threads=threads)
        data = np.fromfile(raw, dtype="<f8")
    finally:
        raw.unlink(missing_ok=True)
    return np.ascontiguousarray(data.reshape(-1, channels), dtype=np.float64)


def write_wav24(path: Path, samples: Signal, rate: int) -> None:
    scaled = np.clip(np.round(samples * FULL_SCALE), -FULL_SCALE, FULL_SCALE - 1.0).astype("<i4")
    frames = scaled.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :SAMPLE_BYTES].tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(samples.shape[1])
        out.setsampwidth(SAMPLE_BYTES)
        out.setframerate(rate)
        out.writeframes(frames)


__all__ = ["FULL_SCALE", "decode", "has_audio", "write_wav24"]
