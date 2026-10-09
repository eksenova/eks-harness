"""WebScene media renderer: JS scenes on the virtual clock, score inputs, marker events, textures, emitted events."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import Project, RenderSettings, Seconds, Segment, Track
from eks_harness.video.plugins.base import MediaRenderRequest
from eks_harness.video.plugins.builtin.media.web_scene import SceneEvent, WebScene, WebSceneRenderer, runner
from eks_harness.video.render.context import RenderContext, RenderOptions

FPS = 10
SIZE = 32


def _browser_ok() -> bool:
    try:
        runner.require_playwright()
        runner.runtime_script()
    except runner.WebSceneError:
        return False
    return runner.browser_version() != "unknown" and shutil.which("ffmpeg") is not None


needs_browser = pytest.mark.skipif(not _browser_ok(), reason="needs Playwright Chromium, the scene runtime and ffmpeg")

SCENE = """<!doctype html>
<html><head><style>
html, body { margin: 0; width: 32px; height: 32px; overflow: hidden; }
#box { position: absolute; left: 0; top: 0; width: 32px; height: 32px; background: rgb(255, 0, 0); }
#fade { position: absolute; left: 0; top: 16px; width: 32px; height: 16px; background: rgb(0, 255, 0);
        animation: fade 1s linear forwards; }
@keyframes fade { from { opacity: 0 } to { opacity: 1 } }
</style></head>
<body><div id="box"></div><div id="fade"></div>
<script>
  const box = document.getElementById("box");
  let half = false;
  ehx.on("flip", (data) => { box.style.background = data.color; });
  ehx.on("beat", (data) => { ehx.emit("saw-beat", data); });
  requestAnimationFrame(function tick(t) {
    if (!half && t >= 500) { half = true; ehx.emit("half", { at: t }); }
    requestAnimationFrame(tick);
  });
  setTimeout(() => { document.body.dataset.timer = String(Date.now()); }, 300);
</script></body></html>
"""


def _ctx(tmp_path: Path, media: WebScene, duration: float) -> RenderContext:
    seg = Segment(id="scene", start=Seconds(t=0.0), in_=Seconds(t=0.0), out=Seconds(t=duration), media=media)
    project = Project(fps=FPS, resolution=(SIZE, SIZE), duration=duration,
                      render_settings=RenderSettings(pix_fmt="yuv420p"), tracks=[Track(name="main", segments=[seg])])
    return RenderContext(project=project, options=RenderOptions(output=tmp_path / "out.mp4", workspace=tmp_path),
                         markers=MarkerSet(), workspace=tmp_path, cache_dir=tmp_path / "cache")


def _frames(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, SIZE, SIZE, 4)


def test_scene_inputs_merge_events_and_markers() -> None:
    media = WebScene(entry="scene/index.html", events=[SceneEvent(time=0.5, name="flip")],
                     marker_events={"beat": "beat"})
    request = MediaRenderRequest(segment_id="s", media=media, fps=FPS, resolution=(SIZE, SIZE), source_start=1.0,
                                 source_end=2.0, project_start=10.0, speed=1.0, frame_count=10,
                                 markers={"streams": {"beat": [9.0, 10.25, 10.75, 12.0]}}, work_dir=Path("."),
                                 output=Path("x.mov"))
    inputs = runner.scene_inputs(request)
    assert [(round(i["time"], 3), i["name"]) for i in inputs] == [(0.5, "flip"), (1.25, "beat"), (1.75, "beat")]
    assert inputs[1]["data"] == {"index": 1, "stream": "beat"}


@needs_browser
def test_web_scene_renders_frame_exact_with_events(tmp_path: Path) -> None:
    (tmp_path / "scene").mkdir()
    (tmp_path / "scene" / "index.html").write_text(SCENE, encoding="utf-8")
    media = WebScene(entry="scene/index.html", transparent=False,
                     events=[SceneEvent(time=0.4, name="flip", data={"color": "rgb(0, 0, 255)"})],
                     marker_events={"beat": "beat"})
    ctx = _ctx(tmp_path, media, 1.0)
    request = MediaRenderRequest(segment_id="scene", media=media, fps=FPS, resolution=(SIZE, SIZE), source_start=0.0,
                                 source_end=1.0, project_start=0.0, speed=1.0, frame_count=FPS,
                                 markers={"streams": {"beat": [0.2, 0.7]}}, work_dir=tmp_path,
                                 output=tmp_path / "scene.mov")
    WebSceneRenderer().check_available(ctx)
    out = WebSceneRenderer().render(request, ctx)
    frames = _frames(out)
    assert frames.shape[0] == FPS
    top = frames[:, 4, 4, :3].astype(int)
    assert top[3][0] > 200 and top[3][2] < 50
    assert top[4][2] > 200 and top[4][0] < 50
    green = frames[:, 24, 4, 1].astype(int)
    assert green[0] < 30 and 100 < green[5] < 160 and green[9] > 200
    emitted = runner.emitted_events(out)
    names = [(e["name"], e["frame"]) for e in emitted]
    assert ("saw-beat", 2) in names and ("saw-beat", 7) in names and ("half", 5) in names
    assert json.loads(out.with_suffix(".events.json").read_text())["segment"] == "scene"
