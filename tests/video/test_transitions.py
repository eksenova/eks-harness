"""End-to-end render tests for video transitions.

These tests build two-segment :class:`Solid` projects, run the full
:class:`Renderer` pipeline, and inspect the produced mp4 with ffprobe /
ffmpeg to assert that the transition IR is actually honoured. Skipped
when ffmpeg / ffprobe are not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eks_harness.video.ir import Project, Seconds, Segment, Solid, Track
from eks_harness.video.ir.transitions import (
    BeatFlash,
    BeatGlitch,
    BlurThrough,
    Crossfade,
    DipToBlack,
    Dissolve,
    FadeGrays,
    FilmBurn,
    Iris,
    LightLeak,
    LumaWipe,
    Pixelize,
    Radial,
    RGBShiftWipe,
    WhipPan,
    ZoomTransition,
)
from eks_harness.video.render import RenderOptions, Renderer
from eks_harness.video.render.transitions import _build_filter_complex

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe binaries not on PATH; skipping transition render tests",
)


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def _extract_frame_rgb(video: Path, timestamp: float, tmp_path: Path) -> tuple[int, int, int]:
    """Decode a single 1x1 RGB pixel at ``timestamp`` and return its (R, G, B)."""

    raw = tmp_path / f"frame_{timestamp:.3f}.rgb"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-ss", f"{timestamp:.6f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", "scale=1:1",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )
    data = raw.read_bytes()
    assert len(data) == 3, f"expected 3 bytes of rgb24, got {len(data)}"
    return data[0], data[1], data[2]


def _two_segment_project(
    color_a: tuple[int, int, int, int],
    color_b: tuple[int, int, int, int],
    *,
    seg_duration: float,
    transition_out=None,
    transition_in=None,
    fps: int = 30,
) -> Project:
    seg_a = Segment(
        id="a",
        start=Seconds(t=0.0),
        media=Solid(color=color_a),
        in_=Seconds(t=0.0),
        out=Seconds(t=seg_duration),
    )
    if transition_out is not None:
        seg_a = seg_a.model_copy(update={"transition_out": transition_out})

    seg_b = Segment(
        id="b",
        start=Seconds(t=seg_duration),
        media=Solid(color=color_b),
        in_=Seconds(t=0.0),
        out=Seconds(t=seg_duration),
    )
    if transition_in is not None:
        seg_b = seg_b.model_copy(update={"transition_in": transition_in})

    return Project(
        fps=fps,
        resolution=(64, 64),
        duration=seg_duration * 2,
        tracks=[Track(name="main", segments=[seg_a, seg_b])],
    )


def _render(project: Project, tmp_path: Path) -> Path:
    output = tmp_path / "out.mp4"
    renderer = Renderer(project, RenderOptions(output=output, workspace=tmp_path / "ws"))
    return renderer.render(output)


_RED: tuple[int, int, int, int] = (255, 0, 0, 255)
_BLUE: tuple[int, int, int, int] = (0, 0, 255, 255)


def test_crossfade_rendered_duration_is_shortened(tmp_path: Path) -> None:
    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=Crossfade(duration=transition_duration),
    )
    final = _render(project, tmp_path)

    duration = _probe_duration(final)
    expected = 2.0 - transition_duration
    frame = 1.0 / project.fps
    assert abs(duration - expected) <= 2 * frame, (
        f"expected ~{expected:.3f}s, got {duration:.3f}s"
    )


def test_cut_rendered_duration_is_sum(tmp_path: Path) -> None:
    project = _two_segment_project(_RED, _BLUE, seg_duration=1.0)
    final = _render(project, tmp_path)

    duration = _probe_duration(final)
    frame = 1.0 / project.fps
    assert abs(duration - 2.0) <= 2 * frame, f"expected ~2.0s, got {duration:.3f}s"


def test_crossfade_midpoint_is_blended(tmp_path: Path) -> None:
    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=Crossfade(duration=transition_duration),
    )
    final = _render(project, tmp_path)

    midpoint = 1.0 - transition_duration / 2.0
    r, g, b = _extract_frame_rgb(final, midpoint, tmp_path)
    assert 78 <= r <= 178, f"R={r} out of blended range"
    assert 78 <= b <= 178, f"B={b} out of blended range"


def test_dip_to_black_midpoint_is_dark(tmp_path: Path) -> None:
    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=DipToBlack(duration=transition_duration),
    )
    final = _render(project, tmp_path)

    midpoint = 1.0 - transition_duration / 2.0
    r, g, b = _extract_frame_rgb(final, midpoint, tmp_path)
    # fadeblack's brightness curve dips to near-black around the centre but
    # is not perfectly flat at 0; any frame we sample inside the transition
    # window should be drastically darker than the source colours (255).
    assert max(r, g, b) < 100, (
        f"expected dim frame at dip midpoint, got ({r}, {g}, {b})"
    )


# ---------------------------------------------------------------------------
# IR round-trip
# ---------------------------------------------------------------------------

_ROUND_TRIP_CASES = [
    ZoomTransition(duration=0.5),
    Dissolve(duration=0.5),
    BlurThrough(duration=0.5),
    Iris(duration=0.5, mode="open"),
    Iris(duration=0.5, mode="close"),
    Radial(duration=0.5),
    Pixelize(duration=0.5),
    FadeGrays(duration=0.5),
    LumaWipe(duration=0.5, direction="up"),
    LumaWipe(duration=0.5, direction="left"),
    RGBShiftWipe(duration=0.5, direction="right", intensity=0.6),
    WhipPan(duration=0.5, direction="down", intensity=0.4),
    LightLeak(duration=0.5, intensity=0.7),
    FilmBurn(duration=0.5, intensity=0.8),
    BeatFlash(duration=0.5, stream="kick", intensity=0.7),
    BeatGlitch(duration=0.5, stream="snare", intensity=0.5),
]


@pytest.mark.parametrize("transition", _ROUND_TRIP_CASES, ids=lambda t: type(t).__name__)
def test_transition_ir_round_trip(transition) -> None:
    cls = type(transition)
    restored = cls.model_validate_json(transition.model_dump_json())
    assert restored == transition


@pytest.mark.parametrize("transition", _ROUND_TRIP_CASES, ids=lambda t: type(t).__name__)
def test_transition_round_trips_through_project(transition) -> None:
    """A transition survives a full Project JSON round-trip on both sides."""

    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=transition,
        transition_in=transition,
    )
    restored = Project.model_validate_json(project.model_dump_json())
    track = restored.tracks[0]
    assert track.segments[0].transition_out == transition
    assert track.segments[1].transition_in == transition


# ---------------------------------------------------------------------------
# _build_filter_complex: preset and custom-graph shapes
# ---------------------------------------------------------------------------


def test_filter_complex_preset_transitions_map_to_xfade() -> None:
    cases = [
        (ZoomTransition(duration=0.5), "xfade=transition=zoomin"),
        (Dissolve(duration=0.5), "xfade=transition=dissolve"),
        (BlurThrough(duration=0.5), "xfade=transition=hblur"),
        (Iris(duration=0.5, mode="open"), "xfade=transition=circleopen"),
        (Iris(duration=0.5, mode="close"), "xfade=transition=circleclose"),
        (Radial(duration=0.5), "xfade=transition=radial"),
        (Pixelize(duration=0.5), "xfade=transition=pixelize"),
        (FadeGrays(duration=0.5), "xfade=transition=fadegrays"),
        (LumaWipe(duration=0.5, direction="up"), "xfade=transition=smoothup"),
        (LumaWipe(duration=0.5, direction="right"), "xfade=transition=smoothright"),
    ]
    for transition, expected in cases:
        graph = _build_filter_complex(transition, 0.5, 1.5)
        assert expected in graph, f"{type(transition).__name__}: {graph}"
        assert "duration=0.500000:offset=1.500000" in graph


def test_filter_complex_rgb_shift_wipe_graph() -> None:
    graph = _build_filter_complex(
        RGBShiftWipe(duration=0.5, direction="left", intensity=0.5), 0.5, 1.5
    )
    assert "xfade=transition=wipeleft" in graph
    # intensity 0.5 -> shift = round(0.5 * 20) = 10
    assert "rgbashift=rh=10:bh=-10" in graph
    assert "enable='between(t,1.500000,2.000000)'" in graph


def test_filter_complex_whip_pan_axis_blur() -> None:
    horizontal = _build_filter_complex(
        WhipPan(duration=0.5, direction="right", intensity=1.0), 0.5, 1.5
    )
    assert "xfade=transition=slideright" in horizontal
    # intensity 1.0 -> extent = round(1.0 * 40) = 40, horizontal axis
    assert "avgblur=sizeX=40:sizeY=1" in horizontal

    vertical = _build_filter_complex(
        WhipPan(duration=0.5, direction="up", intensity=1.0), 0.5, 1.5
    )
    assert "avgblur=sizeX=1:sizeY=40" in vertical


def test_filter_complex_light_leak_brightness_pulse() -> None:
    graph = _build_filter_complex(LightLeak(duration=0.5, intensity=0.7), 0.5, 1.5)
    assert "xfade=transition=fade" in graph
    assert "eq=brightness=" in graph
    assert "eval=frame" in graph
    # peak brightness scales with intensity
    assert "0.700000*" in graph


def test_filter_complex_film_burn_chain() -> None:
    graph = _build_filter_complex(FilmBurn(duration=0.5, intensity=1.0), 0.5, 1.5)
    assert "xfade=transition=fade" in graph
    assert "eq=brightness=" in graph
    assert "colorbalance=rs=0.3:rm=0.2:bs=-0.25" in graph
    # intensity 1.0 -> shift = round(1.0 * 10) = 10
    assert "rgbashift=rh=10:bh=-10" in graph


def test_filter_complex_beat_flash_pulses_per_beat() -> None:
    graph = _build_filter_complex(
        BeatFlash(duration=0.5, intensity=0.7), 0.5, 1.5, [0.1, 0.3]
    )
    assert "xfade=transition=fade" in graph
    assert "eval=frame" in graph
    # two beats at window-local 0.1, 0.3 -> absolute 1.6, 1.8
    assert "t-1.600000" in graph
    assert "t-1.800000" in graph


def test_filter_complex_beat_flash_fallback_single_mid_pulse() -> None:
    # No beats threaded -> single pulse at mid-window (offset + duration/2).
    graph = _build_filter_complex(BeatFlash(duration=0.5, intensity=0.7), 0.5, 1.5, None)
    assert "t-1.750000" in graph
    # exactly one Gaussian term -> exactly one exp(
    assert graph.count("exp(") == 1


def test_filter_complex_beat_glitch_combined_enable() -> None:
    graph = _build_filter_complex(
        BeatGlitch(duration=0.5, intensity=1.0), 0.5, 1.5, [0.1, 0.3]
    )
    assert "xfade=transition=pixelize" in graph
    assert "rgbashift=rh=20:bh=-20" in graph
    # single combined enable expression OR-ing the two beat windows
    assert "between(t,1.560000,1.640000)+between(t,1.760000,1.840000)" in graph


def test_filter_complex_beat_glitch_fallback_single_window() -> None:
    graph = _build_filter_complex(BeatGlitch(duration=0.5, intensity=1.0), 0.5, 1.5, [])
    # empty list -> mid-window fallback at 1.75, one between() term
    assert graph.count("between(") == 1
    assert "between(t,1.710000,1.790000)" in graph


# ---------------------------------------------------------------------------
# End-to-end renders: preset, custom-graph, and beat-reactive
# ---------------------------------------------------------------------------


def _render_sw(project: Project, tmp_path: Path, monkeypatch) -> Path:
    """Render forcing software libx264 (EKS_HARNESS_HWACCEL=0) for determinism."""

    monkeypatch.setenv("EKS_HARNESS_HWACCEL", "0")
    output = tmp_path / "out.mp4"
    renderer = Renderer(project, RenderOptions(output=output, workspace=tmp_path / "ws"))
    return renderer.render(output)


def test_zoom_transition_rendered_duration_is_shortened(tmp_path: Path, monkeypatch) -> None:
    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=ZoomTransition(duration=transition_duration),
    )
    final = _render_sw(project, tmp_path, monkeypatch)

    duration = _probe_duration(final)
    expected = 2.0 - transition_duration
    frame = 1.0 / project.fps
    assert abs(duration - expected) <= 2 * frame, (
        f"expected ~{expected:.3f}s, got {duration:.3f}s"
    )


def test_rgb_shift_wipe_rendered_duration_is_shortened(tmp_path: Path, monkeypatch) -> None:
    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=RGBShiftWipe(duration=transition_duration, intensity=0.7),
    )
    final = _render_sw(project, tmp_path, monkeypatch)

    duration = _probe_duration(final)
    expected = 2.0 - transition_duration
    frame = 1.0 / project.fps
    assert abs(duration - expected) <= 2 * frame, (
        f"expected ~{expected:.3f}s, got {duration:.3f}s"
    )


def test_beat_flash_with_fake_beats_renders(tmp_path: Path, monkeypatch) -> None:
    """BeatFlash with beats threaded by a BeatTracker stream renders cleanly.

    The stub beat extractor lays an even grid; the orchestrator maps the
    grid points that fall inside the transition window to window-local
    seconds and threads them into the boundary's filter graph.
    """

    from eks_harness.video.ir.markers import BeatTracker

    transition_duration = 0.4
    project = _two_segment_project(
        _RED, _BLUE,
        seg_duration=1.0,
        transition_out=BeatFlash(duration=transition_duration, stream="kick", intensity=0.8),
    )
    # 240 BPM -> a beat every 0.25s, so several land inside the 0.4s window.
    project = project.model_copy(
        update={
            "markers": [
                BeatTracker(
                    name="beats",
                    source="audio_tracks[0]",
                    streams=["kick"],
                    bpm=240.0,
                )
            ]
        }
    )
    final = _render_sw(project, tmp_path, monkeypatch)

    duration = _probe_duration(final)
    expected = 2.0 - transition_duration
    frame = 1.0 / project.fps
    assert abs(duration - expected) <= 2 * frame, (
        f"expected ~{expected:.3f}s, got {duration:.3f}s"
    )
