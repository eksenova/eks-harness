from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.ops.overlay import Blur, OverlayBase, Step
from eks_harness.ir.presets import OverlayStyle
from eks_harness.media.files import (
    BITEXACT_OUTPUT,
    FFMPEG_TIMEOUT,
    MediaToolError,
    tool,
)
from eks_harness.media.overlays.boxmap import Box
from eks_harness.media.overlays.draw import (
    DEFAULT_STYLE,
    ResolvedStyle,
    draw,
    rasterize,
    resolve_style,
    svg_document,
)
from eks_harness.media.overlays.targets import resolve_on_boxmap

FADE_SECONDS = 0.15
ALPHA_STEPS = 255

FloatArray = NDArray[np.float32]

__all__ = ["FADE_SECONDS", "VideoOverlay", "blur_filter", "render_overlay_video", "resolve_on_boxmap"]


@dataclass(frozen=True)
class VideoOverlay:
    overlay: OverlayBase
    start: float
    end: float
    anchor: tuple[float, float, float, float] | None = None
    to: tuple[float, float, float, float] | None = None
    image: Path | None = None


@dataclass(frozen=True, slots=True)
class _Layer:
    start: float
    end: float
    top: int
    left: int
    rgb: FloatArray
    alpha: FloatArray


def fade(start: float, end: float, seconds: float) -> float:
    if seconds < start or seconds >= end:
        return 0.0
    return max(0.0, min(1.0, (seconds - start) / FADE_SECONDS, (end - seconds) / FADE_SECONDS))


def _style_for(overlay: OverlayBase, default: OverlayStyle | None, cache: dict[str, ResolvedStyle]) -> ResolvedStyle:
    if overlay.style is not None:
        key = f"preset:{overlay.style}"
        if key not in cache:
            cache[key] = resolve_style(overlay.style)
        return cache[key]
    key = "default"
    if key not in cache:
        cache[key] = resolve_style("track" if default is not None else DEFAULT_STYLE, model=default)
    return cache[key]


def _layer(item: VideoOverlay, png: bytes) -> _Layer | None:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as opened:
        pixels = np.asarray(opened.convert("RGBA"), dtype=np.float32) / 255.0
    alpha = pixels[:, :, 3]
    rows = np.flatnonzero(alpha.max(axis=1) > 0)
    cols = np.flatnonzero(alpha.max(axis=0) > 0)
    if rows.size == 0 or cols.size == 0:
        return None
    top, bottom, left, right = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    patch_alpha = np.ascontiguousarray(alpha[top:bottom, left:right])
    patch_rgb = np.ascontiguousarray(pixels[top:bottom, left:right, :3] * patch_alpha[:, :, None], dtype=np.float32)
    return _Layer(start=item.start, end=item.end, top=top, left=left, rgb=patch_rgb, alpha=patch_alpha)


def _rasterize(overlays: Sequence[VideoOverlay], size: tuple[int, int], style: OverlayStyle | None) -> list[_Layer]:
    cache: dict[str, ResolvedStyle] = {}
    layers: list[_Layer] = []
    step = 0
    ordered = sorted(enumerate(overlays), key=lambda pair: (pair[1].start, pair[0]))
    steps: dict[int, int] = {}
    for index, item in ordered:
        if isinstance(item.overlay, Step):
            step = item.overlay.index if item.overlay.index is not None else step + 1
            steps[index] = step
    for index, item in enumerate(overlays):
        if item.end <= item.start:
            raise NodeFailure("args_invalid", f"video overlay {index} ({item.overlay.kind}) ends at {item.end}s, "
                              f"not after its start {item.start}s")
        if isinstance(item.overlay, Blur):
            continue
        resolved = _style_for(item.overlay, style, cache)
        drawing = draw(item.overlay, key=f"v{index}", anchor=item.anchor, to=item.to, image=item.image,
                       style=resolved, size=size, step=steps.get(index), renderer="video")
        if not drawing.fragments:
            continue
        png = rasterize(svg_document(drawing.fragments, size), size, resolved.fonts)
        layer = _layer(item, png)
        if layer is not None:
            layers.append(layer)
    return layers


