from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from eks_harness.review.markers import Marker, Markers
from eks_harness.review.media import VideoInfo, audio_samples, probe, select_frames

MAX_EDGE = 2000
PAD = 6
HEADER = 34
LABEL = 22
STRIP = 168
MIN_FRAMES = 12
MAX_FRAMES = 96
BG = (20, 21, 24)
INK = (232, 233, 236)
MUTED = (150, 154, 162)
WAVE = (96, 140, 196)
RMS = (160, 196, 240)
CUE = (255, 176, 32)
KIND_COLORS = {"beat": (88, 92, 102), "downbeat": (210, 212, 218), "cue": CUE, "frame": CUE}
OTHER = (96, 210, 200)
MONO_FONTS = ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf")


@dataclass
class Tile:
    frame: int
    t: float
    cue: str | None = None
    regular: bool = True

    def as_dict(self) -> dict:
        out = {"frame": self.frame, "t": round(self.t, 3)}
        if self.cue is not None:
            out["cue"] = self.cue
        return out


@dataclass
class Sheet:
    path: Path
    part: int
    parts: int
    start: float
    end: float
    tiles: list[Tile] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"path": str(self.path), "part": self.part, "parts": self.parts, "range": [round(self.start, 3),
                round(self.end, 3)], "tiles": [t.as_dict() for t in self.tiles]}


@dataclass
class SheetResult:
    video: VideoInfo
    sheets: list[Sheet]

    def as_dict(self) -> dict:
        return {"video": self.video.as_dict(), "sheets": [s.as_dict() for s in self.sheets]}


@dataclass
class Layout:
    tile_w: int
    tile_h: int
    cols: int
    rows_max: int

    @property
    def capacity(self) -> int:
        return self.cols * self.rows_max


def auto_frame_count(duration: float) -> int:
    return max(MIN_FRAMES, min(MAX_FRAMES, round(duration * 2)))


