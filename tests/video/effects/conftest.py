"""Shared fixtures for effect tests."""

from __future__ import annotations

import pytest

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.plugins.registry import load_builtins
from eks_harness.video.render.context import RenderContext, RenderOptions


@pytest.fixture(scope="session", autouse=True)
def _load_plugins() -> None:
    load_builtins()


@pytest.fixture
def tiny_project() -> Project:
    return Project(
        fps=30,
        resolution=(64, 64),
        duration=1.0,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="seg",
                        start=Seconds(t=0.0),
                        media=ImageFile(path="x.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=1.0),
                    )
                ],
            )
        ],
    )


@pytest.fixture
def render_ctx(tiny_project: Project, tmp_path) -> RenderContext:
    return RenderContext(
        project=tiny_project,
        options=RenderOptions(output=tmp_path / "out.mp4"),
        markers=MarkerSet(),
        workspace=tmp_path,
        cache_dir=tmp_path / "cache",
    )
