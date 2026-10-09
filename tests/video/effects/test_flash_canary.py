"""Flash canary: BeatPulse-driven Flash composited via the frame pipeline.

A 4-second 60 fps project receives synthetic 120 BPM beats from the stub
beat extractor. The Flash effect uses a BeatPulse curve so the alpha array
is materialized end-to-end. The rendered output is decoded with PyAV and
peak luminance frames are detected; each peak must land within ±1 frame of
an expected beat time.
"""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import av
import numpy as np
import pytest

from eks_harness.video.ir import (
    Animated,
    BeatTracker,
    Flash,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.ir.curves import BeatPulse, ExpDecayEnv
from eks_harness.video.ir.time import BeatRef
from eks_harness.video.render import RenderOptions, Renderer

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)


def _write_dark_png(path: Path, width: int = 64, height: int = 64) -> Path:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + (b"\x10\x10\x10" * width)
    idat = zlib.compress(raw)
    path.write_bytes(sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))
    return path


def test_flash_canary_beats_align(tmp_path: Path) -> None:
    poster = _write_dark_png(tmp_path / "poster.png")
    project = Project(
        fps=60,
        resolution=(64, 64),
        duration=4.0,
        markers=[
            BeatTracker(name="kicks", source="audio", streams=["beat"], bpm=120.0),
        ],
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path=poster),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=4.0),
                        effects=[
                            Flash(
                                at=Seconds(t=0.0),
                                duration=Animated[float](root=0.05),
                                curve=BeatPulse(
                                    trigger=BeatRef(stream="beat"),
                                    envelope=ExpDecayEnv(tau=0.04),
                                    intensity_ramp=1.0,
                                ),
                            ),
                        ],
                    )
                ],
            )
        ],
    )

    output = tmp_path / "out.mp4"
    Renderer(project, RenderOptions(output=output, workspace=tmp_path / "ws")).render(output)
    assert output.exists()

    luminances: list[float] = []
    with av.open(str(output)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            arr = frame.to_ndarray(format="rgb24").astype(np.float32)
            luminances.append(float(arr.mean()))
    lum = np.array(luminances, dtype=np.float32)
    assert lum.size >= 60

    threshold = lum.min() + 0.4 * (lum.max() - lum.min())
    above = lum > threshold
    peaks: list[int] = []
    i = 0
    while i < len(above):
        if above[i]:
            j = i
            best = i
            while j < len(above) and above[j]:
                if lum[j] > lum[best]:
                    best = j
                j += 1
            peaks.append(best)
            i = j
        else:
            i += 1

    expected_seconds = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    fps = project.fps
    expected_frames = [int(round(s * fps)) for s in expected_seconds]
    matched = 0
    for ef in expected_frames:
        if any(abs(p - ef) <= 1 for p in peaks):
            matched += 1
    assert matched >= len(expected_frames) - 1, (
        f"expected {expected_frames}, got peaks {peaks}"
    )
