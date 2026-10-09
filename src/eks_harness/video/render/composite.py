"""Multi-track compositing.

The base track (lowest ``z``) is rendered by the regular segment pipeline:
its segments are concatenated and joined by their transitions. Every other
track is an overlay: its segments are placed at their own ``start`` time on
a transparent canvas the length of the project, rendered in one ffmpeg graph
with the alpha channel intact (ProRes 4444), then stacked onto the base in
``z`` order.

Blend modes:

* ``normal`` -- alpha overlay.
* ``add`` / ``multiply`` / ``screen`` / ``overlay`` -- the layer is blended
  with what is below it, then the result is masked by the layer's own alpha,
  so fully transparent pixels never change the image underneath.

``Track.opacity`` scales the layer alpha. Constants and keyframes lower to an
ffmpeg expression; curves are not supported on track opacity.

Overlay segments accept media whose alpha survives decoding (``ImageFile``
PNGs, ``VideoFile`` ProRes 4444 / QuickTime Animation / PNG-in-MOV / VP9
alpha WebM) and ``Solid`` colours with alpha. Their effects must be
graph-compilable: a frame-pipeline effect decodes to bgr24 and would drop the
alpha, so it raises instead of silently flattening the layer.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.compile.expr_compile import animated_to_ffmpeg_expr
from eks_harness.video.compile.time_resolve import resolve_time
from eks_harness.video.ir.media import ImageFile, Solid, VideoFile
from eks_harness.video.ir.transitions import Cut

from .ffmpeg_builder import FilterChain
from .subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from eks_harness.video.compile.markers import MarkerSet
    from eks_harness.video.ir import Project
    from eks_harness.video.ir.tracks import Segment, Track

    from .context import RenderContext
    from .orchestrator import Renderer

__all__ = ["BLEND_MODES", "composite_tracks", "order_tracks", "render_overlay_track"]

BLEND_MODES = {"add": "addition", "multiply": "multiply", "screen": "screen", "overlay": "overlay"}

ALPHA_ENCODE = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                "-alpha_bits", "16", "-vendor", "apl0"]


def order_tracks(project: Project) -> list[Track]:
    """Tracks bottom to top: ascending ``z``, declaration order breaks ties."""

    indexed = sorted(enumerate(project.tracks), key=lambda pair: (pair[1].z, pair[0]))
    return [track for _, track in indexed]


def _time_expr(expr: str) -> str:
    return re.sub(r"(?<![A-Za-z_])t(?![A-Za-z_(])", "T", expr)


def _opacity_filter(track: Track, project: Project, markers: MarkerSet) -> str | None:
    expr = animated_to_ffmpeg_expr(track.opacity, project, markers)
    if expr is None:
        raise NotImplementedError(
            f"track {track.name!r}: opacity curves are not supported; use a constant or keyframes"
        )
    try:
        if abs(float(expr) - 1.0) < 1e-9:
            return None
        return f"colorchannelmixer=aa={float(expr):.6f}"
    except ValueError:
        return f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*clip({_time_expr(expr)},0,1)'"


def _media_file(media: VideoFile | ImageFile, workspace: Path) -> Path:
    path = Path(media.path)
    if not path.is_absolute() and not path.exists() and (workspace / path).exists():
        path = workspace / path
    if not path.exists():
        raise FileNotFoundError(f"overlay media not found: {media.path}")
    return path


def _segment_input(
    segment: Segment, renderer: Renderer, workspace: Path, in_s: float, duration: float
) -> list[str]:
    media = segment.media
    fps = renderer.project.fps
    width, height = renderer.project.resolution
    if isinstance(media, VideoFile):
        path = _media_file(media, workspace)
        return ["-ss", f"{in_s:.6f}", "-t", f"{duration:.6f}", "-i", str(path)]
    if isinstance(media, ImageFile):
        path = _media_file(media, workspace)
        return ["-loop", "1", "-framerate", f"{fps}", "-t", f"{duration:.6f}", "-i", str(path)]
    if isinstance(media, Solid):
        r, g, b, a = media.color
        return ["-f", "lavfi", "-i",
                f"color=c=0x{r:02x}{g:02x}{b:02x}@{a / 255:.4f}:s={width}x{height}:r={fps}:d={duration:.6f},format=rgba"]
    raise NotImplementedError(
        f"segment {segment.id!r}: media kind {media.kind!r} is not supported on an overlay track; "
        "render it to an alpha video (ProRes 4444, PNG MOV) and use VideoFile"
    )


def render_overlay_track(renderer: Renderer, track: Track, ctx: RenderContext) -> Path:
    project = renderer.project
    markers = ctx.markers
    width, height = project.resolution
    fps = project.fps
    total = float(project.duration)

    inputs: list[str] = ["-f", "lavfi", "-i",
                         f"color=c=black@0.0:s={width}x{height}:r={fps}:d={total:.6f},format=rgba"]
    graph: list[str] = ["[0:v]format=rgba[c0]"]
    current = "c0"
    for index, segment in enumerate(track.segments, start=1):
        if not isinstance(segment.transition_in, Cut) or not isinstance(segment.transition_out, Cut):
            raise NotImplementedError(
                f"segment {segment.id!r}: transitions are only supported on the base track; "
                "animate the overlay with effects or track opacity instead"
            )
        start = resolve_time(segment.start, project, markers)
        in_s = resolve_time(segment.in_, project, markers)
        out_s = resolve_time(segment.out, project, markers)
        duration = out_s - in_s
        if duration <= 0:
            raise ValueError(f"segment {segment.id!r} has non-positive duration")
        inputs += _segment_input(segment, renderer, ctx.workspace, in_s, duration)

        plan = renderer._plan_segment(segment, ctx, markers)
        if plan.frame_tail:
            names = ", ".join(type(e).__name__ for e in plan.frame_tail)
            raise NotImplementedError(
                f"segment {segment.id!r} on overlay track {track.name!r}: frame-pipeline effects "
                f"({names}) would drop the alpha channel; use graph-compilable effects or bake them "
                "into the source"
            )
        chain = FilterChain()
        effects_chain = renderer._build_chain_with_effects(plan, ctx, plan.graph_prefix)
        for node in effects_chain.nodes:
            chain.add(node)
        effect_graph = chain.serialize()
        label = f"s{index}"
        graph.append(
            f"[{index}:v]fps={fps},format=yuva444p,{effect_graph},format=rgba,"
            f"trim=duration={duration:.6f},setpts=PTS-STARTPTS+{start:.6f}/TB[{label}]"
        )
        nxt = f"c{index}"
        graph.append(
            f"[{current}][{label}]overlay=eof_action=pass:format=auto:"
            f"enable='between(t,{start:.6f},{start + duration:.6f})'[{nxt}]"
        )
        current = nxt

    with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as fd:
        out_path = Path(fd.name)
    args = [*inputs, "-filter_complex", ";".join(graph), "-map", f"[{current}]",
            "-t", f"{total:.6f}", "-r", f"{fps}", "-an", *ALPHA_ENCODE, str(out_path)]
    run_ffmpeg(args, binary=renderer.options.ffmpeg_binary)
    return out_path


def composite_tracks(
    base: Path,
    layers: Sequence[tuple[Track, Path]],
    output: Path,
    *,
    project: Project,
    markers: MarkerSet,
    encode_args: list[str],
    pix_fmt: str,
    audio_path: Path | None,
    binary: str,
) -> Path:
    inputs = ["-i", str(base)]
    for _, path in layers:
        inputs += ["-i", str(path)]
    graph: list[str] = ["[0:v]format=rgba[b0]"]
    current = "b0"
    for index, (track, _) in enumerate(layers, start=1):
        opacity = _opacity_filter(track, project, markers)
        layer = f"[{index}:v]format=rgba" + (f",{opacity}" if opacity else "")
        nxt = f"b{index}"
        if track.blend == "normal":
            graph.append(f"{layer}[l{index}]")
            graph.append(f"[{current}][l{index}]overlay=format=auto:eof_action=pass[{nxt}]")
        else:
            mode = BLEND_MODES[track.blend]
            graph.append(f"{layer},split[la{index}][lb{index}]")
            graph.append(f"[la{index}]alphaextract[m{index}]")
            graph.append(f"[{current}]split[u{index}][v{index}]")
            graph.append(f"[u{index}]format=gbrp[up{index}]")
            graph.append(f"[lb{index}]format=gbrp[lp{index}]")
            graph.append(f"[up{index}][lp{index}]blend=all_mode={mode}[bl{index}]")
            graph.append(f"[bl{index}][m{index}]alphamerge[ba{index}]")
            graph.append(f"[v{index}][ba{index}]overlay=format=auto[{nxt}]")
        current = nxt
    graph.append(f"[{current}]format={pix_fmt}[out]")

    audio_args: list[str] = []
    if audio_path is not None:
        inputs += ["-i", str(audio_path)]
        audio_args = ["-map", f"{len(layers) + 1}:a:0", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", *audio_args,
            "-r", f"{project.fps}", *encode_args, "-movflags", "+faststart", str(output)]
    run_ffmpeg(args, binary=binary)
    return output
