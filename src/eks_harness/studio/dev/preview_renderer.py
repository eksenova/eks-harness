"""Short-clip preview renders into a single mp4.

Builds an in-memory :class:`eks_harness.video.ir.Project` containing a single video
track and a single segment over the cached animated sample (or two
segments joined by a transition, when previewing a transition). Drives
:class:`eks_harness.video.render.Renderer` programmatically to a temp mp4, then
remuxes that mp4 to the cache path with ``+faststart`` so the catalog
tile's ``<video>`` element streams the moov atom first.

The output is a 320x180 @ 30 fps, 2-second, h264 + yuv420p, audio-less
mp4. We deliberately keep a curated set of "renderable" effect defaults -
many builtin effects depend on infrastructure (marker streams, ML models,
STT sources) or simply have no renderer plugin registered, both of which
disqualify them from a cheap preview render. Effects we cannot synthesise
safely raise :class:`PreviewUnavailableError` so the caller can surface a
clear error message instead of bubbling a raw ffmpeg or lookup error.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from eks_harness.video.ir import (
    BeatFlash,
    BeatGlitch,
    BlurThrough,
    Crossfade,
    Cut,
    DipToBlack,
    DipToWhite,
    Dissolve,
    EffectIR,
    FadeGrays,
    FilmBurn,
    GlitchTransition,
    Iris,
    LightLeak,
    LumaWipe,
    Pixelize,
    PluginTransition,
    Project,
    Push,
    Radial,
    RGBShiftWipe,
    Seconds,
    Segment,
    Slide,
    Track,
    VideoFile,
    WhipPan,
    Wipe,
    ZoomTransition,
    all_effect_models,
)
from eks_harness.video.ir.animated import Animated
from eks_harness.video.ir.markers import STTMarkers
from eks_harness.video.render import RenderOptions, Renderer

from .preview_samples import portrait_sample_path, sample_video_pair, sample_video_path

# Effects whose preview benefits from (or requires) a face-shaped subject in
# frame: AutoReframe centres a crop on the detected face, PunchIn zooms onto
# it. The colour-box sample has no face for these to lock onto, so we route
# them at the bundled portrait clip instead.
_PORTRAIT_EFFECTS: frozenset[str] = frozenset({"AutoReframe", "PunchIn", "SAM3Track", "MatAnyoneRemove", "BiRefNetRemove"})

# Name of the synthetic STTMarkers source injected for the Captions preview.
# The Captions plugin matches each ``WordHit.source`` against the IR's
# ``source`` field, so this name has to stay consistent across the Project's
# declared marker source, the ``Captions`` IR, and the synthetic WordHits
# emitted by the in-process extractor below.
_CAPTIONS_PREVIEW_SOURCE_NAME = "captions_stt"
_CAPTIONS_PREVIEW_WORDS: tuple[str, ...] = ("VIDEO", "DSL", "PREVIEW", "CAPTIONS")

__all__ = [
    "PreviewUnavailableError",
    "default_params_for_effect",
    "non_previewable_reason",
    "render_effect_preview",
    "render_transition_preview",
]


_SAMPLE_DURATION_SECONDS = 2.0
_SAMPLE_FPS = 30
# Match the sample clip dimensions so the project's scale step is a no-op
# and the output mp4 is exactly 320x180.
_SAMPLE_SIZE: tuple[int, int] = (320, 180)

# Bundled assets used by curated preview defaults that need a real file
# path on disk (LUT .cube, Watermark / StickerOverlay PNGs).
_PREVIEW_ASSETS_DIR = Path(__file__).parent / "preview_assets"


class PreviewUnavailableError(RuntimeError):
    """Raised when an effect / transition cannot be safely previewed."""


# Curated default parameter dicts per effect class name. The list is
# intersected with the actual ``eks_harness.video.plugins.registry_snapshot()`` at
# call time so an effect IR with no registered plugin is rejected before
# the renderer ever runs - there is no point queueing a render that we
# know will explode mid-pipeline.
#
# Param values are intentionally mild: previews need to be a recognisable
# rendition of the effect, not a stress test. ``Crop`` stays inside the
# 320x180 frame; ``ColorGrade`` keeps ``temperature``/``tint`` small
# enough that ``colorbalance`` doesn't reject the args.
_EFFECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "Brightness": {"amount": 0.3},
    "Contrast": {"amount": 1.4},
    "Saturation": {"amount": 1.5},
    "Crop": {"x": 20, "y": 10, "w": 280, "h": 160},
    "Mirror": {"axis": "horizontal"},
    "Pan": {"dx": 30, "dy": 0},
    "ChromaKey": {"color": (0, 255, 0), "similarity": 0.1, "blend": 0.05},
    "ColorGrade": {"exposure": 0.2, "temperature": 0.0, "tint": 0.0},
    "Fade": {"direction": "in", "duration": 0.5},
    "Flash": {
        "at": Seconds(t=1.0),
        "duration": 0.2,
        "color": (255, 255, 255),
    },
    "RGBSplit": {"offset_x": 6, "offset_y": 0},
    "Glitch": {"intensity": 0.6, "block_size": 16, "seed": 7},
    "Shake": {"intensity": 0.5, "freq_hz": 8.0, "decay": 0.0},
    "VHS": {"scanlines": True, "chroma_blur": 1.5, "noise": 0.05},
    "Datamosh": {"intensity": 0.5},
    # Newly previewable - Animated[*] fields get fresh Animated wrappers so the
    # preview never trips pydantic when the IR resolves the field type.
    "Blur": {"radius": Animated[float](root=4.0)},
    "Sharpen": {"amount": Animated[float](root=1.0)},
    "Invert": {},
    "Posterize": {"levels": Animated[int](root=5)},
    "Vignette": {"angle": 0.9, "x0": 0.5, "y0": 0.5},
    "FilmGrain": {"intensity": Animated[float](root=12.0), "seed": 7},
    "TextOverlay": {
        "text": "PREVIEW",
        "size": 32,
        "color": (255, 255, 255, 255),
        "x": Animated[int](root=16),
        "y": Animated[int](root=32),
    },
    "LowerThird": {
        "title": "PREVIEW",
        "subtitle": "EFFECT",
        "title_size": 24,
        "subtitle_size": 16,
        "position": "left",
        "margin": 12,
    },
    # Six effects whose plugins were missing or unregistered until A1. The
    # bundled `preview_assets/` ships a tiny identity LUT and a 64x64 sticker
    # so the previews render without requiring user-provided files.
    "Freeze": {"at": Seconds(t=_SAMPLE_DURATION_SECONDS), "hold": 0.5},
    "Zoom": {
        "scale": Animated[float](root=1.6),
        "cx": Animated[float](root=0.5),
        "cy": Animated[float](root=0.5),
    },
    "SpeedRamp": {"factor": Animated[float](root=1.5)},
    "LUT": {"path": str(_PREVIEW_ASSETS_DIR / "sample.cube")},
    "Watermark": {
        "path": str(_PREVIEW_ASSETS_DIR / "sticker.png"),
        "x": Animated[int](root=240),
        "y": Animated[int](root=12),
        "opacity": Animated[float](root=0.85),
        "scale": Animated[float](root=1.0),
    },
    "StickerOverlay": {
        "path": str(_PREVIEW_ASSETS_DIR / "sticker.png"),
        "x": Animated[int](root=12),
        "y": Animated[int](root=12),
        "scale": Animated[float](root=1.0),
        "rotation": Animated[float](root=0.0),
    },
    # Background matting effects. The shared rembg + BiRefNet-portrait
    # backend doesn't need configuration; the IR's transparent-background
    # default is fine for a capability demo (the sample is a SMPTE-style
    # pattern, so the mask will be mostly empty - that's the point).
    "MatAnyoneRemove": {"background": (0, 0, 0, 0)},
    "BiRefNetRemove": {"background": (0, 0, 0, 0)},
    # SAM 3 open-vocabulary segmentation. Routed to the portrait sample
    # (see _PORTRAIT_EFFECTS) so the "person" concept prompt has a subject
    # to segment.
    "SAM3Track": {"prompt": "person", "output": "matte", "confidence_threshold": 0.3},
    # Captions reads per-word timings from a declared ``STTMarkers`` source.
    # We declare the source on the preview project (see
    # ``_build_effect_project``) and short-circuit the real STT extractor
    # with a synthetic one (see ``_synthetic_captions_markers_patch``), so
    # the only IR-side wiring needed is the matching ``source`` name plus a
    # readable style. ``stroke`` is kept default to let the tiktok-style
    # outline draw cleanly at 32 px against the SMPTE sample.
    "Captions": {
        "source": _CAPTIONS_PREVIEW_SOURCE_NAME,
        "style": "tiktok",
        "size": 32,
        "color": (255, 255, 255, 255),
        "position": "bottom",
        "margin": 24,
        "max_words_per_card": 2,
        "word_animation": "pop",
    },
    # Face-tracking previews use the bundled portrait clip via
    # ``_sample_for_effect``. The portrait has a centred figure that
    # gently oscillates ±20 px, so the tracker has motion to react to.
    # ``tracker="face"`` exercises the production mediapipe path; if
    # mediapipe is missing or the detector returns no hits, the tracker
    # gracefully falls back to the frame centre and the crop still
    # renders - no preview failure.
    "AutoReframe": {
        "target_aspect": (9, 16),
        "tracker": "face",
        "smoothing": 0.7,
    },
    # PunchIn defaults to ``track_speaker=True`` which would consult a
    # marker stream that the preview project does not declare; that path
    # is internally guarded (no markers => no triggers => static frame),
    # so we explicitly disable speaker tracking and supply a trigger time
    # so the zoom actually fires mid-clip. ``zoom`` is a fresh Animated
    # to dodge pydantic's reuse check.
    "PunchIn": {
        "track_speaker": False,
        "zoom": Animated[float](root=1.5),
        "trigger": Seconds(t=0.5),
    },
}


# Effects whose IR is registered but cannot be safely synthesised inside the
# fast 320x180 preview pipeline. The reason is surfaced to the UI so tiles
# can show a tooltip instead of a stack trace.
_NON_PREVIEWABLE_REASONS: dict[str, str] = {}


def non_previewable_reason(name: str) -> str | None:
    """Return the curated non-previewable reason for ``name``, or ``None``.

    The catalog tile uses this to populate ``previewable_reason`` so the UI
    can show why a tile is dimmed.
    """

    return _NON_PREVIEWABLE_REASONS.get(name)


_REGISTERED_KINDS_CACHE: set[str] | None = None


def _registered_effect_kinds() -> set[str]:
    """Return the set of registered effect ``kind`` strings (memoized).

    ``registry_snapshot()`` rebuilds its dict on every call (~50ms), and the
    catalog enumerates 35+ effects calling this twice each - so without the
    cache, ``list_effects`` spent ~3.5s here alone, stalling the dashboard.
    The registered-plugin set is stable after startup.
    """

    global _REGISTERED_KINDS_CACHE
    if _REGISTERED_KINDS_CACHE is not None:
        return _REGISTERED_KINDS_CACHE

    result: set[str] = set()
    try:
        from eks_harness.video.plugins import ensure_registry, registry_snapshot

        ensure_registry()
        snap = registry_snapshot()
        effects = snap.get("effects") if isinstance(snap, dict) else None
        if isinstance(effects, dict):
            result = set(effects.keys())
        elif isinstance(effects, (list, tuple, set)):
            result = set(effects)
    except Exception:  # pragma: no cover - registry import failure is fatal elsewhere
        pass

    _REGISTERED_KINDS_CACHE = result
    return result


def _effect_class_kind(name: str) -> str | None:
    for cls in all_effect_models():
        if cls.__name__ == name:
            # ``kind`` is a Literal default on every effect IR model.
            try:
                return cls.model_fields["kind"].default
            except Exception:  # pragma: no cover - defensive
                return None
    return None


def default_params_for_effect(name: str) -> dict[str, Any]:
    """Return the curated default param dict for an effect class.

    Raises :class:`PreviewUnavailableError` when the effect either lacks
    a curated default (e.g. it needs marker streams / ML inference) or
    has no registered renderer plugin (the IR exists but nothing knows
    how to compile it).
    """

    if name not in _EFFECT_DEFAULTS:
        reason = _NON_PREVIEWABLE_REASONS.get(
            name, f"{name} cannot be previewed without external inputs"
        )
        raise PreviewUnavailableError(reason)
    kind = _effect_class_kind(name)
    registered = _registered_effect_kinds()
    if kind is not None and registered and kind not in registered:
        raise PreviewUnavailableError(
            f"{name} has no registered renderer plugin; preview unavailable"
        )
    # Return a fresh dict so callers can mutate it.
    return dict(_EFFECT_DEFAULTS[name])


def _resolve_effect_cls(name: str) -> type[EffectIR]:
    for cls in all_effect_models():
        if cls.__name__ == name:
            return cls
    raise PreviewUnavailableError(f"effect {name!r} not found")


def _build_effect(name: str, params: dict[str, Any] | None) -> EffectIR:
    cls = _resolve_effect_cls(name)
    defaults = default_params_for_effect(name)
    if params:
        defaults.update(params)
    # ``Animated`` fields tolerate bare scalars at validation time, so we can
    # pass the curated dict in unchanged.
    return cls(**defaults)


def _resolve_transition_cls(kind: str) -> type:
    mapping = {
        "Cut": Cut,
        "Crossfade": Crossfade,
        "DipToBlack": DipToBlack,
        "DipToWhite": DipToWhite,
        "Wipe": Wipe,
        "Slide": Slide,
        "Push": Push,
        "GlitchTransition": GlitchTransition,
        "ZoomTransition": ZoomTransition,
        "Dissolve": Dissolve,
        "BlurThrough": BlurThrough,
        "Iris": Iris,
        "Radial": Radial,
        "Pixelize": Pixelize,
        "FadeGrays": FadeGrays,
        "LumaWipe": LumaWipe,
        "RGBShiftWipe": RGBShiftWipe,
        "WhipPan": WhipPan,
        "LightLeak": LightLeak,
        "FilmBurn": FilmBurn,
        "BeatFlash": BeatFlash,
        "BeatGlitch": BeatGlitch,
        "PluginTransition": PluginTransition,
    }
    if kind not in mapping:
        raise PreviewUnavailableError(f"transition {kind!r} not supported for preview")
    return mapping[kind]


# Transition classes that take a ``duration`` and benefit from a default in the
# preview (so the catalog tile renders without the caller supplying params).
_DURATION_TRANSITIONS = (
    Crossfade,
    DipToBlack,
    DipToWhite,
    Wipe,
    Slide,
    Push,
    GlitchTransition,
    ZoomTransition,
    Dissolve,
    BlurThrough,
    Iris,
    Radial,
    Pixelize,
    FadeGrays,
    LumaWipe,
    RGBShiftWipe,
    WhipPan,
    LightLeak,
    FilmBurn,
    BeatFlash,
    BeatGlitch,
)


def _build_transition(kind: str, params: dict[str, Any] | None):
    cls = _resolve_transition_cls(kind)
    merged: dict[str, Any] = {}
    if issubclass(cls, _DURATION_TRANSITIONS):
        merged["duration"] = 0.5
    elif cls is PluginTransition:
        merged["name"] = "preview_placeholder"
    if params:
        merged.update(params)
    return cls(**merged)


def _build_effect_project(effect: EffectIR, media_path: Path) -> Project:
    """Single video track + one segment over ``media_path``.

    For the ``Captions`` effect we additionally declare a synthetic
    ``STTMarkers`` source on the project so the renderer's marker pass has
    something to dispatch on. The real STT plumbing is intercepted by
    :func:`_synthetic_captions_markers_patch` during the render - the
    declared source is just a handle to keep the renderer's accounting
    consistent.
    """

    markers: list[Any] = []
    if type(effect).__name__ == "Captions":
        markers.append(
            STTMarkers(
                name=_CAPTIONS_PREVIEW_SOURCE_NAME,
                # ``source`` is normally an audio track / asset id. The
                # preview clip has no audio track, but the synthetic
                # extractor below ignores this field entirely, so any
                # non-empty placeholder is fine.
                source="preview://captions",
            )
        )

    return Project(
        fps=_SAMPLE_FPS,
        resolution=_SAMPLE_SIZE,
        duration=_SAMPLE_DURATION_SECONDS,
        markers=markers,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="seg0",
                        start=Seconds(t=0.0),
                        media=VideoFile(path=media_path),  # type: ignore[arg-type]
                        in_=Seconds(t=0.0),
                        out=Seconds(t=_SAMPLE_DURATION_SECONDS),
                        effects=[effect],
                    ),
                ],
            )
        ],
    )


def _build_synthetic_caption_markerset() -> Any:
    """Build a :class:`MarkerSet` containing placeholder caption words.

    Words are distributed evenly across the 2-second sample so multiple
    cards are visible across the clip - `max_words_per_card=2` in the
    Captions default gives two cards of two words each, both of which fall
    inside the sample window with comfortable overlap for the active-word
    highlight to actually render mid-frame.
    """

    from eks_harness.video.compile.markers import MarkerSet, WordHit

    out = MarkerSet()
    words: list[WordHit] = []
    times: list[float] = []
    for i, text in enumerate(_CAPTIONS_PREVIEW_WORDS):
        t_start = 0.1 + i * 0.45
        t_end = min(_SAMPLE_DURATION_SECONDS - 0.01, t_start + 0.4)
        words.append(
            WordHit(
                text=text,
                t_start=t_start,
                t_end=t_end,
                source=_CAPTIONS_PREVIEW_SOURCE_NAME,
            )
        )
        times.append(t_start)
    out.words = words
    out.named[_CAPTIONS_PREVIEW_SOURCE_NAME] = list(times)
    out.streams["word"] = list(times)
    return out


class _SyntheticSTTExtractor:
    """In-process replacement for the real STT marker extractor.

    Returns a fixed :class:`MarkerSet` keyed to ``_CAPTIONS_PREVIEW_SOURCE_NAME``
    without touching faster-whisper, the audio path, or the marker cache.
    Swapped into the registry by :func:`_synthetic_captions_markers_patch`
    for the duration of a single ``Renderer.render`` call.
    """

    name = "stt_markers"
    handles = "stt_markers"

    def extract(self, source: Any, ctx: Any) -> Any:  # noqa: ARG002
        return _build_synthetic_caption_markerset()


class _synthetic_captions_markers_patch:
    """Context manager that swaps the registered ``stt_markers`` extractor.

    The video plugin registry keys extractors by ``plugin.name``; the
    builtin STT extractor registers itself under ``"stt_markers"``. We
    overwrite that slot with :class:`_SyntheticSTTExtractor` (also named
    ``"stt_markers"``) so :func:`resolve_marker_extractor` returns the
    synthetic one for the project's declared STTMarkers source.

    :func:`eks_harness.video.plugins.ensure_registry` loads the plugins once
    per process, so the swap holds for the whole render. The prior extractor
    is restored on exit so other previews in the same process keep working
    against the real one.
    """

    def __init__(self) -> None:
        self._prev: Any = None
        self._had_prev = False

    def __enter__(self) -> "_synthetic_captions_markers_patch":
        from eks_harness.video.plugins import registry as _registry

        extractors = _registry._MARKER_EXTRACTORS  # type: ignore[attr-defined]
        if "stt_markers" in extractors:
            self._prev = extractors["stt_markers"]
            self._had_prev = True
        extractors["stt_markers"] = _SyntheticSTTExtractor()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        from eks_harness.video.plugins import registry as _registry

        extractors = _registry._MARKER_EXTRACTORS  # type: ignore[attr-defined]
        if self._had_prev:
            extractors["stt_markers"] = self._prev
        else:
            extractors.pop("stt_markers", None)


def _build_transition_project(transition: Any, media_a: Path, media_b: Path) -> Project:
    """Two segments back-to-back joined by ``transition``."""

    half = _SAMPLE_DURATION_SECONDS / 2.0
    return Project(
        fps=_SAMPLE_FPS,
        resolution=_SAMPLE_SIZE,
        duration=_SAMPLE_DURATION_SECONDS,
        tracks=[
            Track(
                name="main",
                segments=[
                    Segment(
                        id="seg0",
                        start=Seconds(t=0.0),
                        media=VideoFile(path=media_a),  # type: ignore[arg-type]
                        in_=Seconds(t=0.0),
                        out=Seconds(t=half),
                        transition_out=transition,
                    ),
                    Segment(
                        id="seg1",
                        start=Seconds(t=half),
                        media=VideoFile(path=media_b),  # type: ignore[arg-type]
                        in_=Seconds(t=0.0),
                        out=Seconds(t=half),
                        transition_in=transition,
                    ),
                ],
            )
        ],
    )


def _sample_for_effect(effect_name: str) -> Path:
    """Pick the right sample clip for ``effect_name``.

    Face-tracking effects need a face to lock onto; everything else uses
    the animated colour-box sample where intensity changes are obvious.
    """

    if effect_name in _PORTRAIT_EFFECTS:
        return portrait_sample_path()
    return sample_video_path()


def _resolve_ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg not found on PATH")
    return binary


async def _finalise_mp4(*, source_mp4: Path, out_path: Path) -> None:
    """Remux ``source_mp4`` to ``out_path`` with ``+faststart``.

    The renderer emits a perfectly valid 320x180 yuv420p h264 mp4 already
    - we just want the moov atom moved to the head so the browser can
    start playback before the whole file lands. A stream-copy remux is
    near-instant (no re-encode) and shaves the file size by a few bytes.
    We write to a sibling temp path and ``os.replace`` so partial writes
    never end up at the cache key.
    """

    binary = _resolve_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_name(f".{out_path.name}.tmp{out_path.suffix}")
    args = [
        binary,
        "-y",
        "-i",
        str(source_mp4),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-an",
        str(tmp_out),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"ffmpeg remux failed (rc={proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace')[-400:]}"
        )
    os.replace(tmp_out, out_path)


def _run_renderer(project: Project, output_path: Path) -> Path:
    """Run :class:`Renderer.render` in a fresh temp workspace.

    Loads the video plugins so effect IR -> plugin
    lookups don't ``LookupError`` when this is the first render in the
    process (the WS handler path doesn't always trigger entry-point
    discovery on its own).
    """

    try:
        from eks_harness.video.plugins import ensure_registry

        ensure_registry()
    except Exception:  # pragma: no cover - defensive
        pass

    workspace = Path(tempfile.mkdtemp(prefix="eks-preview-"))
    options = RenderOptions(
        output=output_path,
        mode="preview",
        workspace=workspace,
        cache_dir=workspace / "cache",
    )
    renderer = Renderer(project, options)
    return renderer.render(output_path)


async def render_effect_preview(
    effect_name: str,
    params: dict[str, Any] | None = None,
    *,
    out_path: Path,
) -> Path:
    """Render a short mp4 showing ``effect_name`` applied to the sample clip.

    ``out_path`` must end in ``.mp4``. Returns ``out_path`` on success.
    Raises on any failure - callers wrap the exception into an error
    reply with stderr context.
    """

    sample = _sample_for_effect(effect_name)
    effect = _build_effect(effect_name, params)
    project = _build_effect_project(effect, sample)

    tmp_dir = Path(tempfile.mkdtemp(prefix="eks-preview-eff-"))
    tmp_mp4 = tmp_dir / "out.mp4"

    def _run() -> Path:
        # The Captions preview needs the synthetic STT extractor swapped in
        # for the lifetime of the render so the registered ``stt_markers``
        # extractor never tries to transcribe the (silent) sample clip.
        # Plugin loading MUST run before the patch enters, because loading
        # registers the real STT extractor under the same ``"stt_markers"``
        # slot and would otherwise clobber our synthetic one mid-render.
        if effect_name == "Captions":
            try:
                from eks_harness.video.plugins import ensure_registry

                ensure_registry()
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                from eks_harness.video.render.orchestrator import _ensure_plugins_loaded

                _ensure_plugins_loaded()
            except Exception:  # pragma: no cover - defensive
                pass
            with _synthetic_captions_markers_patch():
                return _run_renderer(project, tmp_mp4)
        return _run_renderer(project, tmp_mp4)

    try:
        # Renderer.render runs synchronously and shells out to ffmpeg
        # internally; offload to a thread so we don't block the event loop.
        await asyncio.to_thread(_run)
        await _finalise_mp4(source_mp4=tmp_mp4, out_path=out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out_path


async def render_transition_preview(
    transition_kind: str,
    params: dict[str, Any] | None = None,
    *,
    out_path: Path,
) -> Path:
    """Render a short mp4 showing ``transition_kind`` between two clips."""

    sample_a, sample_b = sample_video_pair()
    transition = _build_transition(transition_kind, params)
    project = _build_transition_project(transition, sample_a, sample_b)

    tmp_dir = Path(tempfile.mkdtemp(prefix="eks-preview-tr-"))
    tmp_mp4 = tmp_dir / "out.mp4"
    try:
        await asyncio.to_thread(_run_renderer, project, tmp_mp4)
        await _finalise_mp4(source_mp4=tmp_mp4, out_path=out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out_path
