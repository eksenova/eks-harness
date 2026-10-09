"""Eligibility + chunk-splitting unit tests for the parallel frame pipeline.

These exercise the orchestrator's decision of *when* to fan a segment out
across worker processes and the contiguous range partitioning, without
spawning workers (which require a ``project.py`` on disk and ffmpeg). The
end-to-end determinism proof lives in the repo's verification script.
"""

from __future__ import annotations


from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import (
    Animated,
    Blur,
    Datamosh,
    Glitch,
    ImageFile,
    Project,
    Seconds,
    Segment,
    Shake,
    Track,
)
from eks_harness.video.render.context import RenderContext, RenderOptions
from eks_harness.video.render.orchestrator import Renderer, _frame_count
from eks_harness.video.render.parallel_frame_pipeline import (
    MIN_CHUNK_FRAMES,
    resolve_worker_count,
    split_frame_ranges,
)


def _project(effects: list, *, fps: float = 30.0, duration: float = 10.0) -> Project:
    return Project(
        fps=fps,
        resolution=(64, 64),
        duration=duration,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="s",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="image.jpg"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=duration),
                        effects=effects,
                    )
                ],
            )
        ],
    )


def _renderer_and_plan(effects: list, *, duration: float = 10.0):
    project = _project(effects, duration=duration)
    options = RenderOptions(output="out.mp4")
    renderer = Renderer(project, options)
    ctx = RenderContext(
        project=project,
        options=options,
        markers=MarkerSet(),
        workspace="ws",
        cache_dir="ws/cache",
    )
    plan = renderer._plan_segment(project.tracks[0].segments[0], ctx, MarkerSet())
    frames = _frame_count(plan, fps=float(project.fps))
    return renderer, plan, frames


# --- split_frame_ranges ------------------------------------------------------


def test_split_tiles_exactly_with_even_division() -> None:
    ranges = split_frame_ranges(120, 4)
    assert ranges == [(0, 30), (30, 30), (60, 30), (90, 30)]


def test_split_remainder_goes_to_leading_chunks() -> None:
    ranges = split_frame_ranges(157, 4)
    assert ranges == [(0, 40), (40, 39), (79, 39), (118, 39)]
    # Tiles [0, 157) with no gap or overlap.
    assert ranges[0][0] == 0
    assert sum(count for _start, count in ranges) == 157
    for (s0, c0), (s1, _c1) in zip(ranges, ranges[1:]):
        assert s0 + c0 == s1


def test_split_single_worker_is_one_range() -> None:
    assert split_frame_ranges(157, 1) == [(0, 157)]


# --- resolve_worker_count ----------------------------------------------------


def test_worker_count_env_override_one_forces_serial(monkeypatch) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "1")
    assert resolve_worker_count(10_000) == 1


def test_worker_count_env_override_zero_forces_serial(monkeypatch) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "0")
    assert resolve_worker_count(10_000) == 1


def test_worker_count_clamped_by_min_chunk(monkeypatch) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "8")
    # Only enough frames for 2 chunks of MIN_CHUNK_FRAMES.
    assert resolve_worker_count(2 * MIN_CHUNK_FRAMES) == 2


def test_worker_count_honours_requested_cap(monkeypatch) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "3")
    assert resolve_worker_count(100_000) == 3


# --- eligibility gate --------------------------------------------------------


def test_stateless_tail_is_eligible(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "4")
    effects = [
        Shake(intensity=Animated[float](root=0.5)),
        Glitch(intensity=Animated[float](root=0.5)),
    ]
    renderer, plan, frames = _renderer_and_plan(effects)
    # Eligibility requires a project.py the worker can reconstruct from.
    (tmp_path / "project.py").write_text("project = None\n", encoding="utf-8")
    renderer.options.workspace = tmp_path
    assert renderer._parallel_worker_count(plan, frames) == 4


def test_datamosh_tail_forced_serial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "4")
    renderer, plan, frames = _renderer_and_plan([Datamosh()])
    (tmp_path / "project.py").write_text("project = None\n", encoding="utf-8")
    renderer.options.workspace = tmp_path
    assert renderer._parallel_worker_count(plan, frames) == 1


def test_graph_prefix_forces_serial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "4")
    # Blur lowers to the ffmpeg graph, so it becomes a graph_prefix; the
    # trailing Glitch spills to the frame tail. A non-empty graph prefix is
    # time-dependent in general → serial.
    effects = [
        Blur(radius=Animated[float](root=2.0)),
        Glitch(intensity=Animated[float](root=0.5)),
    ]
    renderer, plan, frames = _renderer_and_plan(effects)
    assert plan.graph_prefix  # sanity: blur folded into the prefix
    (tmp_path / "project.py").write_text("project = None\n", encoding="utf-8")
    renderer.options.workspace = tmp_path
    assert renderer._parallel_worker_count(plan, frames) == 1


def test_small_segment_forced_serial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "4")
    # Below 2 * MIN_CHUNK_FRAMES frames → not worth parallelising.
    renderer, plan, frames = _renderer_and_plan(
        [Glitch(intensity=Animated[float](root=0.5))], duration=1.0
    )
    assert frames < 2 * MIN_CHUNK_FRAMES
    (tmp_path / "project.py").write_text("project = None\n", encoding="utf-8")
    renderer.options.workspace = tmp_path
    assert renderer._parallel_worker_count(plan, frames) == 1


def test_missing_project_py_forces_serial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKS_HARNESS_RENDER_WORKERS", "4")
    renderer, plan, frames = _renderer_and_plan(
        [Glitch(intensity=Animated[float](root=0.5))]
    )
    renderer.options.workspace = tmp_path  # no project.py written
    assert renderer._parallel_worker_count(plan, frames) == 1
