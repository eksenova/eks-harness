from __future__ import annotations

import bisect
import importlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import numpy as np

from eks_harness.media.gfx.job import ACTIVE, GfxJob, JobLayer, JobVariant, track_key
from eks_harness.media.gfx.kerning import kerning
from eks_harness.media.gfx.svgpath import parse_svg_path
from eks_harness.media.gfx.video import VideoStream

skia: Any = importlib.import_module("skia")

DEFAULT_LINE_HEIGHT = 1.2
BLUR_SIGMA_SCALE = 0.57735
BLUR_SIGMA_BIAS = 0.5
BLEND_MODES: Mapping[str, Any] = {
    "normal": skia.BlendMode.kSrcOver,
    "add": skia.BlendMode.kPlus,
    "multiply": skia.BlendMode.kMultiply,
    "screen": skia.BlendMode.kScreen,
    "overlay": skia.BlendMode.kOverlay,
    "src_atop": skia.BlendMode.kSrcATop,
    "dst_in": skia.BlendMode.kDstIn,
    "dst_out": skia.BlendMode.kDstOut,
    "xor": skia.BlendMode.kXor,
    "darken": skia.BlendMode.kDarken,
    "lighten": skia.BlendMode.kLighten,
    "color_dodge": skia.BlendMode.kColorDodge,
    "color_burn": skia.BlendMode.kColorBurn,
    "soft_light": skia.BlendMode.kSoftLight,
    "hard_light": skia.BlendMode.kHardLight,
    "difference": skia.BlendMode.kDifference,
    "exclusion": skia.BlendMode.kExclusion,
}
SAMPLING = skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear)


class RenderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_color(text: str) -> tuple[int, int, int, int]:
    raw = text.lstrip("#")
    if len(raw) == 6:
        raw += "ff"
    if len(raw) != 8:
        raise RenderError("args_invalid", f"{text!r} is not a #rrggbb or #rrggbbaa color")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16)


def color_int(text: str) -> int:
    red, green, blue, alpha = parse_color(text)
    return skia.Color(red, green, blue, alpha)


def blur_sigma(radius: float) -> float:
    return BLUR_SIGMA_SCALE * radius + BLUR_SIGMA_BIAS if radius > 0 else 0.0


@dataclass(frozen=True)
class TextLine:
    text: str
    positions: tuple[float, ...]
    width: float


@dataclass(frozen=True)
class TextLayout:
    lines: tuple[TextLine, ...]
    width: float
    height: float
    leading: float
    baseline: float


@dataclass(frozen=True)
class Geometry:
    width: float
    height: float
    pivot: tuple[float, float]
    shift: tuple[float, float]


