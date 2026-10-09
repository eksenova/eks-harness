"""Media sources backing a segment.

A ``MediaSource`` is purely declarative; the renderer materialises it through
``MediaProvider`` plugins (filesystem / yt-dlp / cards / TTS).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

__all__ = [
    "AudioFile",
    "GeneratedCard",
    "HTMLOverlay",
    "ImageFile",
    "MarkerAnimation",
    "MediaSource",
    "SeparatedStem",
    "Solid",
    "TTSGenerated",
    "VideoFile",
    "all_media_models",
    "register_media_model",
]


def _coerce_to_path(value: str | Path) -> Path:
    return value if isinstance(value, Path) else Path(value)


# Field type is `Path | str` so static checkers (Pylance) accept `path="x.jpg"`
# at call sites; the AfterValidator guarantees the stored value is always a
# `Path` at runtime.
PathLike = Annotated[Path | str, AfterValidator(_coerce_to_path)]


class _MediaBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoFile(_MediaBase):
    kind: Literal["video_file"] = "video_file"
    path: PathLike


class ImageFile(_MediaBase):
    kind: Literal["image_file"] = "image_file"
    path: PathLike


class Solid(_MediaBase):
    """Solid colour fill. Color is RGBA in 0-255."""

    kind: Literal["solid"] = "solid"
    color: tuple[int, int, int, int]


class GeneratedCard(_MediaBase):
    """Text card rendered at compile time."""

    kind: Literal["generated_card"] = "generated_card"
    text: str
    font: str = "DejaVuSans"
    size: int = 64
    fg: tuple[int, int, int, int] = (255, 255, 255, 255)
    bg: tuple[int, int, int, int] = (0, 0, 0, 255)


class AudioFile(_MediaBase):
    kind: Literal["audio_file"] = "audio_file"
    path: PathLike


class TTSGenerated(_MediaBase):
    kind: Literal["tts_generated"] = "tts_generated"
    text: str
    voice: str = "default"
    backend: str = "kokoro"


class SeparatedStem(_MediaBase):
    """Audio source separated via Demucs HTDemucs into vocals/drums/bass/other.

    The renderer runs Demucs once per ``(source path, model)`` pair and caches
    the four stems on disk; the segment's effective audio is the mix of the
    listed ``stems``. When a single stem is requested the cached file is used
    directly; multiple stems are mixed via ffmpeg ``amix``.

    Lazy-loaded; requires ``pip install eks-harness[stems]``.
    """

    kind: Literal["separated_stem"] = "separated_stem"
    source: AudioFile
    stems: list[Literal["vocals", "drums", "bass", "other"]] = Field(min_length=1)
    model: Literal["htdemucs", "htdemucs_ft", "mdx_extra"] = "htdemucs"


class MarkerAnimation(BaseModel):
    """Binds a CSS ``@keyframes`` animation to a marker stream.

    At compile time, the HTML renderer generates one CSS rule per trigger
    time in ``markers.streams[stream]`` that falls inside the segment's
    ``[start, end]`` window. Each rule applies ``animation_name`` to the
    given ``selector`` at ``trigger_t - segment_start + offset_ms / 1000``
    seconds.

    The user's HTML template must declare the corresponding
    ``@keyframes <animation_name> { ... }`` in its own CSS. No JavaScript
    is involved; marker sync is purely declarative.
    """

    model_config = ConfigDict(extra="forbid")

    stream: str
    """Marker stream name, e.g. ``"kick"`` or ``"beat"``."""

    selector: str
    """CSS selector to animate, e.g. ``".flash"`` or ``"#title"``."""

    animation_name: str
    """``@keyframes`` rule name defined in the user's template CSS."""

    duration: float = Field(gt=0)
    """Animation duration in seconds (one-shot, ``forwards`` fill mode)."""

    offset_ms: float = 0.0
    """Per-trigger offset applied to the computed animation-delay."""


class HTMLOverlay(_MediaBase):
    """HTML+CSS template rendered via headless Chromium with frame-
    deterministic virtual-time capture.

    The renderer drives Playwright's headless Chromium, advances the page's
    virtual clock one frame at a time (Replit's
    ``Emulation.setVirtualTimePolicy`` pattern), screenshots each frame,
    and encodes the resulting PNG sequence to mp4 via ffmpeg.

    No JavaScript is executed in the user's template - CSS animations
    only. Marker sync is achieved by baking extra ``@keyframes`` /
    ``animation-delay`` declarations into the page at compile time, one
    rule per marker trigger that falls inside the segment window.

    Lazy-loaded; requires ``pip install eks-harness[html] && playwright
    install chromium``.
    """

    kind: Literal["html_overlay"] = "html_overlay"

    template: PathLike
    """Path to an ``.html`` file (relative to project / cwd)."""

    style_vars: dict[str, str] | None = None
    """CSS custom properties injected at ``:root`` for the template."""

    viewport: tuple[int, int] | None = None
    """``(width, height)`` in pixels; defaults to the project resolution."""

    transparent: bool = True
    """If True, the page background is transparent and the output uses an
    alpha-capable codec so the overlay composites cleanly."""

    marker_animations: list[MarkerAnimation] = Field(default_factory=list)
    """CSS animation bindings driven by project marker streams."""


_BUILTIN_MEDIA_MODELS: tuple[type[BaseModel], ...] = (
    VideoFile,
    ImageFile,
    Solid,
    GeneratedCard,
    AudioFile,
    TTSGenerated,
    SeparatedStem,
    HTMLOverlay,
)
_MEDIA_MODELS: list[type[BaseModel]] = list(_BUILTIN_MEDIA_MODELS)


def all_media_models() -> tuple[type[BaseModel], ...]:
    return tuple(_MEDIA_MODELS)


def _build_media_union(models: list[type[BaseModel]]) -> Any:
    return Annotated[Union[tuple(models)], Field(discriminator="kind")]  # noqa: UP007


if TYPE_CHECKING:
    MediaSource = Annotated[
        VideoFile
        | ImageFile
        | Solid
        | GeneratedCard
        | AudioFile
        | TTSGenerated
        | SeparatedStem
        | HTMLOverlay,
        Field(discriminator="kind"),
    ]
else:
    MediaSource = _build_media_union(_MEDIA_MODELS)
"""Discriminated union of every media model, re-bound by :func:`register_media_model`."""


def register_media_model(model: type[BaseModel]) -> None:
    """Add a plugin media model (unique ``kind`` literal) to the ``MediaSource`` union.

    Media renderer plugins call this when they register. Pydantic resolves a
    field's annotation once, so the models embedding ``MediaSource`` on the
    path to ``Project`` are re-pointed at the new union and rebuilt.
    """

    if model in _MEDIA_MODELS:
        return
    _MEDIA_MODELS.append(model)
    rebuild_media_union()


def rebuild_media_union() -> None:
    import sys

    global MediaSource
    MediaSource = _build_media_union(_MEDIA_MODELS)
    tracks = sys.modules.get("eks_harness.video.ir.tracks")
    if tracks is None:
        return
    tracks.Segment.model_fields["media"].annotation = MediaSource
    tracks.Segment.model_rebuild(force=True)
    tracks.Track.model_rebuild(force=True)
    project = sys.modules.get("eks_harness.video.ir.project")
    if project is not None:
        project.Project.model_rebuild(force=True)
