"""BlenderScene media renderer: IR, registry, timing map, cache key and real Blender renders."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir import PluginRequirement, Project, RenderSettings, Seconds, Segment, Solid, Track
from eks_harness.video.plugins.base import MediaRenderRequest
from eks_harness.video.plugins.builtin.media.blender import BlenderRenderer, BlenderScene, runner
from eks_harness.video.plugins.registry import get_media_renderer, register_media_renderer
from eks_harness.video.render.context import RenderContext, RenderOptions
from eks_harness.video.render.materialize import materialize_media, request_key

HAS_BLENDER = True
try:
    runner.find_blender()
except RuntimeError:
    HAS_BLENDER = False
needs_blender = pytest.mark.skipif(not HAS_BLENDER or shutil.which("ffmpeg") is None,
                                   reason="needs a Blender executable and ffmpeg")

@pytest.fixture(autouse=True)
def _no_user_farm(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never pick up the developer's user farm.toml (it would send test work to real machines)."""

    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("EKS_HARNESS_FARM_CONFIG", raising=False)
    monkeypatch.delenv("EKS_HARNESS_FARM", raising=False)


FPS = 24
SIZE = 64
BEAT_SCRIPT = """
import bpy
import eks_harness.blender as vdb

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
scene = bpy.context.scene
bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0, 0, 0))
plane = bpy.context.active_object
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
scene.collection.objects.link(cam)
cam.location = (0, 0, 3)
scene.camera = cam
plane.hide_render = True
plane.keyframe_insert("hide_render", frame=vdb.frame_start)
for f in vdb.frames("beat"):
    plane.hide_render = False
    plane.keyframe_insert("hide_render", frame=f)
    plane.hide_render = True
    plane.keyframe_insert("hide_render", frame=f + 1)
for fc in plane.animation_data.action.fcurves if hasattr(plane.animation_data.action, "fcurves") else []:
    for kp in fc.keyframe_points:
        kp.interpolation = "CONSTANT"
"""


def _ctx(tmp_path: Path, project: Project, markers: MarkerSet | None = None) -> RenderContext:
    return RenderContext(project=project, options=RenderOptions(output=tmp_path / "out.mp4", workspace=tmp_path),
                         markers=markers or MarkerSet(), workspace=tmp_path, cache_dir=tmp_path / "cache")


def _project(media: BlenderScene, *, duration: float = 1.0, overlay: bool = False, enabled: bool = True) -> Project:
    blender_seg = Segment(id="scene", start=Seconds(t=0.0), in_=Seconds(t=0.0), out=Seconds(t=duration), media=media)
    base = Segment(id="bg", start=Seconds(t=0.0), out=Seconds(t=duration), media=Solid(color=(10, 20, 30, 255)))
    tracks = [Track(name="base", z=0, segments=[base]), Track(name="fx", z=1, segments=[blender_seg])] if overlay \
        else [Track(name="main", segments=[blender_seg])]
    return Project(fps=FPS, resolution=(SIZE, SIZE), duration=duration,
                   render_settings=RenderSettings(pix_fmt="yuv420p"), tracks=tracks,
                   plugins=[PluginRequirement(name="blender")] if enabled else [])


def _runtime():
    spec = importlib.util.spec_from_file_location("vdb_test", runner.RUNTIME)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_needs_a_blend_or_a_script() -> None:
    with pytest.raises(ValueError, match="blend file, a script"):
        BlenderScene()


def test_project_roundtrips_with_blender_scene(tmp_path: Path) -> None:
    project = _project(BlenderScene(script="scene.py", params={"color": "gold"}, workers=2))
    again = Project.model_validate_json(project.model_dump_json(by_alias=True))
    media = again.tracks[0].segments[0].media
    assert isinstance(media, BlenderScene)
    assert media.params == {"color": "gold"} and media.workers == 2


def test_registered_renderer_handles_the_kind() -> None:
    register_media_renderer(BlenderRenderer())
    assert isinstance(get_media_renderer("blender_scene"), BlenderRenderer)


def test_runtime_maps_project_time_through_start_in_and_speed() -> None:
    vdb = _runtime()
    vdb._load({"fps": 30.0, "width": 10, "height": 10, "frame_zero": 1, "first_frame": 61, "frame_count": 90,
               "source_start": 2.0, "source_end": 5.0, "project_start": 10.0, "speed": 2.0, "params": {},
               "markers": {"streams": {"beat": [10.0, 10.5, 30.0]}, "named": {}, "words": []},
               "segment_id": "s"})
    assert vdb.frame_at_project(10.0) == pytest.approx(61.0)
    assert vdb.frame_at_project(10.5) == pytest.approx(91.0)
    assert vdb.frames("beat") == [61, 91]
    assert vdb.frames("beat", inside=False) == [61, 91, 1261]


