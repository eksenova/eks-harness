"""Tests for the HTMLOverlay media kind.

The render test is guarded by ``pytest.importorskip("playwright")``;
when Playwright (and its bundled Chromium) are not installed the heavy
end-to-end render is skipped while the IR round-trip still runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from eks_harness.video import (
    HTMLOverlay,
    MarkerAnimation,
    Project,
    RenderSettings,
    Seconds,
    Segment,
    Solid,
    Track,
)
from eks_harness.video.ir.media import MediaSource


_MINIMAL_HTML = """<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<style>
  body { margin: 0; }
  .title {
    position: absolute;
    inset: 40% 0 0 0;
    text-align: center;
    color: rgb(0, 200, 255);
    font: bold 64px sans-serif;
    opacity: 0;
    animation: fadein 1s linear 0s 1 forwards;
  }
  @keyframes fadein {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
  @keyframes pop {
    0%   { transform: scale(1); }
    50%  { transform: scale(1.5); }
    100% { transform: scale(1); }
  }
</style>
</head>
<body>
  <div class=\"title\">HELLO</div>
</body>
</html>
"""


def test_html_overlay_ir_roundtrip(tmp_path: Path) -> None:
    """The ``HTMLOverlay`` IR survives a round trip through the
    discriminated ``MediaSource`` union."""

    template = tmp_path / "overlay.html"
    template.write_text(_MINIMAL_HTML, encoding="utf-8")

    overlay = HTMLOverlay(
        template=str(template),
        style_vars={"accent": "#ff00aa", "size": "64px"},
        viewport=(720, 1280),
        transparent=True,
        marker_animations=[
            MarkerAnimation(
                stream="kick",
                selector=".title",
                animation_name="pop",
                duration=0.2,
                offset_ms=0.0,
            )
        ],
    )

    adapter: TypeAdapter[MediaSource] = TypeAdapter(MediaSource)
    payload = json.loads(overlay.model_dump_json())
    restored = adapter.validate_python(payload)

    assert isinstance(restored, HTMLOverlay)
    assert restored.kind == "html_overlay"
    assert restored.transparent is True
    assert restored.viewport == (720, 1280)
    assert restored.style_vars == {"accent": "#ff00aa", "size": "64px"}
    assert len(restored.marker_animations) == 1
    assert restored.marker_animations[0].stream == "kick"
    assert restored.marker_animations[0].animation_name == "pop"


def test_marker_animation_rejects_non_positive_duration() -> None:
    """``MarkerAnimation.duration`` is constrained ``> 0``."""

    with pytest.raises(Exception):
        MarkerAnimation(
            stream="kick",
            selector=".x",
            animation_name="pop",
            duration=0.0,
        )


def test_html_overlay_compose_style_block(tmp_path: Path) -> None:
    """The pure-Python compose helpers produce the expected CSS without
    needing Playwright on PATH."""

    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.render.html_renderer import (
        _compose_marker_divs,
        _compose_style_block,
    )

    media = HTMLOverlay(
        template=str(tmp_path / "x.html"),  # not read by the compose helpers
        style_vars={"accent": "#ff00aa"},
        transparent=True,
        marker_animations=[
            MarkerAnimation(
                stream="kick",
                selector=".title",
                animation_name="pop",
                duration=0.2,
            )
        ],
    )

    class _Ctx:
        markers = MarkerSet(streams={"kick": [0.25, 0.75, 2.5]})

    style = _compose_style_block(media, _Ctx(), seg_start=0.0, seg_end=1.0)
    assert "--accent: #ff00aa" in style
    assert "background:transparent" in style
    # First two triggers fall inside [0, 1]; the third is outside.
    assert "marker-trigger-kick-0" in style
    assert "marker-trigger-kick-1" in style
    assert "marker-trigger-kick-2" not in style
    # Delays are seg-relative.
    assert "0.25s" in style
    assert "0.75s" in style

    divs = _compose_marker_divs(media, _Ctx(), seg_start=0.0, seg_end=1.0)
    assert 'class="marker-trigger-kick-0"' in divs
    assert 'class="marker-trigger-kick-1"' in divs
    assert "marker-trigger-kick-2" not in divs


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe binaries not on PATH",
)
def test_html_overlay_renders_to_mp4(tmp_path: Path) -> None:
    """End-to-end: a one-segment ``HTMLOverlay`` project renders to a
    valid mp4 whose duration matches the segment.

    Skipped if Playwright (or its Chromium runtime) isn't installed.
    """

    pytest.importorskip(
        "playwright",
        reason="HTMLOverlay requires `pip install eks-harness[html]`",
    )
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"Playwright Chromium not available (run `playwright install chromium`): {exc}"
        )

    from eks_harness.video.render import RenderOptions, Renderer

    template = tmp_path / "overlay.html"
    template.write_text(_MINIMAL_HTML, encoding="utf-8")

    project = Project(
        fps=24,
        resolution=(320, 240),
        duration=1.0,
        render_settings=RenderSettings(pix_fmt="yuv420p"),
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="s0",
                        start=Seconds(t=0.0),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=1.0),
                        media=HTMLOverlay(
                            template=str(template),
                            transparent=False,
                            viewport=(320, 240),
                        ),
                    )
                ],
            )
        ],
    )

    out = tmp_path / "out.mp4"
    Renderer(project, RenderOptions(output=out, workspace=tmp_path)).render()

    assert out.exists() and out.stat().st_size > 0

    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(proc.stdout.strip())
    # Allow a generous ±0.2s tolerance for codec rounding.
    assert 0.8 <= duration <= 1.3, f"unexpected duration: {duration}"


def test_solid_project_still_renders_after_html_overlay_import() -> None:
    """Importing the orchestrator with the new HTMLOverlay branch must
    not regress the existing media-kind dispatch."""

    project = Project(
        fps=24,
        resolution=(64, 64),
        duration=0.1,
        tracks=[
            Track(
                name="t",
                segments=[
                    Segment(
                        id="s",
                        start=Seconds(t=0.0),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=0.1),
                        media=Solid(color=(0, 0, 0, 255)),
                    )
                ],
            )
        ],
    )
    assert project.tracks[0].segments[0].media.kind == "solid"
