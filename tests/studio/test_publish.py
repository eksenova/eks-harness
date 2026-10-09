"""Publishing an outside render into the studio's render list and the artifact store, with a deep link."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eks_harness.studio.artifacts import list_artifacts
from eks_harness.studio.publish import publish


@pytest.fixture()
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:duration=1",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


class FakeClient:
    base_url = "http://harness.test:7171"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, list[Path], dict[str, Any]]] = []

    def upload(self, path: str, files: list[Path], fields: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, files, fields))
        if self.fail:
            raise ConnectionError("daemon unreachable")
        return {"id": "A1", "url": "http://harness.test:7171/p/acme/promo/s/promo/a/A1",
                "rawUrl": "http://harness.test:7171/raw/A1/out.mp4"}

    def close(self) -> None:
        return None


def _project(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    (tree / ".harness").mkdir(parents=True)
    (tree / ".harness" / "project.toml").write_text('[project]\nid = "acme/web-app"\n[video]\nproject = "acme/promo"\n')
    project = tree / "videos" / "Promo Cut"
    project.mkdir(parents=True)
    (project / "project.py").write_text("project = None\n")
    return project


def test_publish_lists_the_render_uploads_it_and_links_to_it(tmp_path: Path, clip: Path) -> None:
    project = _project(tmp_path)
    client = FakeClient()
    result = publish(clip, project, mode="final", label="promo final", render_seconds=12.5, client=client,
                     tags=["campaign"])
    assert result.url == f"http://harness.test:7171/studio?project=Promo+Cut&render={result.job_id}"
    assert result.file_url == "http://harness.test:7171/raw/A1/out.mp4"
    assert result.project == "acme/promo" and result.session == "promo-cut"
    ((path, files, fields),) = client.calls
    assert path == "/api/artifacts" and files == [result.output]
    assert fields["project"] == "acme/promo" and fields["tags"] == "render,final,campaign"
    assert fields["meta"]["renderJob"] == result.job_id
    meta = json.loads((result.output.parent / "metadata.json").read_text())
    assert meta["record"]["status"] == "succeeded"
    assert meta["record"]["updated_at"] - meta["record"]["started_at"] == pytest.approx(12.5)
    (art,) = list_artifacts(project)
    assert art.job_id == result.job_id


def test_publish_keeps_the_studio_render_when_the_upload_fails(tmp_path: Path, clip: Path) -> None:
    project = _project(tmp_path)
    result = publish(clip, project, client=FakeClient(fail=True))
    assert result.artifact is None and "daemon unreachable" in result.warnings[0]
    assert result.file_url.endswith(f"/api/studio/files/projects/Promo Cut/renders/{result.job_id}/out.mp4")
    assert result.output.is_file()


def test_publish_without_upload_uses_the_public_url(tmp_path: Path, clip: Path) -> None:
    project = tmp_path / "solo"
    project.mkdir()
    result = publish(clip, project, upload=False)
    assert result.url.startswith("http://127.0.0.1:7171/studio?project=solo&render=")
