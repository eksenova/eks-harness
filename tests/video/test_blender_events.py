"""Score inputs into Blender scripts and events emitted back out of them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import PluginRequirement, Project, RenderSettings, Seconds, Segment, Track
from eks_harness.video.plugins.base import MediaRenderRequest
from eks_harness.video.plugins.builtin.media.blender import BlenderRenderer, BlenderScene, runner
from eks_harness.video.render.context import RenderContext, RenderOptions

try:
    runner.find_blender()
    HAS_BLENDER = shutil.which("ffmpeg") is not None
except RuntimeError:
    HAS_BLENDER = False

FPS = 12
SIZE = 32
SCRIPT = """
import bpy
import eks_harness.blender as ehb

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
scene = bpy.context.scene
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
scene.collection.objects.link(cam)
cam.location = (0, 0, 3)
scene.camera = cam
for frame in ehb.input_frames("impact"):
    ehb.emit("landed", frame + 3, {"from": frame})
ehb.emit("intro")
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("EKS_HARNESS_FARM_CONFIG", raising=False)
    monkeypatch.delenv("EKS_HARNESS_FARM", raising=False)


@pytest.mark.skipif(not HAS_BLENDER, reason="needs Blender and ffmpeg")
def test_blender_reads_score_inputs_and_emits_events(tmp_path: Path) -> None:
    (tmp_path / "scene.py").write_text(SCRIPT, encoding="utf-8")
    media = BlenderScene(script="scene.py", engine="BLENDER_WORKBENCH", farm=False, frame_cache=False,
                         events=[{"time": 0.5, "name": "impact", "verb": "emit"}], emits=True)
    seg = Segment(id="phone", start=Seconds(t=0.0), in_=Seconds(t=0.0), out=Seconds(t=1.0), media=media)
    project = Project(fps=FPS, resolution=(SIZE, SIZE), duration=1.0, render_settings=RenderSettings(pix_fmt="yuv420p"),
                      tracks=[Track(name="main", segments=[seg])], plugins=[PluginRequirement(name="blender")])
    ctx = RenderContext(project=project, options=RenderOptions(output=tmp_path / "o.mp4", workspace=tmp_path),
                        markers=MarkerSet(), workspace=tmp_path, cache_dir=tmp_path / "cache")
    request = MediaRenderRequest(segment_id="phone", media=media, fps=FPS, resolution=(SIZE, SIZE), source_start=0.0,
                                 source_end=1.0, project_start=0.0, speed=1.0, frame_count=FPS, markers={},
                                 work_dir=tmp_path, output=tmp_path / "phone.mov")
    out = BlenderRenderer().render(request, ctx)
    assert out.exists()
    events = json.loads(out.with_suffix(".events.json").read_text())["emitted"]
    by_name = {e["name"]: e for e in events}
    assert by_name["landed"]["frame"] == 1 + 6 + 3 and by_name["landed"]["data"] == {"from": 7}
    assert by_name["landed"]["time"] == pytest.approx(9 / FPS)
    assert by_name["intro"]["frame"] == 1