class Renderer:
    def __init__(self, job: GfxJob, tracks: Mapping[str, np.ndarray[Any, Any]]) -> None:
        self.job = job
        self.tracks = tracks
        self.rate: Fraction = job.rate
        self.layers: dict[str, JobLayer] = {layer.id: layer for layer in job.layers}
        self.starts: dict[str, list[int]] = {
            layer.id: [variant.start for variant in layer.variants] for layer in job.layers}
        self.mask_sources: set[str] = {
            str(variant.record["mask"]) for layer in job.layers for variant in layer.variants
            if variant.record.get("mask")}
        self._typefaces: dict[str, Any] = {}
        self._images: dict[str, Any] = {}
        self._svgs: dict[str, Any] = {}
        self._paths: dict[str, Any] = {}
        self._layouts: dict[tuple[Any, ...], TextLayout] = {}
        self._streams: dict[tuple[str, int], VideoStream] = {}
        self._stream_origin: dict[tuple[str, int], int] = {}
        self.canvas: Any = None
        self.frame = 0

    def close(self) -> None:
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()

    def variant(self, layer_id: str, frame: int) -> tuple[int, JobVariant]:
        index = bisect.bisect_right(self.starts[layer_id], frame) - 1
        position = max(index, 0)
        return position, self.layers[layer_id].variants[position]

    def value(self, layer_id: str, prop: str) -> float:
        track = self.tracks.get(track_key(layer_id, prop))
        if track is None:
            return math.nan
        return float(track[self.frame])

    def color(self, layer_id: str, prop: str) -> tuple[float, float, float, float] | None:
        track = self.tracks.get(track_key(layer_id, prop))
        if track is None:
            return None
        row = track[self.frame]
        if bool(np.isnan(row).any()):
            return None
        return float(row[0]), float(row[1]), float(row[2]), float(row[3])

    def active(self, layer_id: str) -> bool:
        track = self.tracks.get(track_key(layer_id, ACTIVE))
        return bool(track[self.frame]) if track is not None else True

    def draw_frame(self, canvas: Any, frame: int) -> None:
        self.canvas = canvas
        self.frame = frame
        background = self.job.background
        canvas.clear(color_int(background) if background else skia.ColorTRANSPARENT)
        canvas.save()
        canvas.setMatrix(self.root_matrix())
        for root in self.job.roots:
            self.draw(root)
        canvas.restore()

    def root_matrix(self) -> Any:
        width, height = self.job.size
        return skia.Matrix.Translate(width / 2.0, height / 2.0)

    def draw(self, layer_id: str, *, as_mask: bool = False) -> None:
        if not as_mask and layer_id in self.mask_sources:
            return
        if not self.active(layer_id):
            return
        opacity = self.value(layer_id, "opacity")
        opacity = 1.0 if math.isnan(opacity) else min(max(opacity, 0.0), 1.0)
        if opacity <= 0.0:
            return
        layer = self.layers[layer_id]
        index, variant = self.variant(layer_id, self.frame)
        record = variant.record
        geometry = self.geometry(layer, variant)
        canvas = self.canvas
        canvas.save()
        canvas.concat(self.local_matrix(layer_id, geometry))
        blend = str(record.get("blend") or "normal")
        blur = self.value(layer_id, "blur")
        shadow = record.get("shadow")
        mask = record.get("mask")
        isolated = (layer.kind == "group" or opacity < 1.0 or blend != "normal" or bool(mask)
                    or (not math.isnan(blur) and blur > 0) or shadow is not None)
        if isolated:
            paint = skia.Paint()
            paint.setAlphaf(opacity)
            mode = BLEND_MODES.get(blend)
            if mode is None:
                raise RenderError("args_invalid", f"layer {layer_id!r} has unknown blend mode {blend!r}")
            paint.setBlendMode(mode)
            effect = self.image_filter(blur, shadow)
            if effect is not None:
                paint.setImageFilter(effect)
            canvas.saveLayer(None, paint)
        self.content(layer, index, variant, geometry)
        if mask:
            self.apply_mask(str(mask), str(record.get("mask_mode") or "alpha"))
        if isolated:
            canvas.restore()
        canvas.restore()

    def image_filter(self, blur: float, shadow: Any) -> Any:
        effect: Any = None
        if not math.isnan(blur) and blur > 0:
            sigma = blur_sigma(blur)
            effect = skia.ImageFilters.Blur(sigma, sigma, skia.TileMode.kDecal, None)
        if isinstance(shadow, Mapping):
            values = cast(Mapping[str, Any], shadow)
            sigma = blur_sigma(float(values.get("blur", 8.0)))
            effect = skia.ImageFilters.DropShadow(float(values.get("dx", 0.0)), float(values.get("dy", 4.0)), sigma,
                                                  sigma, color_int(str(values.get("color", "#00000080"))), effect)
        return effect

    def apply_mask(self, mask_id: str, mode: str) -> None:
        if mask_id not in self.layers:
            raise RenderError("args_invalid", f"mask layer {mask_id!r} is not a layer of the scene")
        canvas = self.canvas
        paint = skia.Paint()
        paint.setBlendMode(skia.BlendMode.kDstOut if mode.endswith("_inverted") else skia.BlendMode.kDstIn)
        if mode.startswith("luminance"):
            paint.setColorFilter(skia.LumaColorFilter.Make())
        canvas.saveLayer(None, paint)
        canvas.setMatrix(self.parent_matrix(mask_id))
        self.draw(mask_id, as_mask=True)
        canvas.restore()

    def parent_matrix(self, layer_id: str) -> Any:
        chain: list[str] = []
        parent = self.layers[layer_id].parent
        while parent is not None:
            chain.append(parent)
            parent = self.layers[parent].parent
        matrix = self.root_matrix()
        for ancestor in reversed(chain):
            _index, variant = self.variant(ancestor, self.frame)
            matrix = skia.Matrix.Concat(matrix, self.local_matrix(ancestor, self.geometry(self.layers[ancestor], variant)))
        return matrix

    def local_matrix(self, layer_id: str, geometry: Geometry) -> Any:
        x = _number(self.value(layer_id, "x"), 0.0)
        y = _number(self.value(layer_id, "y"), 0.0)
        scale = _number(self.value(layer_id, "scale"), 1.0)
        rotation = _number(self.value(layer_id, "rotation"), 0.0)
        matrix = skia.Matrix.Translate(x + geometry.shift[0], y + geometry.shift[1])
        matrix.preRotate(rotation)
        matrix.preScale(scale, scale)
        matrix.preTranslate(-geometry.pivot[0], -geometry.pivot[1])
        return matrix

    def geometry(self, layer: JobLayer, variant: JobVariant) -> Geometry:
        record = variant.record
        anchor = record.get("anchor") or (0.5, 0.5)
        ax, ay = float(anchor[0]), float(anchor[1])
        if layer.kind == "group":
            return Geometry(0.0, 0.0, (0.0, 0.0), (0.0, 0.0))
        if layer.kind == "path":
            bounds = self.path(str(record["d"])).computeTightBounds()
            pivot = (bounds.left() + ax * bounds.width(), bounds.top() + ay * bounds.height())
            return Geometry(bounds.width(), bounds.height(), pivot, pivot)
        width, height = self.box(layer, variant)
        return Geometry(width, height, (ax * width, ay * height), (0.0, 0.0))

    def box(self, layer: JobLayer, variant: JobVariant) -> tuple[float, float]:
        if layer.kind in ("rect", "ellipse"):
            return _number(self.value(layer.id, "width"), 0.0), _number(self.value(layer.id, "height"), 0.0)
        if layer.kind == "text":
            layout = self.layout(layer.id, variant)
            return layout.width, layout.height
        natural = self.natural_size(layer, variant)
        width = self.value(layer.id, "width")
        height = self.value(layer.id, "height")
        if math.isnan(width) and math.isnan(height):
            return natural
        if math.isnan(height):
            return width, width * natural[1] / natural[0] if natural[0] else 0.0
        if math.isnan(width):
            return height * natural[0] / natural[1] if natural[1] else 0.0, height
        return width, height

    def natural_size(self, layer: JobLayer, variant: JobVariant) -> tuple[float, float]:
        assets = variant.assets
        if layer.kind == "video":
            return float(assets["width"]), float(assets["height"])
        path = str(assets["path"])
        if assets.get("svg"):
            size = self.svg(path).containerSize()
            if size.width() <= 0 or size.height() <= 0:
                raise RenderError("content_unavailable", f"SVG image {Path(path).name} declares no width and height; "
                                  "give the image layer width= and height=")
            return float(size.width()), float(size.height())
        image = self.image(path)
        return float(image.width()), float(image.height())

    def content(self, layer: JobLayer, index: int, variant: JobVariant, geometry: Geometry) -> None:
        kind = layer.kind
        canvas = self.canvas
        if kind == "group":
            for child in layer.children:
                self.draw(child)
        elif kind == "rect":
            radius = max(_number(self.value(layer.id, "radius"), 0.0), 0.0)
            rect = skia.Rect.MakeWH(geometry.width, geometry.height)
            self.shape(layer.id, variant, lambda paint: canvas.drawRRect(skia.RRect.MakeRectXY(rect, radius, radius),
                                                                          paint))
        elif kind == "ellipse":
            rect = skia.Rect.MakeWH(geometry.width, geometry.height)
            self.shape(layer.id, variant, lambda paint: canvas.drawOval(rect, paint))
        elif kind == "path":
            path = self.trimmed(layer.id, str(variant.record["d"]))
            self.shape(layer.id, variant, lambda paint: canvas.drawPath(path, paint))
        elif kind == "text":
            self.text(layer.id, variant)
        elif kind == "image":
            self.picture(layer.id, variant, geometry)
        elif kind == "video":
            self.video(layer.id, index, variant, geometry)
        else:
            raise RenderError("args_invalid", f"layer {layer.id!r} has unknown kind {kind!r}")

    def shape(self, layer_id: str, variant: JobVariant, draw: Callable[[Any], None]) -> None:
        fill = self.paint(layer_id, variant, "fill")
        if fill is not None:
            fill.setStyle(skia.Paint.kFill_Style)
            draw(fill)
        width = _number(self.value(layer_id, "stroke_width"), 0.0)
        stroke = self.paint(layer_id, variant, "stroke") if width > 0 else None
        if stroke is not None:
            stroke.setStyle(skia.Paint.kStroke_Style)
            stroke.setStrokeWidth(width)
            draw(stroke)

    def paint(self, layer_id: str, variant: JobVariant, prop: str) -> Any:
        color = self.color(layer_id, prop)
        if color is not None:
            paint = skia.Paint(AntiAlias=True)
            paint.setColor4f(skia.Color4f(*color))
            return paint
        gradient = variant.record.get(prop)
        if isinstance(gradient, Mapping):
            paint = skia.Paint(AntiAlias=True)
            paint.setShader(self.gradient(cast(Mapping[str, Any], gradient)))
            return paint
        return None

    def gradient(self, spec: Mapping[str, Any]) -> Any:
        stops = [(float(offset), color_int(str(color))) for offset, color in spec["stops"]]
        positions = [offset for offset, _color in stops]
        colors = [color for _offset, color in stops]
        if spec.get("kind") == "radial":
            center = spec["center"]
            return skia.GradientShader.MakeRadial(skia.Point(float(center[0]), float(center[1])),
                                                  float(spec["radius"]), colors, positions)
        start, end = spec["start"], spec["end"]
        return skia.GradientShader.MakeLinear([skia.Point(float(start[0]), float(start[1])),
                                               skia.Point(float(end[0]), float(end[1]))], colors, positions)

    def path(self, data: str) -> Any:
        found = self._paths.get(data)
        if found is None:
            try:
                found = parse_svg_path(data)
            except ValueError as error:
                raise RenderError("args_invalid", str(error)) from error
            self._paths[data] = found
        return found

    def trimmed(self, layer_id: str, data: str) -> Any:
        path = self.path(data)
        start = min(max(_number(self.value(layer_id, "trim_start"), 0.0), 0.0), 1.0)
        end = min(max(_number(self.value(layer_id, "trim_end"), 1.0), 0.0), 1.0)
        if start <= 0.0 and end >= 1.0:
            return path
        result = skia.Path()
        if end <= start:
            return result
        lengths: list[float] = []
        measure = skia.PathMeasure(path, False)
        while True:
            lengths.append(measure.getLength())
            if not measure.nextContour():
                break
        total = sum(lengths)
        if total <= 0:
            return result
        low, high = start * total, end * total
        offset = 0.0
        measure = skia.PathMeasure(path, False)
        for length in lengths:
            begin, stop = max(low - offset, 0.0), min(high - offset, length)
            if stop > begin:
                measure.getSegment(begin, stop, result, True)
            offset += length
            if not measure.nextContour():
                break
        return result

    def typeface(self, path: str) -> Any:
        found = self._typefaces.get(path)
        if found is None:
            found = skia.Typeface.MakeFromFile(path)
            if found is None:
                raise RenderError("font_unavailable", f"{Path(path).name} is not a font file Skia can read")
            self._typefaces[path] = found
        return found

    def font(self, path: str, size: float) -> Any:
        font = skia.Font(self.typeface(path), size)
        font.setEdging(skia.Font.Edging.kAntiAlias)
        font.setHinting(skia.FontHinting.kNone)
        font.setSubpixel(True)
        font.setLinearMetrics(True)
        return font

    def layout(self, layer_id: str, variant: JobVariant) -> TextLayout:
        record = variant.record
        size = max(_number(self.value(layer_id, "size"), 64.0), 0.0)
        tracking = _number(self.value(layer_id, "tracking"), 0.0)
        font_file = str(variant.assets["font"])
        max_width = record.get("max_width")
        line_height = float(record.get("line_height") or DEFAULT_LINE_HEIGHT)
        key = (str(record.get("text", "")), font_file, size, tracking, max_width, line_height)
        found = self._layouts.get(key)
        if found is not None:
            return found
        font = self.font(font_file, size)
        table = kerning(font_file)
        scale = size / table.units_per_em

        def place(text: str) -> TextLine:
            glyphs = font.textToGlyphs(text)
            widths = font.getWidths(glyphs)
            positions: list[float] = []
            cursor = 0.0
            for position, glyph in enumerate(glyphs):
                if position:
                    cursor += widths[position - 1] + tracking + table.adjust(glyphs[position - 1], glyph) * scale
                positions.append(cursor)
            width = cursor + widths[-1] if len(glyphs) else 0.0
            return TextLine(text, tuple(positions), width)

        lines: list[TextLine] = []
        for paragraph in key[0].split("\n"):
            if max_width is None:
                lines.append(place(paragraph))
                continue
            words = paragraph.split(" ")
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if place(candidate).width <= float(max_width) or not current:
                    current = candidate
                else:
                    lines.append(place(current))
                    current = word
            lines.append(place(current))
        metrics = font.getMetrics()
        leading = line_height * size
        ascent, descent = -float(metrics.fAscent), float(metrics.fDescent)
        baseline = (leading - (ascent + descent)) / 2.0 + ascent
        width = float(max_width) if max_width is not None else max((line.width for line in lines), default=0.0)
        found = TextLayout(tuple(lines), width, leading * len(lines), leading, baseline)
        self._layouts[key] = found
        return found

    def text(self, layer_id: str, variant: JobVariant) -> None:
        layout = self.layout(layer_id, variant)
        color = self.color(layer_id, "color")
        if color is None:
            return
        size = max(_number(self.value(layer_id, "size"), 64.0), 0.0)
        font = self.font(str(variant.assets["font"]), size)
        paint = skia.Paint(AntiAlias=True)
        paint.setColor4f(skia.Color4f(*color))
        align = str(variant.record.get("align") or "center")
        for row, line in enumerate(layout.lines):
            if not line.text:
                continue
            if align == "left":
                offset = 0.0
            elif align == "right":
                offset = layout.width - line.width
            else:
                offset = (layout.width - line.width) / 2.0
            blob = skia.TextBlob.MakeFromPosTextH(line.text, [offset + x for x in line.positions],
                                                  row * layout.leading + layout.baseline, font)
            if blob is not None:
                self.canvas.drawTextBlob(blob, 0.0, 0.0, paint)

    def image(self, path: str) -> Any:
        found = self._images.get(path)
        if found is None:
            found = skia.Image.MakeFromEncoded(skia.Data.MakeFromFileName(path))
            if found is None:
                raise RenderError("content_unavailable", f"{Path(path).name} is not an image Skia can decode")
            self._images[path] = found
        return found

    def svg(self, path: str) -> Any:
        found = self._svgs.get(path)
        if found is None:
            found = skia.SVGDOM.MakeFromStream(skia.Stream.MakeFromFile(path))
            if found is None:
                raise RenderError("content_unavailable", f"{Path(path).name} is not an SVG document Skia can read")
            self._svgs[path] = found
        return found

    def fitted(self, natural: tuple[float, float], geometry: Geometry, fit: str) -> Any:
        width, height = geometry.width, geometry.height
        if natural[0] <= 0 or natural[1] <= 0 or width <= 0 or height <= 0:
            return None
        sx, sy = width / natural[0], height / natural[1]
        if fit == "contain":
            sx = sy = min(sx, sy)
        elif fit == "cover":
            sx = sy = max(sx, sy)
        dx, dy = (width - natural[0] * sx) / 2.0, (height - natural[1] * sy) / 2.0
        return skia.Rect.MakeXYWH(dx, dy, natural[0] * sx, natural[1] * sy)

    def picture(self, layer_id: str, variant: JobVariant, geometry: Geometry) -> None:
        natural = self.natural_size(self.layers[layer_id], variant)
        target = self.fitted(natural, geometry, str(variant.record.get("fit") or "contain"))
        if target is None:
            return
        canvas = self.canvas
        canvas.save()
        canvas.clipRect(skia.Rect.MakeWH(geometry.width, geometry.height), skia.ClipOp.kIntersect, True)
        path = str(variant.assets["path"])
        if variant.assets.get("svg"):
            dom = self.svg(path)
            canvas.translate(target.left(), target.top())
            canvas.scale(target.width() / natural[0], target.height() / natural[1])
            dom.render(canvas)
        else:
            canvas.drawImageRect(self.image(path), target, SAMPLING, skia.Paint(AntiAlias=True))
        canvas.restore()

    def video(self, layer_id: str, index: int, variant: JobVariant, geometry: Geometry) -> None:
        assets = variant.assets
        key = (layer_id, index)
        stream = self._streams.get(key)
        if stream is None:
            speed = float(variant.record.get("speed") or 1.0)
            local = self.frame / float(self.rate) - float(assets.get("start", 0.0))
            source_start = float(assets.get("src_start", 0.0)) + max(local, 0.0) * speed
            end = assets.get("src_end")
            length = float(end) - source_start if end is not None else None
            if length is not None and length <= 0:
                return
            stream = VideoStream(Path(str(assets["path"])), width=int(assets["width"]), height=int(assets["height"]),
                                 start=source_start, length=length, speed=speed, fps=self.rate,
                                 threads=self.job.decode_threads)
            self._streams[key] = stream
            self._stream_origin[key] = self.frame
        pixels = stream.frame(self.frame - self._stream_origin[key])
        if pixels is None:
            return
        image = skia.Image.fromarray(np.ascontiguousarray(pixels), skia.ColorType.kRGBA_8888_ColorType)
        natural = (float(assets["width"]), float(assets["height"]))
        target = self.fitted(natural, geometry, str(variant.record.get("fit") or "cover"))
        if target is None:
            return
        canvas = self.canvas
        canvas.save()
        canvas.clipRect(skia.Rect.MakeWH(geometry.width, geometry.height), skia.ClipOp.kIntersect, True)
        canvas.drawImageRect(image, target, SAMPLING, skia.Paint(AntiAlias=True))
        canvas.restore()


def _number(value: float, default: float) -> float:
    return default if math.isnan(value) else value


__all__ = ["BLEND_MODES", "RenderError", "Renderer", "blur_sigma", "color_int", "parse_color"]
