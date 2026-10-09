from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.score.model import AudioTrack, BlenderTrack, DeviceTrack, EditTrack, Score, WebTrack
from eks_harness.score.schedule import Analyzer, Event, Plan, build_context, plan

log = logging.getLogger("eks_harness.score")

SCENE_KINDS = ("web", "blender")


class ScoreRenderError(RuntimeError):
    pass


@dataclass
class Take:
    footage: Path
    events: list[Event] = field(default_factory=list)


TakeRunner = Callable[[DeviceTrack, Plan, Path, Path], Take]
Progress = Callable[[str, dict[str, Any]], None]


@dataclass
class ScoreRender:
    output: Path | None
    plan: Plan
    clips: dict[str, Path]
    events: list[Event]
    iterations: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"output": str(self.output) if self.output else None, "iterations": self.iterations,
                "clips": {k: str(v) for k, v in self.clips.items()},
                "events": [e.as_dict(self.plan.context.fps) for e in self.events], "warnings": self.warnings,
                "plan": self.plan.as_dict()}


def _resolve(base: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def dependency_order(score: Score) -> list[Any]:
    scenes = {t.id: t for t in score.tracks if t.kind in SCENE_KINDS and t.enabled}
    order: list[Any] = []
    state: dict[str, int] = {}

    def visit(track_id: str, chain: list[str]) -> None:
        if state.get(track_id) == 2:
            return
        if state.get(track_id) == 1:
            raise ScoreRenderError(f"texture bindings form a cycle: {' -> '.join([*chain, track_id])}")
        state[track_id] = 1
        for source in (getattr(scenes[track_id], "textures", {}) or {}).values():
            if source in scenes:
                visit(source, [*chain, track_id])
        state[track_id] = 2
        order.append(scenes[track_id])

    for track_id in scenes:
        visit(track_id, [])
    return order


def marker_payload(current: Plan) -> dict[str, Any]:
    ctx = current.context
    return {"streams": {"beat": list(ctx.beats), "downbeat": list(ctx.downbeats)},
            "named": {name: [t] for name, t in ctx.markers.items()}, "words": []}


def scene_inputs(current: Plan, track_id: str) -> list[dict[str, Any]]:
    out = []
    for item in current.inputs(track_id):
        args = dict(item.get("args") or {})
        entry: dict[str, Any] = {"time": item["local"], "verb": item["verb"], "source": item.get("rule")}
        if item["verb"] == "emit":
            entry["name"] = args.get("name")
            entry["data"] = args.get("data") or {}
        elif item["verb"] in ("set", "key"):
            entry["prop"] = args.get("prop")
            entry["value"] = args.get("value")
            if args.get("ease") is not None:
                entry["ease"] = args.get("ease")
        else:
            entry["name"] = item["verb"]
            entry["data"] = args
        out.append(entry)
    return out


def _texture_media(track: Any, clips: dict[str, Path], base: Path) -> dict[str, Any]:
    media: dict[str, Any] = {}
    for name, source in (getattr(track, "textures", {}) or {}).items():
        if source in clips:
            media[name] = str(clips[source])
        elif source.startswith("file:"):
            media[name] = str(_resolve(base, source[len("file:"):]))
        else:
            raise ScoreRenderError(f"{track.id}: texture {name} needs track {source}, which has no footage yet")
    return media


def scene_media(track: Any, current: Plan, clips: dict[str, Path], base: Path) -> Any:
    inputs = scene_inputs(current, track.id)
    textures = _texture_media(track, clips, base)
    if isinstance(track, WebTrack):
        from eks_harness.video.plugins.builtin.media.web_scene import SceneEvent, WebScene

        entry = _resolve(base, track.entry)
        return WebScene(entry=str(entry), root=str(entry.parent), props=track.props,
                        events=[SceneEvent(**e) for e in inputs], media=textures, resolution=tuple(track.size),
                        transparent=track.transparent)
    if isinstance(track, BlenderTrack):
        from eks_harness.video.plugins.builtin.media.blender import BlenderScene

        return BlenderScene(script=str(_resolve(base, track.script)) if track.script else None,
                            blend=str(_resolve(base, track.blend)) if track.blend else None, root=str(base),
                            params=track.props, events=inputs, emits=bool(track.emits), media=textures,
                            engine=track.engine, samples=track.samples, resolution=tuple(track.size),
                            transparent=track.transparent)
    raise ScoreRenderError(f"{track.id}: {track.kind} tracks are not scene tracks")


def _render_context(score: Score, current: Plan, workspace: Path, output: Path) -> Any:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project, Seconds, Segment, Solid, Track
    from eks_harness.video.render.context import RenderContext, RenderOptions

    edit = next((t for t in score.tracks if isinstance(t, EditTrack)), None)
    size = tuple(edit.size) if edit else (1080, 1920)
    duration = max(current.duration, 1.0 / current.context.fps)
    placeholder = Project(fps=current.context.fps, resolution=size, duration=duration,
                          tracks=[Track(name="score", segments=[Segment(id="placeholder", start=Seconds(t=0.0),
                                                                        out=Seconds(t=duration),
                                                                        media=Solid(color=(0, 0, 0, 0)))])])
    return RenderContext(project=placeholder, options=RenderOptions(output=output, workspace=workspace),
                         markers=MarkerSet(), workspace=workspace, cache_dir=workspace / "cache")


def render_scene_track(track: Any, current: Plan, clips: dict[str, Path], base: Path, rctx: Any) -> tuple[Path, list[Event]]:
    from eks_harness.video.plugins.base import MediaRenderRequest
    from eks_harness.video.plugins.registry import ensure_registry, get_media_renderer, register_media_renderer
    from eks_harness.video.render.materialize import render_request

    ensure_registry()
    media = scene_media(track, current, clips, base)
    renderer = get_media_renderer(media.kind)
    if renderer is None:
        if media.kind == "blender_scene":
            from eks_harness.video.plugins.builtin.media.blender import BlenderRenderer

            renderer = BlenderRenderer()
            register_media_renderer(renderer)
        else:
            raise ScoreRenderError(f"no renderer for {media.kind}; enable its plugin")
    renderer.check_available(rctx)
    start, end = current.track_spans[track.id]
    fps = current.context.fps
    frames = max(1, round((end - start) * fps))
    probe = MediaRenderRequest(segment_id=f"score.{track.id}", media=media, fps=fps, resolution=tuple(track.size),
                               source_start=0.0, source_end=frames / fps, project_start=start, speed=1.0,
                               frame_count=frames, markers=marker_payload(current), work_dir=rctx.cache_dir,
                               output=rctx.cache_dir / f"{track.id}.mov")
    clip = render_request(renderer, probe, rctx)
    events = []
    sidecar = clip.with_suffix(".events.json")
    if sidecar.is_file():
        for item in json.loads(sidecar.read_text(encoding="utf-8")).get("emitted") or []:
            events.append(Event(time=start + float(item["time"]), source=track.id, name=str(item["name"]),
                                data=dict(item.get("data") or {})))
    return clip, events


def _key(events: list[Event]) -> list[tuple]:
    return sorted((round(e.time, 4), e.source, e.name, json.dumps(e.data, sort_keys=True, default=str))
                  for e in events)


def compose_project(score: Score, current: Plan, clips: dict[str, Path], base: Path) -> Any:
    from eks_harness.video.ir import AudioSegment, AudioTrack as EngineAudio, Project, Seconds, Segment, Track
    from eks_harness.video.ir.media import AudioFile, ImageFile, VideoFile

    edit = next((t for t in score.tracks if isinstance(t, EditTrack)), None)
    if edit is None:
        raise ScoreRenderError("the score has no edit track; add score.edit.layer(...) to composite it")
    ctx = current.context
    duration = current.duration or max(end for _, end in current.track_spans.values())
    tracks = []
    for index, layer in enumerate(edit.layers):
        if layer.source.startswith("file:"):
            path = _resolve(base, layer.source[len("file:"):])
            media = ImageFile(path=str(path)) if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") \
                else VideoFile(path=str(path))
            span = (0.0, duration)
        else:
            if layer.source not in clips:
                raise ScoreRenderError(f"edit layer {layer.source} has no rendered footage")
            media = VideoFile(path=str(clips[layer.source]))
            span = current.track_spans[layer.source]
        start = ctx.resolve(layer.start) if layer.start else span[0]
        end = ctx.resolve(layer.end) if layer.end else span[1]
        if end <= start:
            continue
        offset = max(0.0, start - span[0])
        tracks.append(Track(name=f"layer{index}-{layer.source.replace(':', '_')}", z=index,
                            blend=layer.blend if layer.blend in ("normal", "add", "multiply", "screen", "overlay")
                            else "normal",
                            segments=[Segment(id=f"layer{index}", start=Seconds(t=start), in_=Seconds(t=offset),
                                              out=Seconds(t=offset + end - start), media=media,
                                              effects=list(layer.effects))]))
    audio = []
    for audio_id in edit.audio:
        track = score.track(audio_id)
        if not isinstance(track, AudioTrack):
            continue
        window = track.window or score.clock.window or (0.0, duration)
        audio.append(EngineAudio(name=track.id, segments=[AudioSegment(
            id=track.id, start=Seconds(t=0.0), in_=Seconds(t=window[0]), out=Seconds(t=window[0] + duration),
            media=AudioFile(path=str(_resolve(base, track.path))))]))
    return Project(fps=ctx.fps, resolution=tuple(edit.size), duration=duration, tracks=tracks, audio_tracks=audio,
                   metadata={"score": score.name})


def render_score(score: Score, base: Path, output: Path | None, *, workspace: Path | None = None,
                 analyzer: Analyzer | None = None, take_runner: TakeRunner | None = None, max_iterations: int = 4,
                 progress: Progress | None = None, mode: str = "final") -> ScoreRender:
    from eks_harness.video.render.context import RenderOptions
    from eks_harness.video.render.orchestrator import Renderer

    report = progress or (lambda stage, detail: None)
    workspace = (workspace or base / ".score-cache").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    context = build_context(score, base, analyzer)
    clips: dict[str, Path] = {}
    device_events: list[Event] = []
    warnings: list[str] = []
    current = plan(score, base=base, context=context)
    for track in score.of_kind("device"):
        footage = track.options.get("footage")
        report("take", {"track": track.id})
        if footage:
            clips[track.id] = _resolve(base, footage)
            device_events += [Event(float(e["time"]), track.id, e["name"], dict(e.get("data") or {}))
                              for e in track.options.get("events") or []]
            continue
        if take_runner is None:
            raise ScoreRenderError(f"device track {track.id} needs a take runner (a device driver) or "
                                   f"options.footage with a recorded take")
        take = take_runner(track, current, workspace, base)
        clips[track.id] = take.footage
        device_events += take.events
    rctx = _render_context(score, current, workspace, output or workspace / "score.mp4")
    scene_events: list[Event] = []
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        current = plan(score, observed=[*device_events, *scene_events], base=base, context=context)
        emitted: list[Event] = []
        for track in dependency_order(score):
            report("scene", {"track": track.id, "iteration": iterations})
            clip, events = render_scene_track(track, current, clips, base, rctx)
            clips[track.id] = clip
            emitted += events
        if _key(emitted) == _key(scene_events):
            break
        scene_events = emitted
    else:
        warnings.append(f"scene events did not settle after {max_iterations} passes (a feedback loop between "
                        f"scenes); the last pass is used")
    current = plan(score, observed=[*device_events, *scene_events], base=base, context=context)
    final = None
    if output is not None:
        report("composite", {"output": str(output)})
        project = compose_project(score, current, clips, base)
        options = RenderOptions(output=output, mode=mode, workspace=workspace, cache_dir=workspace / "cache")
        final = Renderer(project, options).render(output)
    return ScoreRender(output=final, plan=current, clips=clips, events=[*device_events, *scene_events],
                       iterations=iterations, warnings=warnings + current.warnings)
