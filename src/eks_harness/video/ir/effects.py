"""Effect IR models.

Each builtin effect declares a pydantic IR class plus its supported
``compile_targets``. The orchestrator inspects the latter to decide whether
to fold the effect into an ffmpeg filter chain, run it on the frame pipeline,
or split the segment between the two.

Plugins extend the discriminated ``Effect`` union by registering a new model
through :func:`register_effect_model`; this is what lets the SDK pick up
freshly installed effect packages without a process restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .animated import Animated
from .curves import Curve
from .time import TimeRef

__all__ = [
    "LUT",
    "VHS",
    "AutoReframe",
    "BiRefNetRemove",
    "Blur",
    "Brightness",
    "Captions",
    "ChromaKey",
    "ColorGrade",
    "Contrast",
    "Crop",
    "Datamosh",
    "Effect",
    "EffectAdapter",
    "EffectIR",
    "Fade",
    "FilmGrain",
    "Flash",
    "Freeze",
    "Glitch",
    "Invert",
    "LowerThird",
    "MatAnyoneRemove",
    "Mirror",
    "Pan",
    "Posterize",
    "PunchIn",
    "RGBSplit",
    "SAM3Track",
    "Saturation",
    "SceneMarkersEffect",
    "Shake",
    "Sharpen",
    "SpeedRamp",
    "StickerOverlay",
    "TextOverlay",
    "Vignette",
    "Watermark",
    "Zoom",
    "all_effect_models",
    "rebuild_effect_union",
    "register_effect_model",
]


class EffectIR(BaseModel):
    """Base class shared by every effect IR model.

    ``compile_targets`` is a class-level attribute consulted by the
    orchestrator to decide whether the effect can fold into an ffmpeg filter
    chain or must run on the frame pipeline.
    """

    model_config = ConfigDict(extra="forbid")
    compile_targets: ClassVar[frozenset[str]] = frozenset()


# --- Color / exposure --------------------------------------------------------


class Brightness(EffectIR):
    kind: Literal["brightness"] = "brightness"
    amount: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class Contrast(EffectIR):
    kind: Literal["contrast"] = "contrast"
    amount: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Saturation(EffectIR):
    kind: Literal["saturation"] = "saturation"
    amount: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


# --- Geometry ----------------------------------------------------------------


class Crop(EffectIR):
    kind: Literal["crop"] = "crop"
    x: Animated[int]
    y: Animated[int]
    w: Animated[int]
    h: Animated[int]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Mirror(EffectIR):
    kind: Literal["mirror"] = "mirror"
    axis: Literal["horizontal", "vertical", "both"] = "horizontal"
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Zoom(EffectIR):
    kind: Literal["zoom"] = "zoom"
    scale: Animated[float]
    cx: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.5))
    cy: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.5))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class Pan(EffectIR):
    kind: Literal["pan"] = "pan"
    dx: Animated[int]
    dy: Animated[int]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


# --- Time ramps --------------------------------------------------------------


class SpeedRamp(EffectIR):
    kind: Literal["speed_ramp"] = "speed_ramp"
    factor: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class Freeze(EffectIR):
    kind: Literal["freeze"] = "freeze"
    at: TimeRef
    hold: float = Field(gt=0)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


# --- Color FX ----------------------------------------------------------------


class ChromaKey(EffectIR):
    kind: Literal["chroma_key"] = "chroma_key"
    color: tuple[int, int, int] = (0, 255, 0)
    similarity: float = 0.1
    blend: float = 0.0
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class ColorGrade(EffectIR):
    kind: Literal["color_grade"] = "color_grade"
    lut_path: Path | None = None
    exposure: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.0))
    temperature: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.0))
    tint: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.0))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Fade(EffectIR):
    kind: Literal["fade"] = "fade"
    direction: Literal["in", "out"]
    duration: float = Field(gt=0)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class Blur(EffectIR):
    kind: Literal["blur"] = "blur"
    radius: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Sharpen(EffectIR):
    kind: Literal["sharpen"] = "sharpen"
    amount: Animated[float]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Vignette(EffectIR):
    kind: Literal["vignette"] = "vignette"
    angle: float = 0.628
    x0: float = 0.5
    y0: float = 0.5
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class FilmGrain(EffectIR):
    kind: Literal["film_grain"] = "film_grain"
    intensity: Animated[float]
    seed: int | None = None
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class LUT(EffectIR):
    kind: Literal["lut"] = "lut"
    path: Path
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})

    @classmethod
    def _expand_user(cls, value: Any) -> Any:
        if isinstance(value, str):
            return str(Path(value).expanduser())
        if isinstance(value, Path):
            return Path(str(value)).expanduser()
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_path(cls, data: Any) -> Any:
        if isinstance(data, dict) and "path" in data:
            data = {**data, "path": cls._expand_user(data["path"])}
        return data


class Invert(EffectIR):
    kind: Literal["invert"] = "invert"
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class Posterize(EffectIR):
    kind: Literal["posterize"] = "posterize"
    levels: Animated[int]
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


# --- Overlays ----------------------------------------------------------------


class TextOverlay(EffectIR):
    kind: Literal["text_overlay"] = "text_overlay"
    text: str
    font: str = "Inter"
    size: int = 48
    color: tuple[int, int, int, int] = (255, 255, 255, 255)
    x: Animated[int] = Field(default_factory=lambda: Animated[int](root=100))
    y: Animated[int] = Field(default_factory=lambda: Animated[int](root=100))
    start: TimeRef | None = None
    duration: float | None = None
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph"})


class LowerThird(EffectIR):
    kind: Literal["lower_third"] = "lower_third"
    title: str
    subtitle: str = ""
    font: str = "Inter"
    title_size: int = 72
    subtitle_size: int = 36
    position: Literal["left", "center", "right"] = "left"
    margin: int = 120
    color: tuple[int, int, int, int] = (255, 255, 255, 255)
    bg_color: tuple[int, int, int, int] = (0, 0, 0, 180)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class Watermark(EffectIR):
    kind: Literal["watermark"] = "watermark"
    path: Path
    x: Animated[int] = Field(default_factory=lambda: Animated[int](root=20))
    y: Animated[int] = Field(default_factory=lambda: Animated[int](root=20))
    opacity: Animated[float] = Field(default_factory=lambda: Animated[float](root=1.0))
    scale: Animated[float] = Field(default_factory=lambda: Animated[float](root=1.0))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


class StickerOverlay(EffectIR):
    kind: Literal["sticker"] = "sticker"
    path: Path
    x: Animated[int] = Field(default_factory=lambda: Animated[int](root=0))
    y: Animated[int] = Field(default_factory=lambda: Animated[int](root=0))
    scale: Animated[float] = Field(default_factory=lambda: Animated[float](root=1.0))
    rotation: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.0))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class Flash(EffectIR):
    """Solid-color flash with envelope.

    ``at`` may be a TimeRef or an Animated[float] of trigger times.
    ``curve`` (when set, typically :class:`BeatPulse`) drives the per-frame
    alpha envelope used by the frame-pipeline composite path.
    """

    kind: Literal["flash"] = "flash"
    at: TimeRef | Animated[float]
    duration: Animated[float]
    color: tuple[int, int, int] = (255, 255, 255)
    curve: Curve | None = None
    compile_targets: ClassVar[frozenset[str]] = frozenset({"ffmpeg_graph", "frame_pipeline"})


# --- Frame-only effects ------------------------------------------------------


class RGBSplit(EffectIR):
    kind: Literal["rgb_split"] = "rgb_split"
    offset_x: Animated[int]
    offset_y: Animated[int] = Field(default_factory=lambda: Animated[int](root=0))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class Glitch(EffectIR):
    kind: Literal["glitch"] = "glitch"
    intensity: Animated[float]
    block_size: int = Field(default=16, gt=0)
    seed: int | None = None
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class Shake(EffectIR):
    kind: Literal["shake"] = "shake"
    intensity: Animated[float]
    freq_hz: float = 6.0
    decay: float = 0.0
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class VHS(EffectIR):
    kind: Literal["vhs"] = "vhs"
    scanlines: bool = True
    chroma_blur: float = 1.5
    noise: float = 0.05
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class Datamosh(EffectIR):
    kind: Literal["datamosh"] = "datamosh"
    intensity: Animated[float] = Field(default_factory=lambda: Animated[float](root=0.5))
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


# --- Captions / ML-driven ----------------------------------------------------


class Captions(EffectIR):
    kind: Literal["captions"] = "captions"
    source: str
    font: str = "Inter"
    size: int = 64
    color: tuple[int, int, int, int] = (255, 255, 255, 255)
    stroke: tuple[int, int, int, int] | None = None
    stroke_width: int = 0
    position: Literal["top", "center", "bottom"] = "bottom"
    margin: int = 120
    style: Literal["tiktok", "subtitle", "kinetic"] = "tiktok"
    word_animation: Literal["pop", "fade", "none"] = "pop"
    max_words_per_card: int = 4
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class MatAnyoneRemove(EffectIR):
    """Background matting. Current backend: ``rembg`` + BiRefNet-portrait
    (per-frame, no temporal propagation). The native MatAnyone 2 integration
    is pending - for now this and :class:`BiRefNetRemove` share the rembg
    backend. Requires ``pip install eks-harness[matting]``.
    """

    kind: Literal["matanyone_remove"] = "matanyone_remove"
    background: tuple[int, int, int, int] = (0, 0, 0, 0)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class BiRefNetRemove(EffectIR):
    """Per-frame BiRefNet matting via ``rembg``. Requires
    ``pip install eks-harness[matting]``.
    """

    kind: Literal["birefnet_remove"] = "birefnet_remove"
    background: tuple[int, int, int, int] = (0, 0, 0, 0)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class SAM3Track(EffectIR):
    """SAM 3 (Meta, 2025) - open-vocabulary segmentation + tracking.

    Lazy-loaded; requires ``pip install eks-harness[sam3]``.
    """

    kind: Literal["sam3_track"] = "sam3_track"
    prompt: str
    output: Literal["mask", "matte", "highlight"] = "mask"
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class AutoReframe(EffectIR):
    kind: Literal["auto_reframe"] = "auto_reframe"
    target_aspect: tuple[int, int] = (9, 16)
    tracker: Literal["face", "salience"] = "face"
    smoothing: float = 0.85
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


class PunchIn(EffectIR):
    kind: Literal["punch_in"] = "punch_in"
    track_speaker: bool = True
    zoom: Animated[float] = Field(default_factory=lambda: Animated[float](root=1.2))
    trigger: TimeRef | None = None
    compile_targets: ClassVar[frozenset[str]] = frozenset({"frame_pipeline"})


# Unused alias so pyflakes does not flag SceneMarkers IR import re-use upstream.
SceneMarkersEffect = None


_BUILTIN_EFFECT_MODELS: tuple[type[EffectIR], ...] = (
    Brightness,
    Contrast,
    Saturation,
    Crop,
    Mirror,
    Zoom,
    Pan,
    SpeedRamp,
    Freeze,
    ChromaKey,
    ColorGrade,
    Fade,
    Blur,
    Sharpen,
    Vignette,
    FilmGrain,
    LUT,
    Invert,
    Posterize,
    TextOverlay,
    LowerThird,
    Watermark,
    StickerOverlay,
    Flash,
    RGBSplit,
    Glitch,
    Shake,
    VHS,
    Datamosh,
    Captions,
    MatAnyoneRemove,
    BiRefNetRemove,
    SAM3Track,
    AutoReframe,
    PunchIn,
)
_EFFECT_MODELS: list[type[EffectIR]] = list(_BUILTIN_EFFECT_MODELS)


def all_effect_models() -> tuple[type[EffectIR], ...]:
    return tuple(_EFFECT_MODELS)


def register_effect_model(model: type[EffectIR]) -> None:
    """Register an effect model so it appears in the discriminated union."""

    if model in _EFFECT_MODELS:
        return
    _EFFECT_MODELS.append(model)
    rebuild_effect_union()


def _build_union(models: list[type[EffectIR]]) -> Any:
    if len(models) == 1:
        return Annotated[models[0], Field(discriminator="kind")]
    return Annotated[
        Union[tuple(models)],  # noqa: UP007
        Field(discriminator="kind"),
    ]


if TYPE_CHECKING:
    # Static type checkers can't follow the runtime-mutated union, so we
    # expose the built-in models here as the visible `Effect` type. Plugin-
    # provided effect IRs still validate at runtime via `EffectAdapter`.
    Effect = Annotated[
        Union[
            Brightness,
            Contrast,
            Saturation,
            Crop,
            Mirror,
            Zoom,
            Pan,
            SpeedRamp,
            Freeze,
            ChromaKey,
            ColorGrade,
            Fade,
            Blur,
            Sharpen,
            Vignette,
            FilmGrain,
            LUT,
            Invert,
            Posterize,
            TextOverlay,
            LowerThird,
            Watermark,
            StickerOverlay,
            Flash,
            RGBSplit,
            Glitch,
            Shake,
            VHS,
            Datamosh,
            Captions,
            MatAnyoneRemove,
            BiRefNetRemove,
            SAM3Track,
            AutoReframe,
            PunchIn,
        ],
        Field(discriminator="kind"),
    ]
else:
    Effect = _build_union(_EFFECT_MODELS)
"""Discriminated union of every registered effect IR model.

