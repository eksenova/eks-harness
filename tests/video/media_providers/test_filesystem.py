"""FilesystemProvider tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import ImageFile, Project, Seconds, Segment, Track
from eks_harness.video.plugins.builtin.media_providers.filesystem import FilesystemProvider
from eks_harness.video.render.context import RenderContext, RenderOptions


@pytest.fixture
def ctx(tmp_path: Path) -> RenderContext:
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
                        media=ImageFile(path="x.png"),
                        in_=Seconds(t=0.0),
                        out=Seconds(t=1.0),
                    )
                ],
            )
        ],
    )
    return RenderContext(
        project=project,
        options=RenderOptions(output=tmp_path / "out.mp4"),
        markers=MarkerSet(),
        workspace=tmp_path,
        cache_dir=tmp_path / "cache",
    )


def test_handles_bare_paths_and_file_urls() -> None:
    provider = FilesystemProvider()
    assert provider.can_handle("/tmp/foo.mp4")
    assert provider.can_handle("file:///tmp/foo.mp4")
    assert not provider.can_handle("https://example.com/foo.mp4")


def test_resolve_returns_absolute_existing_path(tmp_path: Path, ctx: RenderContext) -> None:
    target = tmp_path / "video.mp4"
    target.write_bytes(b"x")
    resolved = FilesystemProvider().resolve(str(target), ctx)
    assert resolved == target


def test_resolve_missing_path_raises(tmp_path: Path, ctx: RenderContext) -> None:
    with pytest.raises(FileNotFoundError):
        FilesystemProvider().resolve(str(tmp_path / "nope.mp4"), ctx)
