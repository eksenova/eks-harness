"""Integration: render a 2 second clip from a tiny PNG via the graph-only path.

Skipped when ffmpeg is not on PATH. This exercises the full pipeline
end-to-end: marker stub, animated resolve, segment cache, mux step.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from eks_harness.video import (
    Animated,
    Brightness,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.render import RenderOptions, Renderer

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg binary not on PATH; skipping integration test",
)


def _write_solid_png(path: Path, width: int = 64, height: int = 64) -> Path:
    """Write a minimal valid solid-red PNG without depending on Pillow."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + (b"\xff\x00\x00" * width)
    idat = zlib.compress(raw)
    png = signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


def test_render_two_second_clip(tmp_path: Path) -> None:
    poster = _write_solid_png(tmp_path / "poster.png")
    project = Project(
        fps=30,
        resolution=(64, 64),
        duration=2.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="hero",
                        start=Seconds(t=0.0),
                        media=ImageFile(path=poster),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=2.0),
                        effects=[Brightness(amount=Animated[float](root=0.2))],
                    )
                ],
            )
        ],
    )

    output = tmp_path / "out.mp4"
    renderer = Renderer(project, RenderOptions(output=output, workspace=tmp_path / "ws"))
    final = renderer.render(output)

    assert final.exists()
    assert final.stat().st_size > 1024

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(final)],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        duration = float(probe.stdout.strip())
        assert 1.5 < duration < 2.5
