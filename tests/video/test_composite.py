"""Multi-track compositing: z order, segment placement, alpha, opacity, blend modes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from PIL import Image
from eks_harness.video import ImageFile, Project, RenderSettings, Seconds, Segment, Solid, Track
from eks_harness.video.ir.animated import Animated, Keyframe

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

W, H = 160, 120
NAVY = (20, 35, 64)


def _half_red_png(path: Path) -> Path:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for x in range(W // 2):
        for y in range(H):
            img.putpixel((x, y), (230, 20, 20, 255))
    img.save(path)
    return path


def _pixel(video: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True,
    ).stdout
    i = (y * W + x) * 3
    return raw[i], raw[i + 1], raw[i + 2]


def _close(a: tuple[int, int, int], b: tuple[int, ...], tol: int = 18) -> bool:
    return all(abs(int(p) - int(q)) <= tol for p, q in zip(a, b, strict=False))


def _base() -> Track:
    return Track(name="base", segments=[
        Segment(id="bg", start=Seconds(t=0.0), out=Seconds(t=2.0), media=Solid(color=(*NAVY, 255))),
    ])


def _render(tmp_path: Path, tracks: list[Track]) -> Path:
    from eks_harness.video.render import Renderer, RenderOptions

    project = Project(fps=10, resolution=(W, H), duration=2.0,
                      render_settings=RenderSettings(pix_fmt="yuv420p"), tracks=tracks)
    out = tmp_path / "out.mp4"
    Renderer(project, RenderOptions(output=out, workspace=tmp_path)).render()
    return out


def test_overlay_segment_lands_at_its_start_and_keeps_alpha(tmp_path: Path) -> None:
    png = _half_red_png(tmp_path / "half.png")
    overlay = Track(name="ov", z=1, segments=[
        Segment(id="red", start=Seconds(t=0.5), out=Seconds(t=1.0), media=ImageFile(path=str(png))),
    ])
    out = _render(tmp_path, [overlay, _base()])

    assert _close(_pixel(out, 0.2, 20, 60), NAVY)
    assert _close(_pixel(out, 1.0, 20, 60), (230, 20, 20))
    assert _close(_pixel(out, 1.0, 140, 60), NAVY)
    assert _close(_pixel(out, 1.8, 20, 60), NAVY)


def test_track_opacity_scales_the_layer(tmp_path: Path) -> None:
    png = _half_red_png(tmp_path / "half.png")
    overlay = Track(name="ov", z=1, opacity=Animated[float](root=0.5), segments=[
        Segment(id="red", start=Seconds(t=0.0), out=Seconds(t=2.0), media=ImageFile(path=str(png))),
    ])
    out = _render(tmp_path, [_base(), overlay])
    mixed = tuple((a + b) // 2 for a, b in zip((230, 20, 20), NAVY, strict=True))
    assert _close(_pixel(out, 1.0, 20, 60), mixed, tol=24)


def test_keyframed_opacity_fades_the_layer_in(tmp_path: Path) -> None:
    png = _half_red_png(tmp_path / "half.png")
    overlay = Track(name="ov", z=1, opacity=Animated[float](root=[
        Keyframe[float](t=Seconds(t=0.0), v=0.0), Keyframe[float](t=Seconds(t=2.0), v=1.0),
    ]), segments=[
        Segment(id="red", start=Seconds(t=0.0), out=Seconds(t=2.0), media=ImageFile(path=str(png))),
    ])
    out = _render(tmp_path, [_base(), overlay])
    early, late = _pixel(out, 0.1, 20, 60), _pixel(out, 1.9, 20, 60)
    assert early[0] < late[0] - 120


def test_screen_blend_brightens_only_where_the_layer_is_opaque(tmp_path: Path) -> None:
    png = _half_red_png(tmp_path / "half.png")
    overlay = Track(name="glow", z=1, blend="screen", segments=[
        Segment(id="red", start=Seconds(t=0.0), out=Seconds(t=2.0), media=ImageFile(path=str(png))),
    ])
    out = _render(tmp_path, [_base(), overlay])
    lit = _pixel(out, 1.0, 20, 60)
    screened = tuple(255 - (255 - a) * (255 - b) // 255 for a, b in zip((230, 20, 20), NAVY, strict=True))
    assert _close(lit, screened, tol=24)
    assert _close(_pixel(out, 1.0, 140, 60), NAVY)


def test_lowest_z_is_the_base_track_regardless_of_order(tmp_path: Path) -> None:
    from eks_harness.video.render.composite import order_tracks

    a, b, c = Track(name="a", z=2), Track(name="b", z=0), Track(name="c", z=2)
    project = Project(fps=10, resolution=(W, H), duration=1.0, tracks=[a, b, c])
    assert [t.name for t in order_tracks(project)] == ["b", "a", "c"]


def test_registered_plugin_effect_validates_inside_a_segment() -> None:
    from eks_harness.video.ir.effects import EffectIR
    from eks_harness.video.plugins.base import Effect as EffectPlugin
    from eks_harness.video.plugins.registry import register_effect

    class CompositeTestIR(EffectIR):
        kind: Literal["composite_test_fx"] = "composite_test_fx"
        compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})

    class CompositeTestPlugin(EffectPlugin):
        model = CompositeTestIR
        name = "composite_test_fx"
        compile_targets = frozenset({"frame_pipeline"})

        def open(self, ir, ctx):  # type: ignore[no-untyped-def]
            return None

    register_effect(CompositeTestPlugin())
    project = Project.model_validate({
        "fps": 10, "resolution": [W, H], "duration": 1.0,
        "tracks": [{"name": "m", "segments": [{
            "id": "s", "start": {"kind": "seconds", "t": 0}, "out": {"kind": "seconds", "t": 1},
            "media": {"kind": "solid", "color": [0, 0, 0, 255]},
            "effects": [{"kind": "composite_test_fx"}],
        }]}],
    })
    assert type(project.tracks[0].segments[0].effects[0]).__name__ == "CompositeTestIR"


def test_prores_4444_alpha_video_overlay(tmp_path: Path) -> None:
    from eks_harness.video import VideoFile

    png = _half_red_png(tmp_path / "half.png")
    mov = tmp_path / "alpha.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "10", "-t", "1", "-i", str(png),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(mov)],
        check=True,
    )
    overlay = Track(name="ov", z=3, segments=[
        Segment(id="v", start=Seconds(t=1.0), out=Seconds(t=1.0), media=VideoFile(path=str(mov))),
    ])
    out = _render(tmp_path, [_base(), overlay])
    assert _close(_pixel(out, 0.5, 20, 60), NAVY)
    assert _close(_pixel(out, 1.5, 20, 60), (230, 20, 20))
    assert _close(_pixel(out, 1.5, 140, 60), NAVY)
