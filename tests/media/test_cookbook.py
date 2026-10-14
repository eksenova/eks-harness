from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from eks.workspace import plan_spec
from eks_harness.ir.registry import Registry, default_registry

COOKBOOK = Path(__file__).resolve().parents[2] / "examples" / "cookbook"
FAST = ("beat_flash_montage", "speed_ramp_drop", "storytelling_bg_music", "mix_only", "annotated_walkthrough")
EDITS = sorted(path.stem for path in (COOKBOOK / ".harness" / "edits").glob("*.py"))
CHOSEN = EDITS if os.environ.get("EKS_COOKBOOK_FULL") == "1" else [name for name in EDITS if name in FAST]

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("cookbook") / "cookbook"
    shutil.copytree(COOKBOOK, root, ignore=shutil.ignore_patterns("__pycache__", "*.mp4", "*.wav", "*.png", "*.cube",
                                                                  "*.svg"))
    subprocess.run([sys.executable, str(root / ".harness" / "assets" / "generate.py")], check=True,
                   capture_output=True)
    return root.resolve()


def render(client: TestClient, tree: Path, name: str) -> tuple[str, str]:
    plan = plan_spec(f".harness/edits/{name}.py", cwd=tree).model_dump(mode="json", by_alias=True, exclude_none=True)
    response = client.post("/api/runs", json={"plan": plan, "tree": str(tree), "wait": 600,
                                              "options": {"cache": "off"}})
    assert response.status_code == 201, response.text
    run = response.json()["run"]
    report: dict[str, Any] = client.get(f"/api/runs/{run['id']}/report").json()
    assert run["status"] == "passed", report["failures"]
    videos = [item for item in report["artifacts"] if item["kind"] == "video"]
    raw = client.get(urlparse(videos[-1]["rawUrl"]).path)
    assert raw.status_code == 200
    return run["id"], hashlib.sha256(raw.content).hexdigest()


@pytest.mark.parametrize("name", CHOSEN)
def test_cookbook_edits_render_byte_identical_twice(client: TestClient, tree: Path, name: str) -> None:
    _first, one = render(client, tree, name)
    _second, two = render(client, tree, name)
    assert one == two


def test_the_generated_assets_are_reproducible(tree: Path, tmp_path: Path) -> None:
    again = tmp_path / "assets"
    shutil.copytree(tree / ".harness" / "assets", again, ignore=shutil.ignore_patterns("*.mp4", "*.wav", "*.png",
                                                                                      "*.cube", "*.svg"))
    subprocess.run([sys.executable, str(again / "generate.py")], check=True, capture_output=True)
    for path in sorted((tree / ".harness" / "assets").iterdir()):
        if path.suffix in (".mp4", ".wav", ".png", ".cube", ".svg"):
            assert (again / path.name).read_bytes() == path.read_bytes(), path.name
