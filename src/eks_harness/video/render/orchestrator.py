"""Top-level render orchestrator.

Phase 2 introduces the hybrid dispatch: each segment's effect chain is
partitioned into a leading run that fits ffmpeg's filter graph and a tail
that has to run on the frame pipeline. Pure graph-only segments still take
the fast graph-only path; segments with any frame-pipeline tail get
rendered through :class:`FramePipeline` with the graph prefix folded into
the decode-side ffmpeg invocation.

Stages (per plan §5):

1. :func:`extract_markers` - runs registered marker extractors.
2. ``time_resolve`` / ``animated_resolve`` - done lazily inside per-segment
   compilation so we only pay for what we render.
3. Per-segment partition - splits effects at the first non-graph effect.
4. Dispatch - graph-only ffmpeg or hybrid ffmpeg+frame-pipeline rendering.
5. :func:`mux_segments` - concatenates segment outputs into the deliverable.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import tempfile
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.effects import EffectIR
from eks_harness.video.ir.media import (
    AudioFile,
    GeneratedCard,
    HTMLOverlay,
    ImageFile,
    Solid,
    TTSGenerated,
    VideoFile,
)
from eks_harness.video.ir.tracks import Segment, Track
from eks_harness.video.ir.transitions import BeatFlash, BeatGlitch
from eks_harness.video.plugins.manager import get_plugin_manager
from eks_harness.video.plugins.registry import ensure_registry

from .audio import render_audio_stem
from .cache import SegmentCache
from .context import RenderContext, RenderOptions
from . import hwaccel
from .ffmpeg_builder import FilterChain, scale
from .frame_pipeline import FramePipeline, FramePipelineSpec
from .mux import mux_segments
from .progress import (
    NoopProgressReporter,
    ProgressReporter,
    step,
    substep,
)
from .materialize import check_enabled_renderers, materialize_media
from .subprocess_runner import run_ffmpeg
from .transitions import assemble_transitions, has_xfade_transitions

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.plugins.base import Effect as EffectPlugin
    from eks_harness.video.plugins.base import FrameProcessor

_LOG = logging.getLogger(__name__)

_PLUGINS_LOADED = False


def _ensure_plugins_loaded() -> None:
    """Discover entry-point plugins exactly once per process.

    Doing this *before* any IR resolves the discriminated ``EffectAdapter``
    avoids the stale-adapter trap noted in Phase 1: callers that cached the
    adapter before plugin load would otherwise silently miss freshly
    registered effect variants.
    """

    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    ensure_registry()
    _PLUGINS_LOADED = True


@dataclass
class _MarkerExtractCtx:
    """Minimal context handed to marker extractors before the full RenderContext exists.

    Carries just enough state (project + workspace + cache_dir) for
    extractors to resolve marker sources like ``source="audio_tracks[0]"``
    and to read/write their per-source marker cache. ``extra`` mirrors the
    full :class:`RenderContext` so reporters and other cross-cutting state
    can ride along.
    """

    project: "Project"
    workspace: Path
    cache_dir: Path | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class _SegmentPlan:
    segment: Segment
    base_chain: FilterChain
    duration: float
    media_path: Path | None
    graph_prefix: list[EffectIR]
    frame_tail: list[EffectIR]


class Renderer:
    """End-to-end renderer for a :class:`~eks_harness.video.ir.project.Project`."""

    def __init__(self, project: Project, options: RenderOptions) -> None:
        _ensure_plugins_loaded()
        self.project = project
        self.options = options

    def render(self, output: Path | None = None) -> Path:
        target = Path(output) if output is not None else self.options.output
        reporter: ProgressReporter = (
            self.options.progress_reporter
            if self.options.progress_reporter is not None
            else NoopProgressReporter()
        )

        with step(reporter, "load_project", index=0):
            workspace = self._resolve_workspace()
            cache_dir = self.options.cache_dir or (workspace / "cache")
            cache = SegmentCache(cache_dir)
            from eks_harness.video.compile.markers import MarkerSet as _EmptyMarkers

            check_enabled_renderers(self.project, RenderContext(
                project=self.project, options=self.options, markers=_EmptyMarkers(), workspace=workspace,
                cache_dir=cache_dir))

        marker_ctx = _MarkerExtractCtx(
            project=self.project, workspace=workspace, cache_dir=cache_dir
        )
        with step(reporter, "extract_markers", index=1):
            markers = self._extract_markers_with_progress(marker_ctx, reporter)

        ctx = RenderContext(
            project=self.project,
            options=self.options,
            markers=markers,
            workspace=workspace,
            cache_dir=cache_dir,
        )
        ctx.extra["progress_reporter"] = reporter

        with step(reporter, "plan_segments", index=2):
            with substep(reporter, step_name="plan_segments", name="materialize_media", kind="phase"):
                self.project = materialize_media(self.project, ctx)
                ctx.project = self.project
            get_plugin_manager().hook.before_render(project=self.project, ctx=ctx)
            track = self._select_primary_video_track()
            plans = [self._plan_segment(segment, ctx, markers) for segment in track.segments]

        total_frames = sum(_frame_count(plan, self.project.fps) for plan in plans)
        with step(
            reporter,
            "render_segments",
            index=3,
            extra={"frame_total": total_frames, "segment_count": len(plans)},
        ):
            segment_outputs = self._render_planned_segments(plans, ctx, cache, reporter)

        target.parent.mkdir(parents=True, exist_ok=True)
        audio_stem_path: Path | None = None
        if self.project.audio_tracks:
            with step(reporter, "mux_audio", index=4):
                audio_stem_path = render_audio_stem(
                    self.project,
                    markers,
                    workspace / "audio_stem.m4a",
                    ffmpeg_binary=self.options.ffmpeg_binary,
                    cache_dir=workspace,
                )
        else:
            with step(reporter, "mux_audio", index=4, extra={"skipped": True}):
                pass

        with step(reporter, "finalize", index=5):
            track_segments = track.segments
            if has_xfade_transitions(track_segments):
                with substep(
                    reporter,
                    step_name="finalize",
                    name="assemble_transitions",
                    kind="phase",
                ) as detail:
                    final_segment_paths = assemble_transitions(
                        segment_outputs,
                        track_segments,
                        [plan.duration for plan in plans],
                        workspace=workspace,
                        fps=float(self.project.fps),
                        binary=self.options.ffmpeg_binary,
                        boundary_beats=self._boundary_beats(track_segments, markers),
                    )
                    detail["input_count"] = len(segment_outputs)
                    detail["output_count"] = len(final_segment_paths)
            else:
                final_segment_paths = list(segment_outputs)
            overlays = [t for t in self._overlay_tracks(track) if t.segments]
            if not overlays:
                mux_segments(
                    final_segment_paths,
                    target,
                    audio_path=audio_stem_path,
                    binary=self.options.ffmpeg_binary,
                )
            else:
                self._composite(final_segment_paths, overlays, target, audio_stem_path, ctx, markers, reporter)
            cache.close()
        return target

    def _overlay_tracks(self, base: Track) -> list[Track]:
        from .composite import order_tracks

        return [t for t in order_tracks(self.project) if t is not base]

    def _composite(
        self,
        base_segments: list[Path],
        overlays: list[Track],
        target: Path,
        audio_path: Path | None,
        ctx: RenderContext,
        markers: MarkerSet,
        reporter: ProgressReporter,
    ) -> None:
        from .composite import composite_tracks, render_overlay_track

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fd:
            base_path = Path(fd.name)
        mux_segments(base_segments, base_path, audio_path=None, binary=self.options.ffmpeg_binary)
        layers: list[tuple[Track, Path]] = []
        try:
            for track in overlays:
                with substep(reporter, step_name="finalize", name=f"overlay:{track.name}", kind="phase") as detail:
                    layers.append((track, render_overlay_track(self, track, ctx)))
                    detail["segments"] = len(track.segments)
                    detail["blend"] = track.blend
            with substep(reporter, step_name="finalize", name="composite", kind="phase") as detail:
                composite_tracks(
                    base_path,
                    layers,
                    target,
                    project=self.project,
                    markers=markers,
                    encode_args=self._encode_args(),
                    pix_fmt=self.project.render_settings.pix_fmt,
                    audio_path=audio_path,
                    binary=self.options.ffmpeg_binary,
                )
                detail["layers"] = len(layers)
        finally:
            for path in [base_path, *(p for _, p in layers)]:
                with contextlib.suppress(OSError):
                    path.unlink()

    def _extract_markers_with_progress(
        self,
        marker_ctx: "_MarkerExtractCtx",
        reporter: ProgressReporter,
    ) -> "MarkerSet":
        """Run :func:`extract_markers` with per-source substep events.

        Bracketing the whole call inside the ``extract_markers`` step is not
        enough - a single project can declare half-a-dozen marker sources
        and each beat tracker can take many seconds. Drive the same merge
        loop manually so each source gets its own ``substep_*`` pair.
        """

        from eks_harness.video.compile.markers import MarkerSet, StubBeatExtractor
        from eks_harness.video.plugins.registry import resolve_marker_extractor

        out = MarkerSet()
        stub = StubBeatExtractor()
        for source in self.project.markers:
            kind = type(source).__name__
            with substep(
                reporter,
                step_name="extract_markers",
                name=source.name,
                kind=kind,
            ) as detail:
                try:
                    extractor = resolve_marker_extractor(source.kind)
                except LookupError:
                    extractor = None

                if extractor is None and isinstance(source, _BeatTrackerType()):
                    merged = stub.extract(source, self.project.duration)
                    out.merge(merged)
                    detail["backend"] = "stub"
                    continue

                if extractor is None:
                    detail["skipped"] = True
                    continue

                # Marker extractors expect a ctx-like object exposing
                # ``project`` / ``workspace`` / ``cache_dir``; the minimal
                # extractor-time ctx satisfies that contract and also
                # carries the reporter so extractors can emit log events.
                marker_ctx.extra = {"progress_reporter": reporter}  # type: ignore[attr-defined]
                result = extractor.extract(source, ctx=marker_ctx)  # type: ignore[arg-type]
                if isinstance(result, MarkerSet):
                    out.merge(result)
                    for stream, times in result.streams.items():
                        detail.setdefault("counts", {})[stream] = len(times)
        return out

    def _render_planned_segments(
        self,
        plans: list["_SegmentPlan"],
        ctx: RenderContext,
        cache: SegmentCache,
        reporter: ProgressReporter,
    ) -> list[Path]:
        outputs: list[Path] = []
        fps = float(self.project.fps)
        for plan in plans:
            cache_key = self._segment_cache_key(plan)
            cached = cache.lookup(cache_key)
            if cached is not None:
                with substep(
                    reporter,
                    step_name="render_segments",
                    name=plan.segment.id,
                    kind="segment",
                    extra={"cache": "hit"},
                ) as detail:
                    detail["cache"] = "hit"
                    _LOG.debug("cache hit for segment %s", plan.segment.id)
                outputs.append(cached)
                continue

            ctx.extra["segment_duration_seconds"] = plan.duration
            frame_count = _frame_count(plan, fps)
            ctx.extra["segment_frame_total"] = frame_count
            ctx.extra["segment_id"] = plan.segment.id

            extras = {
                "frame_total": frame_count,
                "effects": [type(e).__name__ for e in [*plan.graph_prefix, *plan.frame_tail]],
            }
            with substep(
                reporter,
                step_name="render_segments",
                name=plan.segment.id,
                kind="segment",
                extra=extras,
            ) as detail:
                if plan.frame_tail:
                    workers = self._parallel_worker_count(plan, frame_count)
                    if workers > 1:
                        rendered = self._render_segment_parallel(
                            plan, ctx, frame_count, workers
                        )
                        detail["path"] = "parallel"
                        detail["workers"] = workers
                    else:
                        rendered = self._render_segment_hybrid(plan, ctx)
                        detail["path"] = "hybrid"
                else:
                    rendered = self._render_segment_graph_only(plan, ctx)
                    detail["path"] = "graph_only"
                stored = cache.store(cache_key, rendered)
                with contextlib.suppress(OSError):
                    Path(rendered).unlink()
                outputs.append(stored)
                get_plugin_manager().hook.after_segment_render(
                    segment=plan.segment, output=stored, ctx=ctx
                )
        return outputs

    def _boundary_beats(
        self, segments: Sequence[Segment], markers: "MarkerSet"
    ) -> dict[int, list[float]]:
        """Window-local beat times per boundary, for beat-reactive transitions.

        Only :class:`BeatFlash` / :class:`BeatGlitch` boundaries get an entry.
        For each, the xfade occupies the **last ``duration`` seconds of the
        left segment**, so the window spans project-time
        ``[out_left - duration, out_left]``. A beat at project-time ``tb`` maps
        to window-local ``tb - (out_left - duration)``; we keep only beats that
        land inside ``[0, duration]``. The :func:`assemble_transitions` clamp
        may shrink the effective window, and :func:`_window_pulse_times` drops
        any beat past the clamped end, so an unclamped window here is safe.

        The mapping is keyed by the right segment's index ``i`` to match the
        loop in :func:`assemble_transitions`.
        """

        from .transitions import _resolve_transition

        out: dict[int, list[float]] = {}
        for i in range(1, len(segments)):
            transition = _resolve_transition(segments[i - 1], segments[i])
            if not isinstance(transition, (BeatFlash, BeatGlitch)):
                continue
            stream_times = markers.streams.get(transition.stream, [])
            out_left = resolve_time(segments[i - 1].out, self.project, markers)
            window_start = out_left - transition.duration
            local = [
                tb - window_start
                for tb in stream_times
                if window_start <= tb <= out_left
            ]
            out[i] = local
        return out

    def _resolve_workspace(self) -> Path:
        if self.options.workspace is not None:
            workspace = Path(self.options.workspace)
        else:
            workspace = Path(self.options.output).resolve().parent
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _select_primary_video_track(self) -> Track:
        if not self.project.tracks:
            raise ValueError("project has no video tracks")
        from .composite import order_tracks

        return order_tracks(self.project)[0]

    def _plan_segment(
        self,
        segment: Segment,
        ctx: RenderContext,
        markers: MarkerSet,
    ) -> _SegmentPlan:
        in_seconds = resolve_time(segment.in_, self.project, markers)
        out_seconds = resolve_time(segment.out, self.project, markers)
        duration = out_seconds - in_seconds
        if duration <= 0:
            raise ValueError(f"segment {segment.id!r} has non-positive duration")

        chain = FilterChain()
        width, height = self.project.resolution
        chain.add(scale(width, height))
        media_path = _media_path(segment.media)
        graph_prefix, frame_tail = self._partition_segment(segment, ctx)

        return _SegmentPlan(
            segment=segment,
            base_chain=chain,
            duration=duration,
            media_path=media_path,
            graph_prefix=graph_prefix,
            frame_tail=frame_tail,
        )

    def _partition_segment(
        self, segment: Segment, ctx: RenderContext
    ) -> tuple[list[EffectIR], list[EffectIR]]:
        """Split ``segment.effects`` at the first non-graph-compilable effect.

        An effect joins the graph prefix only when both
        ``ffmpeg_graph in compile_targets`` *and* every animated parameter the
        plugin needs can be lowered to a finite ffmpeg expression.
        """

        graph_prefix: list[EffectIR] = []
        frame_tail: list[EffectIR] = []
        spilled = False
        for effect in segment.effects:
            if spilled:
                frame_tail.append(effect)
                continue
            targets: frozenset[str] = getattr(type(effect), "compile_targets", frozenset())
            plugin = self._maybe_resolve_effect_plugin(effect)
            prefers_frame = bool(
                plugin is not None and plugin.prefers_frame_pipeline(effect, ctx)
            )
            if (
                "ffmpeg_graph" in targets
                and not prefers_frame
                and self._effect_lowers_to_graph(effect, ctx)
            ):
                graph_prefix.append(effect)
            else:
                if "frame_pipeline" not in targets:
                    raise NotImplementedError(
                        f"effect {type(effect).__name__} declares neither ffmpeg_graph nor "
                        "frame_pipeline as a compile target"
                    )
                frame_tail.append(effect)
                spilled = True
        return graph_prefix, frame_tail

    def _maybe_resolve_effect_plugin(self, effect: EffectIR) -> EffectPlugin | None:
        from eks_harness.video.plugins.registry import _EFFECTS

        for plugin in _EFFECTS.values():
            if isinstance(effect, plugin.model):
                return plugin
        return None

    def _effect_lowers_to_graph(self, effect: EffectIR, ctx: RenderContext) -> bool:
        from eks_harness.video.ir.animated import Animated

        for field_name, _info in type(effect).model_fields.items():
            value = getattr(effect, field_name)
            if isinstance(value, Animated):
                lowered = animated_to_ffmpeg_expr(value, ctx.project, ctx.markers)
                if lowered is None:
                    return False
        return True

    def _segment_cache_key(self, plan: _SegmentPlan) -> str:
        media_digest = _digest_for_media(plan.media_path)
        from .cache import compute_segment_key

        return compute_segment_key(
            plan.segment,
            media_digest=media_digest,
            plugin_versions={},
            render_settings=self.project.render_settings.model_dump(),
        )

    def _build_chain_with_effects(
        self,
        plan: _SegmentPlan,
        ctx: RenderContext,
        effects: list[EffectIR],
    ) -> FilterChain:
        chain = FilterChain()
        for node in plan.base_chain.nodes:
            chain.add(node)
        for effect in effects:
            plugin = self._resolve_effect_plugin(effect)
            sub_chain = plugin.compile_graph(effect, ctx)
            for node in sub_chain.nodes:
                chain.add(node)
        return chain

    def _resolve_effect_plugin(self, effect: EffectIR) -> EffectPlugin:
        from eks_harness.video.plugins.registry import _EFFECTS

        for plugin in _EFFECTS.values():
            if isinstance(effect, plugin.model):
                return plugin
        raise LookupError(
            f"no effect plugin registered for IR model {type(effect).__name__}; "
            "ensure its package is installed and entry points are visible"
        )

    def _render_segment_graph_only(
        self, plan: _SegmentPlan, ctx: RenderContext
    ) -> Path:
        chain = self._build_chain_with_effects(plan, ctx, plan.graph_prefix)
        if isinstance(plan.segment.media, VideoFile):
            return self._render_video_segment(plan, chain)
        if isinstance(plan.segment.media, ImageFile):
            return self._render_image_segment(plan, chain)
        if isinstance(plan.segment.media, Solid):
            return self._render_solid_segment(plan, chain)
        if isinstance(plan.segment.media, HTMLOverlay):
            from . import html_renderer

            return html_renderer.render_html_overlay_segment(plan, ctx)
        raise NotImplementedError(
            f"media kind {plan.segment.media.kind!r} is not supported by the graph-only renderer"
        )

    def _frame_tail_runtime(
        self, plan: _SegmentPlan, ctx: RenderContext
    ) -> tuple[str, list["FrameProcessor"], list[str], bool, Path]:
        """Resolve the runtime inputs for a segment's frame-pipeline tail.

        Returns ``(graph_prefix_filter, processors, input_args, is_image,
        media_path)``. Shared by the serial hybrid path and the parallel chunk
        workers so both build the *identical* processor chain and decode-side
        ffmpeg invocation.
        """

        prefix_chain = self._build_chain_with_effects(plan, ctx, plan.graph_prefix)
        processors: list[FrameProcessor] = []
        for effect in plan.frame_tail:
            plugin = self._resolve_effect_plugin(effect)
            processors.append(plugin.open(effect, ctx))

        media_path = plan.media_path
        if media_path is None and isinstance(plan.segment.media, HTMLOverlay):
            if plan.segment.media.transparent:
                raise NotImplementedError(
                    "frame-pipeline effects on a transparent HTMLOverlay are not supported; "
                    "set transparent=False or move the effects to a track below it"
                )
            from . import html_renderer

            media_path = html_renderer.render_html_overlay_segment(plan, ctx)
        if media_path is None:
            raise NotImplementedError(
                "hybrid frame pipeline currently requires a file-backed media source"
            )

        is_image = isinstance(plan.segment.media, ImageFile)
        input_args: list[str] = ["-loop", "1"] if is_image else []
        return prefix_chain.serialize(), processors, input_args, is_image, media_path

    def _render_segment_hybrid(self, plan: _SegmentPlan, ctx: RenderContext) -> Path:
        prefix_filter, processors, input_args, is_image, media_path = (
            self._frame_tail_runtime(plan, ctx)
        )

        out_path = self._tmp_segment_path()
        width, height = self.project.resolution
        spec = FramePipelineSpec(
            input_path=media_path,
            output_path=out_path,
            width=width,
            height=height,
            fps=float(self.project.fps),
            duration=plan.duration,
            pix_fmt="bgr24",
            graph_prefix_filter=prefix_filter,
            input_args=input_args,
            ffmpeg_binary=self.options.ffmpeg_binary,
            is_image=is_image,
        )
        FramePipeline(spec, processors, ctx=ctx).run()
        return out_path

    def _parallel_worker_count(self, plan: _SegmentPlan, frame_count: int) -> int:
        """Worker count for ``plan``'s frame tail, or ``1`` to force serial.

        The parallel chunked path is used only when ALL hold:

        * a worker count greater than one is available (CPU count, capped at
          8, env-overridable via ``EKS_HARNESS_RENDER_WORKERS``);
        * the segment is large enough to amortise process spawn + concat
          (``frame_count >= 2 * MIN_CHUNK_FRAMES``);
        * every frame-tail processor is ``parallel_safe`` (no cross-frame
          temporal state, no heavy ML/GPU model);
        * ``plan.graph_prefix`` is empty, so the only decode-side filter is the
          time-invariant base ``scale`` - animated graph prefixes are
          time-dependent and would not survive the per-chunk window rebuild.
        * the segment is not an ``HTMLOverlay`` (each worker would re-render
          the whole page);
        * the worker can reconstruct the project (``project.py`` is present in
          the workspace).

        Any miss returns ``1`` and the caller takes the unchanged serial path.
        """

        from .parallel_frame_pipeline import (
            MIN_CHUNK_FRAMES,
            resolve_worker_count,
        )

        if plan.graph_prefix:
            return 1
        if isinstance(plan.segment.media, HTMLOverlay):
            return 1
        if frame_count < 2 * MIN_CHUNK_FRAMES:
            return 1
        if not self._frame_tail_parallel_safe(plan):
            return 1
        if not (self._workspace_dir() / "project.py").exists():
            return 1
        return resolve_worker_count(frame_count)

    def _frame_tail_parallel_safe(self, plan: _SegmentPlan) -> bool:
        for effect in plan.frame_tail:
            plugin = self._resolve_effect_plugin(effect)
            processor_cls = self._frame_processor_class(plugin)
            if processor_cls is None:
                return False
            if not getattr(processor_cls, "parallel_safe", True):
                return False
        return True

    def _frame_processor_class(self, plugin: "EffectPlugin") -> type | None:
        """Best-effort resolution of a plugin's :class:`FrameProcessor` type.

        The ``parallel_safe`` flag lives on the processor class, but the
        orchestrator only holds the :class:`Effect` plugin. ``Effect.open``
        returns the processor instance; plugins conventionally expose their
        processor as a module-level ``_*Processor`` class. We resolve it from
        the plugin module so eligibility can be decided without instantiating
        (and thus loading models for) the processor.
        """

        import inspect

        from eks_harness.video.plugins.base import FrameProcessor

        module = inspect.getmodule(type(plugin))
        if module is None:
            return None
        candidates = [
            obj
            for _name, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, FrameProcessor) and obj is not FrameProcessor
        ]
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous module (multiple processors) - treat as not parallel-safe
        # rather than guess wrong and break determinism.
        return None

    def _render_segment_parallel(
        self,
        plan: _SegmentPlan,
        ctx: RenderContext,
        frame_count: int,
        workers: int,
    ) -> Path:
        from .parallel_frame_pipeline import (
            ParallelFramePipelineError,
            render_segment_parallel,
        )

        out_path = self._tmp_segment_path()
        chunk_dir = self._resolve_workspace() / "cache" / "chunks"
        try:
            return render_segment_parallel(
                project_path=self._workspace_dir(),
                plan=plan,
                ctx=ctx,
                output_path=out_path,
                frame_total=frame_count,
                workers=workers,
                ffmpeg_binary=self.options.ffmpeg_binary,
                chunk_dir=chunk_dir,
            )
        except ParallelFramePipelineError:
            _LOG.warning(
                "parallel frame pipeline failed for segment %s; falling back to serial",
                plan.segment.id,
                exc_info=True,
            )
            with contextlib.suppress(OSError):
                out_path.unlink()
            return self._render_segment_hybrid(plan, ctx)

    def _workspace_dir(self) -> Path:
        if self.options.workspace is not None:
            return Path(self.options.workspace)
        return self._resolve_workspace()

    def _encode_args(self) -> list[str]:
        rs = self.project.render_settings
        return hwaccel.encode_args(crf=rs.crf, bitrate_kbps=rs.bitrate_kbps)

    def _render_video_segment(self, plan: _SegmentPlan, chain: FilterChain) -> Path:
        assert plan.media_path is not None
        out_path = self._tmp_segment_path()
        # HW-decode the source file; HW-encode the result.
        args = [*hwaccel.decode_args(), "-i", str(plan.media_path), *_filter_args(chain),
                "-t", f"{plan.duration:.6f}", "-r", f"{self.project.fps}", "-an",
                "-pix_fmt", self.project.render_settings.pix_fmt, *self._encode_args(), str(out_path)]
        run_ffmpeg(args, binary=self.options.ffmpeg_binary)
        return out_path

    def _render_image_segment(self, plan: _SegmentPlan, chain: FilterChain) -> Path:
        assert plan.media_path is not None
        out_path = self._tmp_segment_path()
        # No HW decode for a looped still image; HW-encode the result.
        args = ["-loop", "1", "-i", str(plan.media_path), *_filter_args(chain),
                "-t", f"{plan.duration:.6f}", "-r", f"{self.project.fps}", "-an",
                "-pix_fmt", self.project.render_settings.pix_fmt, *self._encode_args(), str(out_path)]
        run_ffmpeg(args, binary=self.options.ffmpeg_binary)
        return out_path

    def _render_solid_segment(self, plan: _SegmentPlan, chain: FilterChain) -> Path:
        assert isinstance(plan.segment.media, Solid)
        r, g, b, _a = plan.segment.media.color
        out_path = self._tmp_segment_path()
        width, height = self.project.resolution
        color = f"0x{r:02x}{g:02x}{b:02x}"
        args = [
            "-f", "lavfi",
            "-i", f"color=c={color}:s={width}x{height}:d={plan.duration:.6f}:r={self.project.fps}",
            "-pix_fmt", self.project.render_settings.pix_fmt,
            *self._encode_args(),
            "-t", f"{plan.duration:.6f}",
            str(out_path),
        ]
        run_ffmpeg(args, binary=self.options.ffmpeg_binary)
        return out_path

    def _tmp_segment_path(self) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fd:
            return Path(fd.name)


def _frame_count(plan: "_SegmentPlan", fps: float) -> int:
    """Conservative upper bound on rendered frames for ``plan``.

    Used to pre-compute ``frame_total`` for the segment substep payload so
    consumers (the MCP layer and the tqdm bar) can render a meaningful
    percentage / ETA from the first frame onwards.
    """

    return max(1, math.ceil(plan.duration * float(fps)))


def _BeatTrackerType() -> type:  # noqa: N802 - sentinel helper
    """Lazy resolver for :class:`BeatTracker`.

    Hoisted to a function so the orchestrator module does not need to
    import :class:`BeatTracker` at the top level (avoiding a wider import
    cycle through the IR package).
    """

    from eks_harness.video.ir.markers import BeatTracker

    return BeatTracker


def _filter_args(chain: FilterChain) -> list[str]:
    """Return ``-vf`` or ``-filter_complex`` args for a serialised chain.

    A simple chain (one implicit input, one implicit output) can use the
    fast ``-vf`` path. Chains that introduce additional inputs (e.g.
    ``movie=...`` in the Watermark plugin) or that reference named pads
    (``[main]``, ``[wm]``) must use ``-filter_complex`` with explicit
    ``[0:v]`` mapping; ``-vf`` refuses any graph that isn't 1-in/1-out.
    """

    serialised = chain.serialize()
    needs_complex = (
        "movie=" in serialised
        or "amovie=" in serialised
        or "[main]" in serialised
        or "[0:v]" in serialised
    )
    if not needs_complex:
        return ["-vf", serialised]
    # Re-label `[main]` as ffmpeg's canonical first-video-input label.
    complex_chain = serialised.replace("[main]", "[0:v]")
    if "[0:v]" not in complex_chain:
        complex_chain = f"[0:v]{complex_chain}"
    return ["-filter_complex", complex_chain]


def _media_path(media: object) -> Path | None:
    if isinstance(media, (VideoFile, ImageFile, AudioFile)):
        return Path(media.path)
    if isinstance(media, (Solid, GeneratedCard, TTSGenerated, HTMLOverlay)):
        return None
    return None


def _digest_for_media(path: Path | None) -> str:
    if path is None:
        return "synthetic"
    if not path.exists():
        return f"missing:{path}"
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["Renderer"]
