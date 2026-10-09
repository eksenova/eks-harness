"""Resource provider smoke tests.

Each resource is read through ``eks_harness.studio.resources.read`` (the URI
dispatcher every transport uses).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from eks_harness.studio.jobs import JobRegistry
from eks_harness.studio.resources.preview_frames import resolve_frame
from eks_harness.studio.roots import MEDIA_LIBRARY_ROOT, PROJECT_WORKSPACE_ROOT, RootsManager
from eks_harness.studio import resources


class _Reader:
    def __init__(self, roots: RootsManager, registry: JobRegistry) -> None:
        self.roots = roots
        self.registry = registry

    async def read_resource(self, uri: str) -> list[object]:
        spec, value = resources.read(uri, roots=self.roots, registry=self.registry)
        return [type("Contents", (), {"content": value, "mime_type": spec.mime_type})()]


def _build_server_with(roots: RootsManager, registry: JobRegistry) -> _Reader:
    return _Reader(roots, registry)


def test_template_resource_returns_python(workspace_root: Path, media_root: Path, job_registry) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    contents = asyncio.run(server.read_resource("eks-harness://video/templates/beat_flash_montage"))
    payload = list(contents)[0]
    assert "from eks_harness.video import" in payload.content


def test_effect_resource_returns_schema(workspace_root: Path, media_root: Path, job_registry) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    contents = asyncio.run(server.read_resource("eks-harness://video/effects/brightness"))
    payload = json.loads(list(contents)[0].content)
    assert payload["name"] == "brightness"
    assert "schema" in payload


def test_easing_resource_returns_png(workspace_root: Path, media_root: Path, job_registry) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    contents = asyncio.run(server.read_resource("eks-harness://video/easings/ease_in_out"))
    payload = list(contents)[0]
    raw = payload.content if isinstance(payload.content, (bytes, bytearray)) else payload.content
    if isinstance(raw, str):
        import base64

        raw = base64.b64decode(raw)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_audio_lib_resource_lists_root(workspace_root: Path, media_root: Path, job_registry) -> None:
    (media_root / "music.mp3").write_bytes(b"fake")
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    contents = asyncio.run(server.read_resource("eks-harness://video/audio_lib"))
    payload = json.loads(list(contents)[0].content)
    assert payload["category"] == "audio"
    assert any(entry["path"].endswith("music.mp3") for entry in payload["entries"])


def test_encoding_preset_resource_returns_builtin(workspace_root: Path, media_root: Path, job_registry) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    contents = asyncio.run(server.read_resource("eks-harness://video/encoding_presets/tiktok-1080-h264"))
    payload = json.loads(list(contents)[0].content)
    assert payload["video_codec"] == "h264"
    assert payload["resolution"] == [1080, 1920]


def test_render_job_resource_returns_status(workspace_root: Path, media_root: Path, job_registry, tmp_path: Path) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    record = asyncio.run(job_registry.create(project_id="demo", project_path=tmp_path, mode="preview"))
    contents = asyncio.run(server.read_resource(f"eks-harness://video/render_jobs/{record.job_id}"))
    payload = json.loads(list(contents)[0].content)
    assert payload["job_id"] == record.job_id
    assert payload["status"] == "queued"


def test_resolve_frame_latest_returns_highest_index(tmp_path: Path) -> None:
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    for idx in (1, 2, 7, 5):
        (frame_dir / f"{idx:04d}.jpg").write_bytes(b"x")
        time.sleep(0.005)
    assert resolve_frame(frame_dir, "latest").name == "0007.jpg"


def test_template_resource_unknown_name_raises(workspace_root: Path, media_root: Path, job_registry) -> None:
    roots = RootsManager()
    roots.register(PROJECT_WORKSPACE_ROOT, workspace_root)
    roots.register(MEDIA_LIBRARY_ROOT, media_root)
    server = _build_server_with(roots, job_registry)

    with pytest.raises(Exception):
        asyncio.run(server.read_resource("eks-harness://video/templates/nonexistent"))
