from __future__ import annotations

import importlib
import importlib.util
import tempfile
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.effects import (
    AutoReframe,
    BiRefNetRemove,
    EffectBase,
    MatAnyoneRemove,
    PunchIn,
    SAM3Track,
)
from eks_harness.ir.time import TICKS_PER_SECOND
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    Image,
    Pixels,
    to_uint8,
)

ML_KINDS = frozenset({"matanyone_remove", "birefnet_remove", "sam3_track", "auto_reframe", "punch_in"})
INSTALL = "install eks-harness[ml]"
BLAZEFACE = ("https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/"
             "blaze_face_short_range.tflite")
DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "matanyone": ("matanyone",),
    "rembg": ("rembg",),
    "torch": ("torch",),
    "onnxruntime": ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"),
    "sam3": ("sam3",),
    "mediapipe": ("mediapipe",),
    "cv2": ("opencv-python-headless", "opencv-python", "opencv-contrib-python"),
}


def modules(effect: EffectBase) -> tuple[str, ...]:
    if isinstance(effect, MatAnyoneRemove):
        return ("matanyone", "rembg", "onnxruntime", "torch")
    if isinstance(effect, BiRefNetRemove):
        return ("rembg", "onnxruntime")
    if isinstance(effect, SAM3Track):
        return ("sam3", "torch")
    if isinstance(effect, AutoReframe):
        return ("mediapipe",) if effect.tracker == "face" else ("cv2",)
    if isinstance(effect, PunchIn):
        return ("mediapipe",) if effect.track_speaker else ()
    raise NodeFailure("executor_error", f"{effect.kind} is not a device-dependent effect")


def require(effect: EffectBase) -> None:
    missing = [name for name in modules(effect) if importlib.util.find_spec(name) is None]
    if missing:
        raise NodeFailure("resolution_unavailable",
                          f"{effect.kind} needs {', '.join(missing)}, which this machine does not have; {INSTALL}",
                          detail={"effect": effect.kind, "missing": list[Any](missing)})


def package_version(module: str) -> str:
    for name in DISTRIBUTIONS.get(module, (module,)):
        try:
            return f"{name} {version(name)}"
        except PackageNotFoundError:
            continue
    return f"{module} unknown"


def torch_device() -> str:
    torch: Any = importlib.import_module("torch")
    if bool(torch.cuda.is_available()):
        return f"cuda:{torch.cuda.get_device_name(0)}"
    return "cpu"


def onnx_device() -> str:
    runtime: Any = importlib.import_module("onnxruntime")
    providers = [str(name) for name in runtime.get_available_providers()]
    accelerated = [name for name in providers if name != "CPUExecutionProvider"]
    return accelerated[0] if accelerated else "CPUExecutionProvider"


def inference(effect: EffectBase) -> dict[str, str]:
    require(effect)
    if isinstance(effect, MatAnyoneRemove):
        return {"device": torch_device(), "model": "MatAnyone + BiRefNet-portrait first-frame mask",
                "weights": "hf:PeiqingYang/MatAnyone, rembg:birefnet-portrait",
                "backend_version": f"{package_version('matanyone')}, {package_version('torch')}, "
                                   f"{package_version('rembg')}"}
    if isinstance(effect, BiRefNetRemove):
        return {"device": onnx_device(), "model": "BiRefNet-portrait", "weights": "rembg:birefnet-portrait",
                "backend_version": f"{package_version('rembg')}, {package_version('onnxruntime')}"}
    if isinstance(effect, SAM3Track):
        return {"device": "cuda" if torch_device().startswith("cuda") else "cpu", "model": "SAM 3 image model",
                "weights": "hf:facebook/sam3",
                "backend_version": f"{package_version('sam3')}, {package_version('torch')}"}
    if isinstance(effect, AutoReframe) and effect.tracker == "salience":
        return {"device": "cpu", "model": "Canny edge salience", "weights": "none",
                "backend_version": package_version("cv2")}
    if isinstance(effect, PunchIn) and not effect.track_speaker:
        return {"device": "cpu", "model": "frame center", "weights": "none", "backend_version": "none"}
    return {"device": "cpu", "model": "MediaPipe BlazeFace short range", "weights": BLAZEFACE,
            "backend_version": package_version("mediapipe")}


def legacy_background(ctx: EffectContext, color: str) -> tuple[int, int, int, int]:
    rgba = np.rint(ctx.color(color, "background") * 255.0).astype(np.int64).tolist()
    return (int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))


def legacy_context(ctx: EffectContext, streams: dict[str, list[float]]) -> Any:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.render.context import RenderContext, RenderOptions

    if not ctx.frames:
        raise ctx.fail("the clip has no frames")
    fps = float(ctx.fps)
    project = Project(fps=fps, resolution=ctx.size, duration=ctx.frames.stop / fps)
    scratch = Path(tempfile.gettempdir())
    return RenderContext(project=project, options=RenderOptions(output=scratch / "unused.mp4"),
                         markers=MarkerSet(streams=streams), workspace=scratch, cache_dir=scratch)


