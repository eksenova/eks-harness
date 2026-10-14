from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog import effects as catalog
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.time import Word
from eks_harness.media.animate import Timebase
from eks_harness.media.edit.fx.base import (
    EffectContext,
    EffectFrame,
    EffectRuntime,
    Image,
)
from eks_harness.media.edit.fx.color import (
    BrightnessFx,
    ChromaKeyFx,
    ColorGradeFx,
    ContrastFx,
    FadeFx,
    FilmGrainFx,
    FlashFx,
    InvertFx,
    LutFx,
    PosterizeFx,
    SaturationFx,
    VignetteFx,
)
from eks_harness.media.edit.fx.geometry import (
    CropFx,
    MirrorFx,
    PanFx,
    RGBSplitFx,
    ShakeFx,
    ZoomFx,
)
from eks_harness.media.edit.fx.ml import ML_KINDS, LegacyFx, inference
from eks_harness.media.edit.fx.overlay import StickerFx, WatermarkFx
from eks_harness.media.edit.fx.stylize import (
    BlurFx,
    DatamoshFx,
    GlitchFx,
    SharpenFx,
    VHSFx,
)
from eks_harness.media.edit.fx.text import CaptionsFx, LowerThirdFx, TextOverlayFx

TIMING_EFFECTS = frozenset({"speed_ramp", "freeze"})
PLUGIN_HOOK = "apply"


def _builtin(ctx: EffectContext, effect: EffectBase) -> EffectRuntime | None:
    match effect:
        case catalog.Brightness():
            return BrightnessFx(ctx, effect)
        case catalog.Contrast():
            return ContrastFx(ctx, effect)
        case catalog.Saturation():
            return SaturationFx(ctx, effect)
        case catalog.Crop():
            return CropFx(ctx, effect)
        case catalog.Mirror():
            return MirrorFx(ctx, effect)
        case catalog.Zoom():
            return ZoomFx(ctx, effect)
        case catalog.Pan():
            return PanFx(ctx, effect)
        case catalog.ChromaKey():
            return ChromaKeyFx(ctx, effect)
        case catalog.ColorGrade():
            return ColorGradeFx(ctx, effect)
        case catalog.Fade():
            return FadeFx(ctx, effect)
        case catalog.Blur():
            return BlurFx(ctx, effect)
        case catalog.Sharpen():
            return SharpenFx(ctx, effect)
        case catalog.Vignette():
            return VignetteFx(ctx, effect)
        case catalog.FilmGrain():
            return FilmGrainFx(ctx, effect)
        case catalog.LUT():
            return LutFx(ctx, effect)
        case catalog.Invert():
            return InvertFx(ctx)
        case catalog.Posterize():
            return PosterizeFx(ctx, effect)
        case catalog.TextOverlay():
            return TextOverlayFx(ctx, effect)
        case catalog.LowerThird():
            return LowerThirdFx(ctx, effect)
        case catalog.Watermark():
            return WatermarkFx(ctx, effect)
        case catalog.StickerOverlay():
            return StickerFx(ctx, effect)
        case catalog.Flash():
            return FlashFx(ctx, effect)
        case catalog.RGBSplit():
            return RGBSplitFx(ctx, effect)
        case catalog.Glitch():
            return GlitchFx(ctx, effect)
        case catalog.Shake():
            return ShakeFx(ctx, effect)
        case catalog.VHS():
            return VHSFx(ctx, effect)
        case catalog.Datamosh():
            return DatamoshFx(ctx, effect)
        case catalog.Captions():
            return CaptionsFx(ctx, effect)
        case _:
            return None


class PluginEffect:
    def __init__(self, ctx: EffectContext, hook: Callable[[Image, EffectFrame, EffectContext], Image]) -> None:
        self.ctx = ctx
        self.hook = hook

    def apply(self, image: Image, frame: EffectFrame) -> Image:
        ctx = self.ctx
        ctx.check(image)
        ctx.position(frame.index)
        out = self.hook(image.copy(), frame, ctx)
        if not hasattr(out, "dtype") or not hasattr(out, "shape"):
            raise ctx.fail(f"{PLUGIN_HOOK}() returned {type(out).__name__}, not an RGBA image", "executor_error")
        ctx.check(out)
        return out

    def close(self) -> None:
        return None


def open_effect(effect: EffectBase, *, base: Timebase, fps: Fraction, frames: range, size: tuple[int, int],
                media: Callable[[JsonValue], Path], words: Mapping[str, Sequence[Word]] | None = None
                ) -> EffectRuntime:
    if effect.kind in TIMING_EFFECTS:
        raise NodeFailure("executor_error", f"{effect.kind} is a timing effect; the edit time map applies it, not the "
                          "frame effects", detail={"effect": effect.kind})
    ctx = EffectContext(effect.kind, base=base, fps=fps, frames=frames, size=size, media=media, words=words)
    if effect.kind in ML_KINDS:
        return LegacyFx(ctx, effect)
    runtime = _builtin(ctx, effect)
    if runtime is not None:
        return runtime
    hook = getattr(effect, PLUGIN_HOOK, None)
    if not callable(hook):
        raise NodeFailure("executor_error", f"effect {effect.kind} has no {PLUGIN_HOOK}(image, frame, ctx) method",
                          detail={"effect": effect.kind})
    return PluginEffect(ctx, cast(Callable[[Image, EffectFrame, EffectContext], Image], hook))


def inference_record(effect: EffectBase) -> dict[str, str] | None:
    if effect.kind not in ML_KINDS:
        return None
    return inference(effect)


__all__ = [
    "TIMING_EFFECTS",
    "EffectContext",
    "EffectFrame",
    "EffectRuntime",
    "inference_record",
    "open_effect",
]
