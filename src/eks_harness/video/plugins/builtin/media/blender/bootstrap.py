"""Blender-side entry point for the video engine's ``BlenderScene`` (runs inside Blender; stdlib and bpy only).

Invoked by the farm as ``blender --factory-startup -b [scene.blend] --python bootstrap.py -- <config> -`` where
``<config>`` is the JSON config (inline, or a path to it). It configures the
scene to the project's timing, runs the user's script (which can
``import eks_harness.blender`` for the timing, params and markers), re-applies
the timing the composite depends on, picks a compute device, prints
``EHX_READY`` and renders the scene frames it reads from stdin to
``frames_dir/f_<frame>.png`` (atomically, so an interrupted render resumes).
With the frame cache on, a frame whose fingerprint matches the recorded one
for its existing image on this platform is reported as ``EHX_SKIP`` instead
of being rendered.
"""

import json
import os
import platform
import runpy
import sys
from pathlib import Path

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:]
CONFIG = json.loads(ARGS[0] if ARGS[0].lstrip().startswith("{") else Path(ARGS[0]).read_text(encoding="utf-8"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if "eks_harness" not in sys.modules:
    import types

    _package = types.ModuleType("eks_harness")
    _package.__path__ = [str(Path(CONFIG["runtime_dir"]).parent)]
    sys.modules["eks_harness"] = _package

import eks_harness.blender  # noqa: E402

eks_harness.blender._load(CONFIG)


def active_scene():
    if CONFIG["scene"]:
        if CONFIG["scene"] not in bpy.data.scenes:
            raise RuntimeError(f"scene {CONFIG['scene']!r} not found")
        return bpy.data.scenes[CONFIG["scene"]]
    return bpy.context.scene


def configure(scene):
    fps = float(CONFIG["fps"])
    scene.render.fps = max(1, round(fps))
    scene.render.fps_base = scene.render.fps / fps
    scene.render.resolution_x = CONFIG["width"]
    scene.render.resolution_y = CONFIG["height"]
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    scene.render.film_transparent = bool(CONFIG["transparent"])
    scene.frame_start = CONFIG["first_frame"]
    scene.frame_end = CONFIG["first_frame"] + CONFIG["frame_count"] - 1
    settings = scene.render.image_settings
    settings.file_format = "PNG"
    settings.color_mode = "RGBA" if CONFIG["transparent"] else "RGB"
    settings.color_depth = "8"
    if CONFIG["engine"]:
        aliases = {"BLENDER_EEVEE_NEXT": "BLENDER_EEVEE", "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT"}
        try:
            scene.render.engine = CONFIG["engine"]
        except TypeError:
            scene.render.engine = aliases.get(CONFIG["engine"], CONFIG["engine"])
    if CONFIG["samples"]:
        if scene.render.engine == "CYCLES":
            scene.cycles.samples = CONFIG["samples"]
        elif hasattr(scene, "eevee"):
            scene.eevee.taa_render_samples = CONFIG["samples"]
    if CONFIG["camera"]:
        if CONFIG["camera"] not in bpy.data.objects:
            raise RuntimeError(f"camera {CONFIG['camera']!r} not found")
        scene.camera = bpy.data.objects[CONFIG["camera"]]


def use_device(scene):
    if scene.render.engine != "CYCLES":
        return
    wanted = os.environ.get("EKS_HARNESS_CYCLES_DEVICE") or CONFIG["device"]
    if wanted == "CPU":
        scene.cycles.device = "CPU"
        return
    prefs = bpy.context.preferences.addons["cycles"].preferences
    order = [wanted] if wanted != "AUTO" else (["METAL"] if sys.platform == "darwin" else
                                               ["OPTIX", "CUDA", "HIP", "ONEAPI"])
    for backend in order:
        try:
            prefs.compute_device_type = backend
        except TypeError:
            continue
        prefs.get_devices()
        found = [d for d in prefs.devices if d.type == backend]
        if not found:
            continue
        for device in prefs.devices:
            device.use = device.type == backend
        scene.cycles.device = "GPU"
        print(f"EHX_DEVICE {backend} {', '.join(d.name for d in found)}", flush=True)
        return
    if wanted != "AUTO":
        raise RuntimeError(f"Cycles device {wanted} is not available")
    scene.cycles.device = "CPU"
    print("EHX_DEVICE CPU", flush=True)


configure(active_scene())
if CONFIG["script"]:
    runpy.run_path(CONFIG["script"], run_name="__main__")
scene = active_scene()
configure(scene)
use_device(scene)
if CONFIG.get("events_out"):
    Path(CONFIG["events_out"]).write_text(json.dumps({"emitted": eks_harness.blender.emitted}), encoding="utf-8")
if CONFIG.get("emit_only"):
    print("EHX_EMITTED", flush=True)
    sys.exit(0)
if scene.camera is None:
    raise RuntimeError("the scene has no camera; set one in the blend file or script, or pass camera=")

def platform_tag():
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system(),
                                                                          platform.system().lower())
    return f"{system}-{platform.machine().lower()}"


out = Path(CONFIG["frames_dir"])
out.mkdir(parents=True, exist_ok=True)
TAG = platform_tag()
known = {int(k): tuple(v) for k, v in (CONFIG.get("fingerprints") or {}).items()}
printer = None
if CONFIG.get("frame_cache"):
    from frame_fingerprint import Fingerprinter

    printer = Fingerprinter(scene)


def one(frame):
    final = out / f"f_{frame:06d}.png"
    fp = printer.frame(frame) if printer else "-"
    if final.exists() and (printer is None or known.get(frame) == (TAG, fp)):
        print(f"EHX_SKIP {frame} {fp} {TAG}", flush=True)
        return
    scene.frame_set(frame)
    partial = out / f"f_{frame:06d}.part.png"
    scene.render.filepath = str(partial)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    os.replace(partial, final)
    print(f"EHX_FRAME {frame} {fp} {TAG}", flush=True)


print("EHX_READY", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line or line == "done":
        break
    one(int(line))
