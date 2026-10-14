from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.ops.overlay import OverlayBase
from eks_harness.ir.time import TICKS_PER_SECOND, TimeContext, TimeError
from eks_harness.ir.types import MediaInfo
from eks_harness.media.edit.sources import fit_box
from eks_harness.media.edit.timing import ClipTiming, frame_of
from eks_harness.media.overlays.boxmap import BoxMap, RecordingBoxes
from eks_harness.media.overlays.targets import resolve_on_boxmap
from eks_harness.media.overlays.video import VideoOverlay

Box = tuple[float, float, float, float]
COORDINATE_TARGETS = frozenset({"point", "rect"})
FADE_SECONDS = 0.15
BoxesFor = Callable[[MediaInfo], BoxMap | RecordingBoxes | None]
ValuesFor = Callable[[ClipTiming], dict[str, Any]]


@dataclass(frozen=True)
class BlurItem:
    track: int
    box: Box
    radius: float
    start: float
    end: float


@dataclass(frozen=True)
class Emphasis:
    timing: ClipTiming
    first: int
    last: int
    mode: str


@dataclass
class TrackOverlays:
    track: int
    items: list[VideoOverlay]
    blurs: list[BlurItem]
    emphasis: list[Emphasis]


def _seconds(context: TimeContext, value: Any, where: str) -> float:
    try:
        return context.resolve(value) / TICKS_PER_SECOND
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"{where}: {error}") from error


def window(overlay: OverlayBase, context: TimeContext, total: float, where: str) -> tuple[float, float]:
    if overlay.window is not None:
        start = _seconds(context, overlay.window.start, f"{where} window start")
        end = _seconds(context, overlay.window.end, f"{where} window end")
    else:
        start = 0.0
        end = start + overlay.hold.ticks / TICKS_PER_SECOND if overlay.hold is not None else total
    if overlay.keep:
        end = total
    return max(0.0, start), min(total, end)


def _target(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True) if isinstance(value, BaseModel) else value


def _empty(size: tuple[int, int]) -> BoxMap:
    return BoxMap.from_json({"schema": "eks/boxes@1", "size": [size[0], size[1]], "scale": 1.0, "nodes": []})


def map_box(box: Box, timing: ClipTiming, info: MediaInfo, values: dict[str, Any], frame: int,
            size: tuple[int, int]) -> Box:
    if info.width is None or info.height is None:
        raise NodeFailure("args_invalid", f"clip {timing.clip.id} has no picture size to map its boxes")
    (box_w, box_h), _filters = fit_box(timing.clip.fit, (info.width, info.height), size)
    width, height = size
    if timing.clip.fit == "fill":
        fx, fy, ox, oy = width / info.width, height / info.height, 0.0, 0.0
    else:
        factor = (max if timing.clip.fit == "cover" else min)(width / info.width, height / info.height)
        scaled_w, scaled_h = info.width * factor, info.height * factor
        fx = fy = factor
        ox, oy = ((scaled_w - box_w) / 2.0, (scaled_h - box_h) / 2.0) if timing.clip.fit == "cover" else (0.0, 0.0)
    local = min(max(frame - timing.first, 0), timing.count - 1)
    scale = float(values["scale"][local])
    angle = math.radians(float(values["rotation"][local]))
    cx, cy = width / 2.0 + float(values["x"][local]), height / 2.0 + float(values["y"][local])
    x, y, w, h = box
    corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    mapped: list[tuple[float, float]] = []
    for px, py in corners:
        ix, iy = px * fx - ox - box_w / 2.0, py * fy - oy - box_h / 2.0
        rx = ix * math.cos(angle) - iy * math.sin(angle)
        ry = ix * math.sin(angle) + iy * math.cos(angle)
        mapped.append((cx + rx * scale, cy + ry * scale))
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


