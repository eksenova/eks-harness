"""Plugin abstract base classes.

These ABCs define the surface every video plugin implements. Concrete
implementations live in separate distributions and are discovered via
``importlib.metadata`` entry points; the registry instantiates them once per
process and re-binds discriminated unions where applicable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from eks_harness.video.ir import EffectIR
    from eks_harness.video.render.ffmpeg_builder import FilterChain


CompileTarget = Literal["ffmpeg_graph", "frame_pipeline"]


class FrameProcessor(ABC):
    """Per-segment frame consumer used by the frame pipeline.

    ``process`` receives a decoded frame, the absolute timestamp in seconds
    and the frame index relative to the segment start, and returns the
    processed frame. ``close`` releases any resources (CUDA contexts, model
    sessions) the processor allocated.

    ``parallel_safe`` declares whether the processor's output for a given
    frame depends only on that frame plus its ``(t, frame_idx)`` - i.e. the
    same input frame and index always yields the same output regardless of
    which frames were processed before it. When every processor in a
    segment's frame tail is ``parallel_safe`` the orchestrator may split the
    segment into contiguous frame ranges and render them concurrently across
    worker processes. Set it to ``False`` when the processor either (a)
    carries cross-frame state that affects output (temporal blends,
    smoothing, motion memory) or (b) loads a heavy ML/GPU model that must
    not be replicated across worker processes.
    """

    parallel_safe: ClassVar[bool] = True

    @abstractmethod
    def process(self, frame: Any, t: float, frame_idx: int) -> Any: ...

    def close(self) -> None:
        return None


class Effect(ABC):
    """Effect plugin contract.

    A plugin owns one IR model class (``model``) and declares which compile
    targets it supports. ``compile_graph`` is called when the orchestrator
    folds the effect into an ffmpeg filter chain; ``open`` is called when the
    effect must run as a frame-pipeline processor.
    """

    model: ClassVar[type[BaseModel]]
    name: ClassVar[str]
    compile_targets: ClassVar[frozenset[CompileTarget]]

    def compile_graph(self, ir: EffectIR, ctx: RenderContext) -> FilterChain:
        raise NotImplementedError(f"{type(self).__name__} does not implement ffmpeg_graph compilation")

    def open(self, ir: EffectIR, ctx: RenderContext) -> FrameProcessor:
        raise NotImplementedError(f"{type(self).__name__} does not implement frame_pipeline execution")

    def prefers_frame_pipeline(self, ir: EffectIR, ctx: RenderContext) -> bool:
        """Return ``True`` when this IR instance must run on the frame pipeline.

        Default: ``False``. Plugins override when a particular IR shape (e.g.
        a curve-driven envelope) cannot be folded into ffmpeg's filter graph.
        """

        return False


class TransitionPlugin(ABC):
    """Transition plugin contract for :class:`PluginTransition` boundaries.

    ``filter_complex`` returns the ffmpeg filter graph that joins the leaving
    clip ``[a]`` and the entering clip ``[b]`` (both already normalised to
    yuv420p with zeroed timestamps) into ``[v]``; ``offset`` is where the
    transition window starts on the leaving clip and ``boundary_beats`` are beat
    times in window-local seconds.
    """

    name: ClassVar[str]

    @abstractmethod
    def filter_complex(self, params: dict[str, Any], duration: float, offset: float,
                       boundary_beats: list[float]) -> str: ...


class Captioner(ABC):
    """Speech-to-text plugin that produces word-level timestamps.

    A captioner takes an audio path, transcribes it, and returns the
    aligned words. The :class:`STTMarkersExtractor` consumes that list to
    build per-word marker streams; on-screen rendering is the
    ``CaptionsPlugin`` effect's responsibility.
    """

    name: ClassVar[str]

    @abstractmethod
    def transcribe(self, audio_path: Path, opts: dict[str, Any] | None = None) -> list[WordToken]: ...

    def register(self) -> bool:
        """Hook called once after instantiation; return ``False`` to opt out.

        Backends with platform-specific runtime requirements (e.g. mlx-whisper
        on Apple Silicon) override this to refuse registration on hardware
        they cannot support, while still being importable everywhere.
        """

        return True


class EasingPlugin(ABC):
    """Custom easing function exposed under a name.

    The IR-level ``PluginEasing`` carries ``name`` and ``params``; the
    plugin's :meth:`evaluate` method returns the eased ``t`` in [0, 1].
    """

    name: ClassVar[str]

    @abstractmethod
    def evaluate(self, t01: float, params: dict[str, Any]) -> float: ...


class MediaProvider(ABC):
    """Resolves a logical media reference to a local path.

    Filesystem paths are resolved to absolute paths; remote URLs are
    downloaded and cached. The provider declares which URI schemes it can
    resolve via :meth:`can_handle`.
    """

    name: ClassVar[str]

    @abstractmethod
    def can_handle(self, uri: str) -> bool: ...

    @abstractmethod
    def resolve(self, uri: str, ctx: RenderContext) -> Path: ...


class Encoder(ABC):
    """Negotiates an ffmpeg encoder + flags for the active platform.

    The orchestrator asks every registered encoder for a priority
    (``probe``); the highest priority wins. Encoders return their ffmpeg
    output args as a list of strings.
    """

    name: ClassVar[str]

    @abstractmethod
    def probe(self) -> int: ...

    @abstractmethod
    def output_args(self, settings: RenderSettings) -> list[str]: ...


class MarkerExtractor(ABC):
    """Extracts markers (beats, words, scenes) from a media source.

    Plugins declare which IR ``MarkerSource.kind`` they handle. The compile
    step calls :meth:`extract` and merges the result into the project-wide
    ``MarkerSet``.
    """

    name: ClassVar[str]
    handles: ClassVar[str]

    @abstractmethod
    def extract(self, source: MarkerSource, ctx: RenderContext) -> MarkerSet: ...


@dataclass(frozen=True)
class MediaRenderRequest:
    """Everything a :class:`MediaRenderer` needs to render one segment's source window.

    Times are seconds. ``source_start``/``source_end`` are the segment's
    resolved ``in``/``out`` on the media's own clock; ``project_start`` is the
    segment's resolved ``start`` on the project timeline. ``speed`` is the
    segment's constant playback speed, or ``None`` when it is animated (the
    rendered clip still covers the source window; the engine applies the speed
    curve on playback). ``markers`` holds every project marker on the project
    timeline: ``streams`` and ``named`` map names to times, ``words`` is a
    list of ``{"text", "t_start", "t_end", "source"}``. ``frame_count`` frames
    are rendered at ``fps``, the first one at ``source_start``.
    """

    segment_id: str
    media: BaseModel
    fps: float
    resolution: tuple[int, int]
    source_start: float
    source_end: float
    project_start: float
    speed: float | None
    frame_count: int
    markers: dict[str, Any]
    work_dir: Path
    output: Path


class MediaRenderer(ABC):
    """Renders a synthetic media kind (3D scenes, procedural generators) to a video file.

    A renderer owns one IR media model (a pydantic model with a unique
    ``kind`` literal). Registering the renderer adds that model to the
    ``MediaSource`` union, so projects can place it on any track. Before
    segments are planned, the orchestrator hands every segment of that kind
    to :meth:`render` and swaps the segment's media for the resulting file;
    from then on it composites, takes effects, speed and transitions exactly
    like a ``VideoFile``. Results are cached under the render cache keyed by
    :meth:`cache_inputs` contents, the media model, the request and
    ``version``.
    """

    name: ClassVar[str]
    model: ClassVar[type[BaseModel]]
    version: ClassVar[str] = "1"
    opt_in: ClassVar[bool] = False
    """When True the project must list the renderer in ``Project.plugins`` before its media kind renders."""

    def check_available(self, ctx: RenderContext) -> None:
        """Fail fast (raise ``RuntimeError`` with an install hint) when an enabled renderer cannot run."""

    def cache_inputs(self, media: BaseModel, ctx: RenderContext) -> list[Path]:
        """Files or directories whose contents invalidate the cached render when they change."""

        return []

    def cache_salt(self, media: BaseModel, ctx: RenderContext) -> str:
        """Extra cache-key material that is not a file (a tool version, for example)."""

        return ""

    @abstractmethod
    def render(self, request: MediaRenderRequest, ctx: RenderContext) -> Path:
        """Render ``request`` to ``request.output`` (an alpha-capable .mov) and return its path."""


if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import MarkerSource
    from eks_harness.video.ir.render_settings import RenderSettings
    from eks_harness.video.plugins.builtin.captioners import WordToken
    from eks_harness.video.render.context import RenderContext

__all__ = [
    "Captioner",
    "CompileTarget",
    "EasingPlugin",
    "Effect",
    "Encoder",
    "FrameProcessor",
    "MarkerExtractor",
    "MediaProvider",
    "MediaRenderRequest",
    "MediaRenderer",
]