def test_workers_capped_by_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKS_HARNESS_BLENDER_WORKERS", "8")
    assert runner.worker_count(None, 3) == 3
    assert runner.worker_count(2, 100) == 2


def test_cache_key_follows_script_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "find_blender", lambda explicit=None: "blender")
    monkeypatch.setattr(runner, "blender_version", lambda binary: "Blender 9.9")
    script = tmp_path / "scene.py"
    script.write_text("print(1)")
    media = BlenderScene(script="scene.py")
    project = _project(media)
    ctx = _ctx(tmp_path, project)
    req = MediaRenderRequest(segment_id="scene", media=media, fps=FPS, resolution=(SIZE, SIZE), source_start=0.0,
                             source_end=1.0, project_start=0.0, speed=1.0, frame_count=FPS, markers={},
                             work_dir=tmp_path, output=tmp_path / "x.mov")
    renderer = BlenderRenderer()
    first = request_key(renderer, req, ctx)
    assert request_key(renderer, req, ctx) == first
    script.write_text("print(2)")
    assert request_key(renderer, req, ctx) != first



def test_cache_key_follows_nested_media_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eks_harness.video.render import materialize

    monkeypatch.setattr(runner, "find_blender", lambda explicit=None: "blender")
    monkeypatch.setattr(runner, "blender_version", lambda binary: "Blender 9.9")
    (tmp_path / "scene.py").write_text("print(1)")
    screen = tmp_path / "screen.mov"
    screen.write_bytes(b"take one")
    media = BlenderScene(script="scene.py", media={"screen": str(screen)})
    project = _project(media)
    ctx = _ctx(tmp_path, project)
    req = MediaRenderRequest(segment_id="scene", media=media, fps=FPS, resolution=(SIZE, SIZE), source_start=0.0,
                             source_end=1.0, project_start=0.0, speed=1.0, frame_count=FPS, markers={},
                             work_dir=tmp_path, output=tmp_path / "x.mov")
    renderer = BlenderRenderer()
    rendered = []

    def fake_render(request, context):
        rendered.append(request.output.name)
        request.output.write_bytes(b"clip")
        return request.output

    monkeypatch.setattr(renderer, "render", fake_render)
    first = materialize.render_request(renderer, req, ctx)
    assert materialize.render_request(renderer, req, ctx) == first and len(rendered) == 1
    screen.write_bytes(b"take two, re-captured")
    second = materialize.render_request(renderer, req, ctx)
    assert second != first and len(rendered) == 2
    assert materialize.nested_media(media) == {"screen": str(screen)}

def _frames_rgba(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, SIZE, SIZE, 4)