class OverlayPlanner:
    def __init__(self, *, context: TimeContext, fps: Fraction, size: tuple[int, int], total: int,
                 timings: Sequence[ClipTiming], infos: dict[str, MediaInfo], z_of: dict[int, int],
                 values_for: ValuesFor, boxes_for: BoxesFor, media: Callable[[Any], Path]) -> None:
        self.context = context
        self.fps = fps
        self.size = size
        self.total = total
        self.seconds = total / float(fps)
        self.timings = timings
        self.infos = infos
        self.z_of = z_of
        self.values_for = values_for
        self.boxes_for = boxes_for
        self.media = media

    def _under(self, track: int, frame: int) -> ClipTiming | None:
        candidates = [timing for timing in self.timings
                      if timing.covers(frame) and (self.z_of[timing.track_index], timing.track_index)
                      <= (self.z_of[track], track)]
        candidates.sort(key=lambda timing: (self.z_of[timing.track_index], timing.track_index), reverse=True)
        return candidates[0] if candidates else None

    def resolve(self, target: Any, track: int, start: float, where: str) -> Box:
        dump = _target(target)
        if dump.get("by") in COORDINATE_TARGETS:
            return resolve_on_boxmap(dump, _empty(self.size))
        frame = min(frame_of(round(start * TICKS_PER_SECOND), self.fps), self.total - 1)
        timing = self._under(track, frame)
        if timing is None:
            raise NodeFailure("target_not_found", f"{where}: no clip is visible under the overlay at {start:.3f}s, so "
                              "its target cannot be found; use by.point or by.rect, or place it over a recording")
        info = self.infos[timing.clip.id]
        boxes = self.boxes_for(info)
        if boxes is None:
            raise NodeFailure("content_unavailable", f"{where}: clip {timing.clip.id} ({info.uri}) has no box map, so "
                              "the target cannot be found on it; record it with eks or use by.point or by.rect")
        source_seconds = float(timing.source[frame - timing.first])
        found = boxes.at(source_seconds) if isinstance(boxes, RecordingBoxes) else boxes
        if found is None:
            raise NodeFailure("target_not_found", f"{where}: the recording of clip {timing.clip.id} has no box map "
                              f"at {source_seconds:.3f}s")
        box = resolve_on_boxmap(dump, found)
        return map_box(box, timing, info, self.values_for(timing), frame, self.size)

    def plan(self, track_index: int, overlays: Sequence[OverlayBase]) -> TrackOverlays:
        result = TrackOverlays(track=track_index, items=[], blurs=[], emphasis=[])
        for index, overlay in enumerate(overlays):
            where = f"overlay {index} ({overlay.kind}) of track {track_index}"
            start, end = window(overlay, self.context, self.seconds, where)
            if end <= start:
                continue
            anchor = getattr(overlay, "anchor", None)
            box = self.resolve(anchor, track_index, start, where) if anchor is not None else None
            if overlay.kind == "blur":
                radius = float(getattr(overlay, "radius", None) or 12)
                if box is None:
                    raise NodeFailure("args_invalid", f"{where}: a blur needs an anchor")
                result.blurs.append(BlurItem(track=track_index, box=box, radius=radius, start=start, end=end))
                continue
            to_target = getattr(overlay, "to", None)
            to_box = self.resolve(to_target, track_index, start, where) if to_target is not None else None
            image_value = getattr(overlay, "image", None)
            image = self.media(image_value) if image_value is not None else None
            result.items.append(VideoOverlay(overlay=overlay, start=start, end=end, anchor=box, to=to_box,
                                             image=image))
            if overlay.emphasis != "none":
                first = frame_of(round(start * TICKS_PER_SECOND), self.fps)
                last = frame_of(round(end * TICKS_PER_SECOND), self.fps)
                timing = self._under(track_index, first)
                if timing is not None:
                    result.emphasis.append(Emphasis(timing=timing, first=first, last=last, mode=overlay.emphasis))
        return result


def apply_emphasis(emphasis: Sequence[Emphasis]) -> None:
    for item in emphasis:
        timing = item.timing
        start = max(item.first, timing.first) - timing.first
        stop = min(item.last, timing.end) - timing.first
        if stop <= start:
            continue
        anchor = float(timing.source[start])
        if item.mode == "freeze":
            timing.source[start:stop] = anchor
        else:
            span = np.arange(stop - start, dtype=np.float64)
            following = timing.source[min(start + 1, timing.count - 1)] - anchor
            timing.source[start:stop] = anchor + span * following / 2.0


def blur_frame(pixels: np.ndarray, blurs: Sequence[BlurItem], seconds: float) -> np.ndarray:
    from scipy import ndimage

    out = pixels
    height, width = pixels.shape[:2]
    for item in blurs:
        if not item.start <= seconds < item.end:
            continue
        level = min(1.0, (seconds - item.start) / FADE_SECONDS, (item.end - seconds) / FADE_SECONDS)
        x0, y0 = max(0, int(item.box[0])), max(0, int(item.box[1]))
        x1 = min(width, math.ceil(item.box[0] + item.box[2]))
        y1 = min(height, math.ceil(item.box[1] + item.box[3]))
        if x1 <= x0 or y1 <= y0:
            continue
        if out is pixels:
            out = pixels.copy()
        region = out[y0:y1, x0:x1].astype(np.float64)
        blurred = ndimage.gaussian_filter(region, sigma=(item.radius / 2.0, item.radius / 2.0, 0), mode="nearest")
        mixed = region + (blurred - region) * max(level, 0.0)
        out[y0:y1, x0:x1] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
    return out


__all__ = ["BlurItem", "Emphasis", "OverlayPlanner", "TrackOverlays", "apply_emphasis", "blur_frame", "map_box",
           "window"]