def legacy_keys(ctx: EffectContext, values: Sequence[float]) -> Any:
    from eks_harness.video.ir.animated import Animated, Keyframe
    from eks_harness.video.ir.time import Seconds

    if len(set(values)) <= 1:
        return Animated[float](root=float(values[0]) if values else 1.0)
    keys = [Keyframe[float](t=Seconds(t=float(index / ctx.fps)), v=float(value))
            for index, value in zip(ctx.frames, values, strict=True)]
    return Animated[float](root=keys)


def legacy_effect(ctx: EffectContext, effect: EffectBase) -> tuple[Any, Any, dict[str, list[float]]]:
    from eks_harness.video.ir import effects as legacy
    from eks_harness.video.plugins.builtin import effects as plugins
    from eks_harness.video.plugins.builtin.effects.punch_in import TRIGGER_STREAM

    if isinstance(effect, MatAnyoneRemove):
        return (plugins.MatAnyoneRemovePlugin(),
                legacy.MatAnyoneRemove(background=legacy_background(ctx, effect.background)), {})
    if isinstance(effect, BiRefNetRemove):
        return (plugins.BiRefNetRemovePlugin(),
                legacy.BiRefNetRemove(background=legacy_background(ctx, effect.background)), {})
    if isinstance(effect, SAM3Track):
        return (plugins.SAM3TrackPlugin(),
                legacy.SAM3Track(prompt=effect.prompt, output=effect.output,
                                 confidence_threshold=effect.confidence_threshold), {})
    if isinstance(effect, AutoReframe):
        return (plugins.AutoReframePlugin(),
                legacy.AutoReframe(target_aspect=effect.target_aspect, tracker=effect.tracker,
                                   smoothing=effect.smoothing), {})
    if isinstance(effect, PunchIn):
        zoom = ctx.floats(effect.zoom, "zoom")
        origin = ctx.base.origin / TICKS_PER_SECOND
        streams: dict[str, list[float]] = {}
        if effect.trigger is not None:
            streams[TRIGGER_STREAM] = sorted(origin + local for local in ctx.times(effect.trigger, "trigger"))
        elif effect.track_speaker and ctx.words:
            streams["word"] = sorted({word.ticks / TICKS_PER_SECOND for words in ctx.words.values()
                                      for word in words})
        model = legacy.PunchIn(track_speaker=effect.track_speaker, zoom=legacy_keys(ctx, zoom.tolist()))
        return plugins.PunchInPlugin(), model, streams
    raise ctx.fail("is not a device-dependent effect", "executor_error")


class LegacyFx:
    def __init__(self, ctx: EffectContext, effect: EffectBase) -> None:
        require(effect)
        self.ctx = ctx
        self.matting = isinstance(effect, MatAnyoneRemove | BiRefNetRemove)
        self.geometric = isinstance(effect, AutoReframe | PunchIn)
        self.background = (ctx.color(getattr(effect, "background", "#00000000"), "background") * 255.0
                           ).astype(np.float32)
        plugin, model, streams = legacy_effect(ctx, effect)
        try:
            self.processor: Any = plugin.open(model, legacy_context(ctx, streams))
        except (ImportError, RuntimeError) as exc:
            raise NodeFailure("resolution_unavailable", f"{effect.kind} cannot start: {exc}; {INSTALL}",
                              detail={"effect": effect.kind}) from exc

    def apply(self, image: Image, frame: EffectFrame) -> Image:
        ctx = self.ctx
        ctx.check(image)
        ctx.position(frame.index)
        if self.geometric and int(image[..., 3].min()) < 255:
            raise ctx.fail("runs on opaque clips only; this frame has transparent pixels")
        bgr = np.ascontiguousarray(image[..., 2::-1])
        result = np.asarray(self.processor.process(bgr, frame.seconds, frame.index), dtype=np.uint8)
        if result.shape != bgr.shape:
            raise ctx.fail(f"the processor returned a {result.shape} frame for a {bgr.shape} one", "executor_error")
        if not self.matting:
            out = np.empty_like(image)
            out[..., :3] = result[..., ::-1]
            out[..., 3] = image[..., 3]
            return out
        matte = getattr(self.processor, "last_alpha", None)
        if matte is None:
            raise ctx.fail("the matting processor reported no alpha matte", "executor_error")
        return self.composite(image, np.asarray(matte, dtype=np.float32))

    def composite(self, image: Image, matte: Pixels) -> Image:
        source = image.astype(np.float32)
        fa = (matte * source[..., 3] / 255.0)[..., None]
        ba = self.background[3] / 255.0
        oa = fa + ba * (1.0 - fa)
        safe = np.where(oa > 0.0, oa, 1.0)
        rgb = np.where(oa > 0.0, (source[..., :3] * fa + self.background[:3] * ba * (1.0 - fa)) / safe, 0.0)
        out = np.empty_like(image)
        out[..., :3] = to_uint8(rgb.astype(np.float32))
        out[..., 3] = to_uint8((oa[..., 0] * 255.0).astype(np.float32))
        return out

    def close(self) -> None:
        self.processor.close()


__all__ = ["ML_KINDS", "LegacyFx", "inference", "require"]
