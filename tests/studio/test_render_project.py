"""``render_project`` tool tests.

We replace the render driver subprocess with a tiny inline script that emits
synthetic JSON-line progress events, then assert that progress notifications
land on the fake context and the job registry tracks them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eks_harness.studio.toolimpl import render_project as render_tool

pytestmark = pytest.mark.asyncio


_FAKE_DRIVER = """
import json
import sys
import time

events = [
    {"progress": 0.0, "message": "loading"},
    {"progress": 0.3, "message": "rendering segment 1"},
    {"progress": 0.7, "message": "rendering segment 2"},
    {"progress": 1.0, "message": "done"},
]
for event in events:
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()
    time.sleep(0.02)

# Touch the output file so render_project marks the job as succeeded.
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--project-dir")
parser.add_argument("--output")
parser.add_argument("--mode", default="preview")
parser.add_argument("--preview-frames-dir")
parser.add_argument("--range-start", default=None)
parser.add_argument("--range-end", default=None)
parser.add_argument("--settings", default=None)
args = parser.parse_args()
import pathlib
pathlib.Path(args.output).write_bytes(b"fake mp4")
"""


def _write_fake_project(workspace: Path, name: str) -> Path:
    project_dir = workspace / name
    (project_dir / "renders").mkdir(parents=True)
    (project_dir / "cache" / "preview_frames").mkdir(parents=True)
    (project_dir / "project.py").write_text("project = None\n", encoding="utf-8")
    (project_dir / "project.json").write_text("{}", encoding="utf-8")
    return project_dir


async def test_render_project_streams_progress(
    workspace_root: Path,
    roots,
    job_registry,
    fake_session_factory,
    fake_ctx_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_dir = _write_fake_project(workspace_root, "demo")

    fake_driver_path = tmp_path / "fake_driver.py"
    fake_driver_path.write_text(_FAKE_DRIVER, encoding="utf-8")

    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def fake_subprocess(*cmd: str, **kwargs):  # type: ignore[no-untyped-def]
        rewritten = list(cmd)
        for idx, item in enumerate(rewritten):
            if item == "-m" and idx + 1 < len(rewritten) and rewritten[idx + 1] == render_tool.RENDER_DRIVER_MODULE:
                rewritten[idx] = str(fake_driver_path)
                rewritten.pop(idx + 1)
                break
        return await real_create_subprocess_exec(rewritten[0], *rewritten[1:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(render_tool.asyncio, "create_subprocess_exec", fake_subprocess)

    ctx = fake_ctx_factory(fake_session_factory())

    job_payload = await render_tool._run(
        project_path=Path("demo"),
        mode="preview",
        encoder=None,
        bitrate_kbps=None,
        crf=None,
        range_start=None,
        range_end=None,
        ctx=ctx,
        roots=roots,
        registry=job_registry,
    )

    job_id = job_payload["job_id"]

    for _ in range(200):
        record = job_registry.get(job_id)
        if record is not None and record.status in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.05)

    record = job_registry.get(job_id)
    assert record is not None
    assert record.status == "succeeded", f"unexpected status: {record.status} ({record.error})"
    assert any(p > 0 for p, _, _ in ctx.progress_events)
    assert record.output_path is not None and record.output_path.startswith("file://")
    output_path = Path(record.output_path[len("file://"):].lstrip("/"))
    if not output_path.exists():
        # Path on Windows: file:///D:/... -> strip leading slash
        output_path = Path(record.output_path.replace("file:///", ""))
    assert (project_dir / "renders").exists()
