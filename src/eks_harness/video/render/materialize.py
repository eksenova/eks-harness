"""Turn plugin-rendered media into cached video files before segment planning.

A :class:`~eks_harness.video.plugins.base.MediaRenderer` owns a synthetic media kind
(a Blender scene, a procedural generator). :func:`materialize_media` walks
every track, renders each segment of such a kind once into the render cache
and replaces the segment's media with a ``VideoFile`` covering exactly the
segment's source window. Everything downstream (planning, effects, speed,
overlay compositing, transitions) then treats it like footage.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.media import VideoFile
from eks_harness.video.ir.time import Seconds
from eks_harness.video.plugins.base import MediaRenderRequest
from eks_harness.video.plugins.registry import get_media_renderer

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.ir.tracks import Segment
    from eks_harness.video.plugins.base import MediaRenderer
    from eks_harness.video.render.context import RenderContext

_LOG = logging.getLogger(__name__)


def marker_payload(markers: MarkerSet) -> dict[str, Any]:
    """Every project marker as plain JSON on the project timeline."""

    return {
        "streams": {name: list(times) for name, times in sorted(markers.streams.items())},
        "named": {name: list(times) for name, times in sorted(markers.named.items())},
        "words": [{"text": w.text, "t_start": w.t_start, "t_end": w.t_end, "source": w.source}
                  for w in markers.words],
    }


def constant_speed(segment: Segment) -> float | None:
    value = segment.speed.root
    return float(value) if isinstance(value, (int, float)) else None


def _digest_path(path: Path, h: Any) -> None:
    if path.is_dir():
        for sub in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            h.update(str(sub.relative_to(path)).encode())
            h.update(sub.read_bytes())
    elif path.exists():
        h.update(path.read_bytes())
    else:
        h.update(f"missing:{path}".encode())


def _digest_file(path: Path, h: Any) -> None:
    if not path.is_file():
        h.update(f"missing:{path}".encode())
        return
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)


def nested_media(media: Any) -> dict[str, Any]:
    found = getattr(media, "media", None)
    return dict(found) if isinstance(found, dict) else {}


def request_key(renderer: MediaRenderer, request: MediaRenderRequest, ctx: RenderContext,
                nested: dict[str, Path] | None = None) -> str:
    """Content-addressed key: renderer identity, media model, timing, markers, input files and nested outputs."""

    h = hashlib.sha256()
    h.update(json.dumps({
        "renderer": renderer.name,
        "version": renderer.version,
        "salt": renderer.cache_salt(request.media, ctx),
        "media": request.media.model_dump(mode="json"),
        "fps": request.fps,
        "resolution": list(request.resolution),
        "source": [round(request.source_start, 6), round(request.source_end, 6)],
        "project_start": round(request.project_start, 6),
        "speed": request.speed,
        "frames": request.frame_count,
        "markers": request.markers,
    }, sort_keys=True).encode())
    for path in renderer.cache_inputs(request.media, ctx):
        h.update(str(path).encode())
        _digest_path(Path(path), h)
    for name, path in sorted((nested or {}).items()):
        h.update(f"nested:{name}".encode())
        _digest_file(Path(path), h)
    return h.hexdigest()[:32]


def enabled_renderers(project: Project) -> dict[str, MediaRenderer]:
    """Opt-in media renderers the project lists in ``Project.plugins``, by plugin name."""

    from eks_harness.video.plugins.registry import media_renderers

    wanted = {req.name for req in project.plugins}
    return {r.name: r for r in media_renderers() if r.name in wanted}


def check_enabled_renderers(project: Project, ctx: RenderContext) -> None:
    """Fail before any work when a renderer the project enables cannot run on this machine."""

    for renderer in enabled_renderers(project).values():
        renderer.check_available(ctx)


def render_request(renderer: MediaRenderer, probe: MediaRenderRequest, ctx: RenderContext) -> Path:
    """Render (or reuse from the cache) one request; returns the clip path."""

    root = Path(ctx.cache_dir) / "media"
    nested = {name: render_nested(media, probe, ctx, name) for name, media in nested_media(probe.media).items()}
    key = request_key(renderer, probe, ctx, nested=nested)
    work_dir = root / renderer.name / key
    output = root / renderer.name / f"{key}.mov"
    if output.exists() and output.stat().st_size > 0:
        _LOG.info("media %s: cached %s", probe.segment_id, output.name)
        return output
    work_dir.mkdir(parents=True, exist_ok=True)
    request = MediaRenderRequest(**{**probe.__dict__, "work_dir": work_dir, "output": output})
    rendered = Path(renderer.render(request, ctx))
    if rendered != output:
        output.parent.mkdir(parents=True, exist_ok=True)
        rendered.replace(output)
    return output


def render_nested(media: Any, parent: MediaRenderRequest, ctx: RenderContext, name: str) -> Path:
    """Render media embedded in another media (e.g. a scene's screen) for the parent's source window.

    ``media`` is a file path, a media model or its dict form; plugin kinds go through their renderer and cache.
    """

    from pydantic import TypeAdapter

    from eks_harness.video.ir import media as media_ir

    if isinstance(media, (str, Path)):
        return Path(media)
    model = media if not isinstance(media, dict) else TypeAdapter(media_ir.MediaSource).validate_python(media)
    if isinstance(model, (media_ir.VideoFile, media_ir.ImageFile)):
        return Path(model.path)
    renderer = get_media_renderer(getattr(model, "kind", None))
    if renderer is None:
        raise ValueError(f"{parent.segment_id}: nested media {name!r} of kind {getattr(model, 'kind', None)!r} "
                         "has no renderer")
    probe = MediaRenderRequest(**{**parent.__dict__, "segment_id": f"{parent.segment_id}.{name}", "media": model})
    return render_request(renderer, probe, ctx)


def materialize_media(project: Project, ctx: RenderContext) -> Project:
    """Render every plugin-media segment and return a project that only references files."""

    enabled = enabled_renderers(project)
    work: list[tuple[int, int, Segment, MediaRenderer]] = []
    for ti, track in enumerate(project.tracks):
        for si, segment in enumerate(track.segments):
            renderer = get_media_renderer(getattr(segment.media, "kind", None))
            if renderer is None:
                continue
            if renderer.opt_in and renderer.name not in enabled:
                raise ValueError(
                    f"segment {segment.id!r} uses {segment.media.kind!r}, which needs the {renderer.name!r} plugin: "
                    f"add PluginRequirement(name={renderer.name!r}) to Project.plugins"
                )
            work.append((ti, si, segment, renderer))
    if not work:
        return project

    markers = marker_payload(ctx.markers)
    result = project.model_copy(deep=True)
    root = Path(ctx.cache_dir) / "media"
    for ti, si, segment, renderer in work:
        source_start = resolve_time(segment.in_, project, ctx.markers)
        source_end = resolve_time(segment.out, project, ctx.markers)
        if source_end <= source_start:
            raise ValueError(f"segment {segment.id!r}: out must be after in")
        frame_count = max(1, round((source_end - source_start) * project.fps))
        probe = MediaRenderRequest(
            segment_id=segment.id, media=segment.media, fps=float(project.fps),
            resolution=tuple(project.resolution), source_start=source_start, source_end=source_end,
            project_start=resolve_time(segment.start, project, ctx.markers), speed=constant_speed(segment),
            frame_count=frame_count, markers=markers, work_dir=root, output=root,
        )
        output = render_request(renderer, probe, ctx)
        result.tracks[ti].segments[si] = segment.model_copy(update={
            "media": VideoFile(path=output),
            "in_": Seconds(t=0.0),
            "out": Seconds(t=frame_count / project.fps),
        })
    return result


__all__ = ["check_enabled_renderers", "constant_speed", "enabled_renderers", "marker_payload", "materialize_media",
           "render_nested", "render_request", "request_key"]
