"""Tests for orchestrator's effect partitioning logic."""

from __future__ import annotations

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import (
    Animated,
    Brightness,
    Fade,
    Flash,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Track,
)
from eks_harness.video.ir.curves import BeatPulse, ExpDecayEnv
from eks_harness.video.ir.markers import BeatTracker
from eks_harness.video.ir.time import BeatRef
from eks_harness.video.plugins.registry import load_builtins
from eks_harness.video.render.context import RenderContext, RenderOptions
from eks_harness.video.render.orchestrator import Renderer


def _make_project(effects: list) -> Project:
    return Project(
        fps=30,
        resolution=(64, 64),
        duration=1.0,
        markers=[BeatTracker(name="kicks", source="audio", streams=["beat"], bpm=120.0)],
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="s",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="x.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=1.0),
                        effects=effects,
                    )
                ],
            )
        ],
    )


def _make_ctx(project: Project, tmp_path) -> RenderContext:
    load_builtins()
    return RenderContext(
        project=project,
        options=RenderOptions(output=tmp_path / "out.mp4"),
        markers=MarkerSet(streams={"beat": [0.0, 0.5]}),
        workspace=tmp_path,
        cache_dir=tmp_path / "cache",
    )


def test_partition_pure_graph(tmp_path) -> None:
    project = _make_project([Brightness(amount=Animated[float](root=0.1))])
    ctx = _make_ctx(project, tmp_path)
    renderer = Renderer(project, ctx.options)
    prefix, tail = renderer._partition_segment(project.tracks[0].segments[0], ctx)
    assert len(prefix) == 1
    assert tail == []


def test_partition_splits_at_curve_flash(tmp_path) -> None:
    flash_with_curve = Flash(
        at=Seconds(t=0.0),
        duration=Animated[float](root=0.1),
        curve=BeatPulse(
            trigger=BeatRef(stream="beat"),
            envelope=ExpDecayEnv(tau=0.05),
            intensity_ramp=1.0,
        ),
    )
    project = _make_project(
        [Brightness(amount=Animated[float](root=0.1)), flash_with_curve]
    )
    ctx = _make_ctx(project, tmp_path)
    renderer = Renderer(project, ctx.options)
    prefix, tail = renderer._partition_segment(project.tracks[0].segments[0], ctx)
    assert [type(e).__name__ for e in prefix] == ["Brightness"]
    assert [type(e).__name__ for e in tail] == ["Flash"]


def test_partition_runs_fade_on_frame_pipeline_when_after_curve(tmp_path) -> None:
    flash_with_curve = Flash(
        at=Seconds(t=0.0),
        duration=Animated[float](root=0.1),
        curve=BeatPulse(
            trigger=BeatRef(stream="beat"),
            envelope=ExpDecayEnv(tau=0.05),
            intensity_ramp=1.0,
        ),
    )
    project = _make_project([flash_with_curve, Fade(direction="in", duration=0.2)])
    ctx = _make_ctx(project, tmp_path)
    renderer = Renderer(project, ctx.options)
    prefix, tail = renderer._partition_segment(project.tracks[0].segments[0], ctx)
    assert prefix == []
    assert [type(e).__name__ for e in tail] == ["Flash", "Fade"]