def layout_for(width: int, height: int, max_edge: int = MAX_EDGE) -> Layout:
    aspect = width / max(1, height)
    if aspect >= 1.3:
        tile_w = 320
    elif aspect >= 0.9:
        tile_w = 240
    else:
        tile_w = 170
    tile_w = min(tile_w, (max_edge - PAD) // 2 - PAD)
    tile_h = max(24, round(tile_w / aspect))
    cols = max(1, (max_edge - PAD) // (tile_w + PAD))
    rows_max = max(1, (max_edge - HEADER - STRIP - PAD) // (tile_h + LABEL + PAD))
    return Layout(tile_w=tile_w, tile_h=tile_h, cols=cols, rows_max=rows_max)


def plan_tiles(info: VideoInfo, count: int, frames: list[Marker]) -> list[Tile]:
    last = max(0, info.frames - 1)
    by_frame: dict[int, Tile] = {}
    if count > 0 and info.frames > 0:
        for k in range(count):
            target = 0 if count == 1 else round(k * last / (count - 1))
            by_frame.setdefault(target, Tile(frame=target, t=info.time_of(target)))
    for marker in frames:
        if marker.t < 0 or marker.t > info.duration + 1e-6:
            continue
        number = info.frame_at(marker.t)
        tile = by_frame.get(number)
        label = marker.label or f"{marker.t:.2f}s"
        if tile is None:
            by_frame[number] = Tile(frame=number, t=info.time_of(number), cue=label, regular=False)
        else:
            tile.cue = label if tile.cue is None else f"{tile.cue}, {label}"
    return [by_frame[k] for k in sorted(by_frame)]


def split(tiles: list[Tile], capacity: int) -> list[list[Tile]]:
    if not tiles:
        return [[]]
    parts = max(1, math.ceil(len(tiles) / capacity))
    size = math.ceil(len(tiles) / parts)
    return [tiles[i: i + size] for i in range(0, len(tiles), size)]


def _font(size: int) -> Any:
    from PIL import ImageFont

    for candidate in MONO_FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _nice_step(span: float, pixels: int) -> float:
    target = span / max(1, pixels / 90)
    for step in (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if step >= target:
            return float(step)
    return 1200.0


def _envelope(samples: np.ndarray, rate: int, start: float, end: float, columns: int) -> tuple[np.ndarray, ...]:
    a = max(0, int(start * rate))
    b = min(samples.size, max(a, int(end * rate)))
    chunk = samples[a:b]
    if chunk.size < columns or columns <= 0:
        zero = np.zeros(max(columns, 0), dtype=np.float32)
        return zero, zero, zero
    edges = np.linspace(0, chunk.size, columns + 1).astype(int)
    lows = np.minimum.reduceat(chunk, edges[:-1])
    highs = np.maximum.reduceat(chunk, edges[:-1])
    squares = np.add.reduceat(chunk.astype(np.float64) ** 2, edges[:-1])
    widths = np.maximum(1, np.diff(edges))
    rms = np.sqrt(squares / widths).astype(np.float32)
    return lows, highs, rms


def _draw_strip(draw: Any, box: tuple[int, int, int, int], start: float, end: float, samples: np.ndarray,
                rate: int, ticks: list[Marker], tiles: list[Tile], has_audio: bool) -> None:
    x0, y0, x1, y1 = box
    small = _font(13)
    draw.rectangle(box, fill=(14, 15, 17))
    axis_h = 20
    wave_top, wave_bottom = y0 + 18, y1 - axis_h
    mid = (wave_top + wave_bottom) // 2
    half = (wave_bottom - wave_top) // 2 - 2
    width = x1 - x0
    span = max(1e-6, end - start)

    def x_of(t: float) -> int:
        return x0 + round((t - start) / span * width)

    if has_audio and samples.size:
        lows, highs, rms = _envelope(samples, rate, start, end, width)
        peak = float(max(np.max(np.abs(highs)) if highs.size else 0, np.max(np.abs(lows)) if lows.size else 0, 1e-4))
        scale = half / max(peak, 0.05)
        for i in range(width):
            draw.line([(x0 + i, mid - int(highs[i] * scale)), (x0 + i, mid - int(lows[i] * scale))], fill=WAVE)
        points = [(x0 + i, mid - int(rms[i] * scale)) for i in range(width)]
        if len(points) > 1:
            draw.line(points, fill=RMS, width=1)
        draw.text((x0 + 6, y0 + 2), f"audio {start:.2f}-{end:.2f}s  peak {20 * math.log10(peak):.1f} dBFS",
                  fill=MUTED, font=small)
    else:
        draw.line([(x0, mid), (x1, mid)], fill=(60, 62, 68))
        draw.text((x0 + 6, y0 + 2), f"no audio  {start:.2f}-{end:.2f}s", fill=MUTED, font=small)
    order = {"beat": 0, "downbeat": 1}
    labelled_until = -1
    for marker in sorted((m for m in ticks if start <= m.t <= end), key=lambda m: (order.get(m.kind, 2), m.t)):
        x = x_of(marker.t)
        color = KIND_COLORS.get(marker.kind, OTHER)
        if marker.kind == "beat":
            draw.line([(x, wave_bottom - 14), (x, wave_bottom)], fill=color)
        elif marker.kind == "downbeat":
            draw.line([(x, wave_top + 10), (x, wave_bottom)], fill=color, width=2)
        else:
            draw.line([(x, wave_top), (x, wave_bottom)], fill=color, width=2)
            if marker.label and x > labelled_until:
                text = marker.label[:24]
                draw.text((x + 3, wave_top), text, fill=color, font=small)
                labelled_until = x + int(draw.textlength(text, font=small)) + 6
    draw.line([(x0, wave_bottom), (x1, wave_bottom)], fill=(70, 72, 80))
    step = _nice_step(span, width)
    first = math.ceil(start / step) * step
    t = first
    while t <= end + 1e-9:
        x = x_of(t)
        draw.line([(x, wave_bottom), (x, wave_bottom + 5)], fill=MUTED)
        draw.text((x + 2, wave_bottom + 4), f"{t:g}s", fill=MUTED, font=small)
        t = round(t + step, 6)
    for index, tile in enumerate(tiles):
        x = x_of(tile.t)
        color = CUE if tile.cue is not None else INK
        draw.polygon([(x - 3, y1 - 1), (x + 3, y1 - 1), (x, y1 - 6)], fill=color)


def render_sheet(info: VideoInfo, tiles: list[Tile], layout: Layout, out: Path, *, part: int, parts: int,
                 start: float, end: float, samples: np.ndarray, rate: int, ticks: list[Marker],
                 first_index: int, title: str) -> Sheet:
    from PIL import Image, ImageDraw

    cols = min(layout.cols, max(1, len(tiles)))
    rows = max(1, math.ceil(len(tiles) / cols))
    width = max(cols * (layout.tile_w + PAD) + PAD, 900)
    height = HEADER + rows * (layout.tile_h + LABEL + PAD) + PAD + STRIP
    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)
    font = _font(15)
    head = (f"{title}  {info.width}x{info.height}  {info.fps:.2f} fps  {info.duration:.2f}s  {info.frames} frames"
            f"  sheet {part}/{parts}  {start:.2f}-{end:.2f}s")
    draw.text((PAD + 2, 9), head, fill=INK, font=font)
    images = select_frames(info, [t.frame for t in tiles], layout.tile_w, layout.tile_h)
    label_font = _font(14)
    for n, tile in enumerate(tiles):
        x = PAD + (n % cols) * (layout.tile_w + PAD)
        y = HEADER + (n // cols) * (layout.tile_h + LABEL + PAD)
        image = images.get(tile.frame)
        if image is not None:
            sheet.paste(image, (x, y + LABEL))
        else:
            draw.rectangle([x, y + LABEL, x + layout.tile_w, y + LABEL + layout.tile_h], fill=(60, 20, 20))
        cue = tile.cue is not None
        draw.rectangle([x, y, x + layout.tile_w - 1, y + LABEL - 1], fill=CUE if cue else (34, 36, 41))
        text = f"#{first_index + n:02d} {tile.t:.3f}s f{tile.frame}"
        if cue:
            text += f" {tile.cue}"
        limit = max(8, layout.tile_w // 8)
        draw.text((x + 4, y + 3), text[:limit], fill=(16, 16, 16) if cue else INK, font=label_font)
        if cue:
            draw.rectangle([x - 2, y - 2, x + layout.tile_w + 1, y + LABEL + layout.tile_h + 1], outline=CUE, width=2)
    _draw_strip(draw, (PAD, height - STRIP, width - PAD, height - PAD), start, end, samples, rate, ticks, tiles,
                info.has_audio)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        sheet.save(out, quality=90)
    else:
        sheet.save(out)
    return Sheet(path=out, part=part, parts=parts, start=start, end=end, tiles=tiles)


def make_sheets(video: Path | VideoInfo, out_dir: Path, *, frames: int | None = None,
                markers: Markers | None = None, max_edge: int = MAX_EDGE, name: str | None = None,
                fmt: str = "jpg") -> SheetResult:
    info = video if isinstance(video, VideoInfo) else probe(Path(video))
    markers = markers or Markers([], [])
    count = frames if frames is not None else auto_frame_count(info.duration)
    tiles = plan_tiles(info, max(0, count), markers.frames)
    layout = layout_for(info.width, info.height, max_edge)
    groups = split(tiles, layout.capacity)
    rate = 8000
    samples = audio_samples(info, rate) if info.has_audio else np.zeros(0, dtype=np.float32)
    stem = name or info.path.stem
    sheets: list[Sheet] = []
    first = 0
    for index, group in enumerate(groups):
        start = 0.0 if index == 0 else group[0].t
        end = info.duration if index == len(groups) - 1 else groups[index + 1][0].t
        suffix = "" if len(groups) == 1 else f"-{index + 1}of{len(groups)}"
        out = Path(out_dir) / f"{stem}-sheet{suffix}.{fmt}"
        sheets.append(render_sheet(info, group, layout, out, part=index + 1, parts=len(groups), start=start,
                                   end=end, samples=samples, rate=rate, ticks=markers.ticks, first_index=first,
                                   title=stem))
        first += len(group)
    return SheetResult(video=info, sheets=sheets)