Re-bound by :func:`rebuild_effect_union` whenever a plugin registers a new
effect.
"""

EffectAdapter: TypeAdapter[Any] = TypeAdapter(Effect)


def rebuild_effect_union() -> None:
    """Recompute the ``Effect`` union and ``EffectAdapter`` from the registry.

    Callers that cache the *adapter* must either re-resolve it after plugins
    load, or rely on the orchestrator's startup sequence which runs
    :func:`eks_harness.video.plugins.registry.ensure_registry` once before any
    adapter is consumed.
    """

    global Effect, EffectAdapter
    Effect = _build_union(_EFFECT_MODELS)
    EffectAdapter = TypeAdapter(Effect)
    _rebuild_effect_dependents()


def _rebuild_effect_dependents() -> None:
    """Re-point the models that embed ``list[Effect]`` at the new union.

    Pydantic resolves a field's annotation once, when the model class is
    built, so ``Segment.effects`` keeps validating against the union that
    existed at import time. A plugin effect registered later would then fail
    with "Input tag ... does not match". Swap the annotation and force a
    rebuild of every model on the path to ``Project``.
    """

    import sys

    tracks = sys.modules.get("eks_harness.video.ir.tracks")
    if tracks is None:
        return
    segment = tracks.Segment
    segment.model_fields["effects"].annotation = list[Effect]  # type: ignore[valid-type]
    segment.model_rebuild(force=True)
    tracks.Track.model_rebuild(force=True)
    project = sys.modules.get("eks_harness.video.ir.project")
    if project is not None:
        project.Project.model_rebuild(force=True)