@needs_blender
def test_scene_is_frame_locked_to_project_markers(tmp_path: Path) -> None:
    (tmp_path / "scene.py").write_text(BEAT_SCRIPT)
    media = BlenderScene(script="scene.py", engine="BLENDER_WORKBENCH", transparent=True)
    project = _project(media)
    register_media_renderer(BlenderRenderer())
    result = materialize_media(project, _ctx(tmp_path, project, MarkerSet(streams={"beat": [0.5]})))

    clip = Path(result.tracks[0].segments[0].media.path)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(clip)],
                                      check=True, capture_output=True, text=True).stdout)["streams"][0]
    assert probe["pix_fmt"].startswith("yuva")
    frames = _frames_rgba(clip)
    assert len(frames) == FPS
    visible = [i for i, f in enumerate(frames) if f[SIZE // 2, SIZE // 2, 3] > 128]
    assert visible == [12]


@needs_blender
def test_overlay_track_renders_in_parallel_and_caches(tmp_path: Path) -> None:
    from eks_harness.video.render import Renderer

    (tmp_path / "scene.py").write_text(BEAT_SCRIPT.replace('vdb.frames("beat")', "[vdb.frame_start + 3]"))
    media = BlenderScene(script="scene.py", engine="BLENDER_WORKBENCH", workers=2)
    project = _project(media, overlay=True)
    out = tmp_path / "out.mp4"
    Renderer(project, RenderOptions(output=out, workspace=tmp_path)).render()
    assert out.exists() and out.stat().st_size > 0
    clips = sorted((tmp_path / "cache" / "media" / "blender").glob("*.mov"))
    assert len(clips) == 1
    stamp = clips[0].stat().st_mtime_ns
    Renderer(project, RenderOptions(output=tmp_path / "again.mp4", workspace=tmp_path)).render()
    assert clips[0].stat().st_mtime_ns == stamp


def test_blender_scene_needs_the_plugin_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_lookup(explicit=None):
        raise AssertionError("Blender must not be looked up when the plugin is not enabled")

    monkeypatch.setattr(runner, "find_blender", no_lookup)
    register_media_renderer(BlenderRenderer())
    project = _project(BlenderScene(script="scene.py"), enabled=False)
    with pytest.raises(ValueError, match="PluginRequirement"):
        materialize_media(project, _ctx(tmp_path, project))


def test_projects_without_blender_never_look_it_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eks_harness.video.render import Renderer

    def no_lookup(explicit=None):
        raise AssertionError("Blender must not be looked up")

    monkeypatch.setattr(runner, "find_blender", no_lookup)
    base = Segment(id="bg", start=Seconds(t=0.0), out=Seconds(t=0.5), media=Solid(color=(1, 2, 3, 255)))
    project = Project(fps=FPS, resolution=(SIZE, SIZE), duration=0.5, render_settings=RenderSettings(pix_fmt="yuv420p"),
                      tracks=[Track(name="main", segments=[base])])
    out = tmp_path / "plain.mp4"
    Renderer(project, RenderOptions(output=out, workspace=tmp_path)).render()
    assert out.exists()


def test_enabled_plugin_fails_fast_without_blender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eks_harness.video.render import Renderer

    monkeypatch.delenv("EKS_HARNESS_BLENDER", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(runner, "_SEARCH", [])
    runner.blender_version.cache_clear()
    project = _project(BlenderScene(script="scene.py"))
    with pytest.raises(RuntimeError, match=r"Blender 4\.2 or newer"):
        Renderer(project, RenderOptions(output=tmp_path / "x.mp4", workspace=tmp_path)).render()


@needs_blender
def test_frame_cache_skips_unchanged_frames(tmp_path: Path) -> None:
    script = tmp_path / "scene.py"
    script.write_text(BEAT_SCRIPT)
    media = BlenderScene(script="scene.py", engine="BLENDER_WORKBENCH", farm=False)
    project = _project(media, duration=0.5)
    register_media_renderer(BlenderRenderer())
    markers = MarkerSet(streams={"beat": [0.25]})
    materialize_media(project, _ctx(tmp_path, project, markers))
    frames_dir = next((tmp_path / "cache" / "media" / "blender" / "frames").iterdir())
    stamps = {p.name: p.stat().st_mtime_ns for p in frames_dir.glob("f_*.png")}
    assert len(stamps) == FPS // 2

    script.write_text(BEAT_SCRIPT + "\n# a comment changes the script but not the scene\n")
    materialize_media(project, _ctx(tmp_path, project, markers))
    assert {p.name: p.stat().st_mtime_ns for p in frames_dir.glob("f_*.png")} == stamps

    script.write_text(BEAT_SCRIPT + "\ncam.location = (0, 0, 2.5)\n")
    materialize_media(project, _ctx(tmp_path, project, markers))
    assert all(p.stat().st_mtime_ns != stamps[p.name] for p in frames_dir.glob("f_*.png"))


@needs_blender
def test_nested_media_reaches_the_script(tmp_path: Path) -> None:
    clip = tmp_path / "screen.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x64:rate={FPS}:duration=1",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    (tmp_path / "scene.py").write_text(BEAT_SCRIPT + '''
import os
assert os.path.exists(vdb.media["screen"]), vdb.media
img = bpy.data.images.load(vdb.media["screen"])
assert img.source == "MOVIE"
''')
    media = BlenderScene(script="scene.py", engine="BLENDER_WORKBENCH", farm=False, media={"screen": str(clip)})
    project = _project(media, duration=0.5)
    register_media_renderer(BlenderRenderer())
    result = materialize_media(project, _ctx(tmp_path, project, MarkerSet(streams={"beat": [0.25]})))
    assert Path(result.tracks[0].segments[0].media.path).exists()
    assert Path(f"{clip.resolve()}.framemd5").exists()
