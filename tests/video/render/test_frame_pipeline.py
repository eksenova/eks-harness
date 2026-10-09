"""Integration test for FramePipeline using a synthetic video input."""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import av
import pytest

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import Animated, Brightness, ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.plugins.builtin.effects.brightness import BrightnessPlugin
from eks_harness.video.render.context import RenderContext, RenderOptions
from eks_harness.video.render.frame_pipeline import FramePipeline, FramePipelineSpec

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)


def _write_solid_png(path: Path, width: int = 64, height: int = 64) -> Path:
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
        raw += b"\x00" + (b"\x80\x80\x80" * width)
    idat = zlib.compress(raw)
    path.write_bytes(sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))
    return path


def test_frame_pipeline_brightens_through_processor(tmp_path: Path) -> None:
    poster = _write_solid_png(tmp_path / "poster.png")
    project = Project(
        fps=30,
        resolution=(64, 64),
        duration=1.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="s",
                        start=Seconds(t=0.0),
                        media=ImageFile(path=poster),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=1.0),
                    )
                ],
            )
        ],
    )
    ctx = RenderContext(
        project=project,
        options=RenderOptions(output=tmp_path / "out.mp4"),
        markers=MarkerSet(),
        workspace=tmp_path,
        cache_dir=tmp_path / "cache",
    )
    ctx.extra["segment_duration_seconds"] = 1.0

    output = tmp_path / "seg.mp4"
    spec = FramePipelineSpec(
        input_path=poster,
        output_path=output,
        width=64,
        height=64,
        fps=30.0,
        duration=1.0,
        pix_fmt="bgr24",
        graph_prefix_filter="scale=64:64",
        input_args=["-loop", "1"],
    )
    processor = BrightnessPlugin().open(Brightness(amount=Animated[float](root=0.3)), ctx)
    result = FramePipeline(spec, [processor], ctx=ctx).run()

    assert result.frames_written > 0
    assert output.exists()

    with av.open(str(output)) as container:
        stream = container.streams.video[0]
        means: list[float] = []
        for frame in container.decode(stream):
            arr = frame.to_ndarray(format="rgb24")
            means.append(float(arr.mean()))
            if len(means) >= 3:
                break
    assert means
    assert max(means) > 130