def _frame(layers: Sequence[tuple[_Layer, float]], size: tuple[int, int]) -> bytes:
    width, height = size
    rgb = np.zeros((height, width, 3), dtype=np.float32)
    alpha = np.zeros((height, width), dtype=np.float32)
    for layer, weight in layers:
        rows = slice(layer.top, layer.top + layer.alpha.shape[0])
        cols = slice(layer.left, layer.left + layer.alpha.shape[1])
        source_alpha = layer.alpha * np.float32(weight)
        keep = np.float32(1.0) - source_alpha
        rgb[rows, cols] = layer.rgb * np.float32(weight) + rgb[rows, cols] * keep[:, :, None]
        alpha[rows, cols] = source_alpha + alpha[rows, cols] * keep
    out = np.zeros((height, width, 4), dtype=np.uint8)
    shown = alpha > 0
    safe = np.where(shown, alpha, np.float32(1.0))
    straight = np.clip(rgb / safe[:, :, None], 0.0, 1.0)
    out[:, :, :3] = np.rint(straight * 255.0).astype(np.uint8)
    out[:, :, 3] = np.rint(np.clip(alpha, 0.0, 1.0) * 255.0).astype(np.uint8)
    out[~shown] = 0
    return out.tobytes()


def _writer(size: tuple[int, int], fps: Fraction, out: Path, threads: int) -> subprocess.Popen[bytes]:
    width, height = size
    argv = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-threads", str(threads),
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-framerate", f"{fps.numerator}/{fps.denominator}",
            "-i", "pipe:0", "-c:v", "png", "-pix_fmt", "rgba", *BITEXACT_OUTPUT, "-threads", str(threads),
            "-f", "mov", str(out)]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def render_overlay_video(overlays: Sequence[VideoOverlay], *, size: tuple[int, int], fps: Fraction, frames: int,
                         style: OverlayStyle | None, out: Path, threads: int = 1) -> Path:
    if size[0] <= 0 or size[1] <= 0 or frames <= 0 or fps <= 0:
        raise NodeFailure("args_invalid", f"overlay video needs a size, fps and frame count, got {size}, {fps}, "
                          f"{frames}")
    layers = _rasterize(overlays, size, style)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = _writer(size, fps, out, threads)
    stdin = writer.stdin
    if stdin is None:
        raise MediaToolError("executor_error", "ffmpeg did not open its input pipe")
    previous: tuple[tuple[int, int], ...] | None = None
    data = b""
    try:
        for index in range(frames):
            seconds = float(Fraction(index) / fps)
            active: list[tuple[_Layer, float]] = []
            key: list[tuple[int, int]] = []
            for number, layer in enumerate(layers):
                weight = fade(layer.start, layer.end, seconds)
                level = round(weight * ALPHA_STEPS)
                if level > 0:
                    active.append((layer, level / ALPHA_STEPS))
                    key.append((number, level))
            current = tuple(key)
            if current != previous:
                data = _frame(active, size)
                previous = current
            stdin.write(data)
        stdin.close()
    except BrokenPipeError:
        pass
    try:
        _, errors = writer.communicate(timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        writer.kill()
        raise MediaToolError("timeout", f"ffmpeg did not finish the overlay video in {FFMPEG_TIMEOUT:.0f}s") from error
    if writer.returncode != 0:
        raise MediaToolError("executor_error",
                             f"ffmpeg failed to write the overlay video ({writer.returncode}): "
                             f"{errors.decode('utf-8', 'replace')[-1500:]}")
    return out


def blur_filter(overlays: Sequence[VideoOverlay], *, size: tuple[int, int], source: str = "0:v",
                output: str = "blurred") -> str | None:
    chains: list[str] = []
    current = source
    count = 0
    for index, item in enumerate(overlays):
        overlay = item.overlay
        if not isinstance(overlay, Blur):
            continue
        if item.anchor is None:
            raise NodeFailure("target_not_found", f"video blur overlay {index} has no resolved anchor box")
        x, y, w, h = _clip(item.anchor, size)
        if w <= 0 or h <= 0:
            continue
        radius = overlay.radius if overlay.radius is not None else 8
        sigma = max(0.5, radius / 2)
        start, end = item.start, item.end
        fade_out = max(start, end - FADE_SECONDS)
        label = f"blur{count}"
        target = f"{label}o"
        chains.append(
            f"[{current}]split=2[{label}a][{label}b];"
            f"[{label}b]crop={w}:{h}:{x}:{y},gblur=sigma={sigma:g},format=yuva444p,"
            f"fade=t=in:st={start:.6f}:d={FADE_SECONDS}:alpha=1,"
            f"fade=t=out:st={fade_out:.6f}:d={FADE_SECONDS}:alpha=1[{label}c];"
            f"[{label}a][{label}c]overlay={x}:{y}:enable='between(t,{start:.6f},{end:.6f})'[{target}]")
        current = target
        count += 1
    if not chains:
        return None
    chains.append(f"[{current}]null[{output}]")
    return ";".join(chains)


def _clip(box: Box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = box
    left, top = max(0, round(x)), max(0, round(y))
    right, bottom = min(size[0], round(x + w)), min(size[1], round(y + h))
    return left, top, right - left, bottom - top
