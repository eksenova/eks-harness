from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import numpy as np
from pydantic import JsonValue, TypeAdapter, ValidationError

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog.animated import AColor, AFloat
from eks_harness.ir.catalog.easing import Easing
from eks_harness.ir.ops.gfx import (
    EllipseLayer,
    GroupLayer,
    ImageLayer,
    PathLayer,
    RectLayer,
    TextLayer,
    VideoLayer,
)
from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    Time,
    TimeContext,
    TimeError,
    TimeSet,
    format_fps,
    parse_fps,
)
from eks_harness.media.animate import (
    AnimateError,
    Floats,
    Timebase,
    colors,
    ease,
    floats,
)
from eks_harness.media.fonts import font_path
from eks_harness.media.gfx.job import (
    ACTIVE,
    COLOR_PROPS,
    FLOAT_PROPS,
    GfxJob,
    JobLayer,
    JobVariant,
    track_key,
)

MediaPath = Callable[[JsonValue], Path]
Probe = Callable[[Path], tuple[int, int]]
KIND_MODELS: dict[str, type[Any]] = {
    "text": TextLayer,
    "rect": RectLayer,
    "ellipse": EllipseLayer,
    "path": PathLayer,
    "image": ImageLayer,
    "video": VideoLayer,
    "group": GroupLayer,
}
GRADIENT_KINDS = frozenset({"linear", "radial"})
SVG_SUFFIX = ".svg"
_FLOAT: TypeAdapter[Any] = TypeAdapter(cast(Any, AFloat))
_COLOR: TypeAdapter[Any] = TypeAdapter(cast(Any, AColor))
_TIME: TypeAdapter[Time] = TypeAdapter(cast(Any, Time))
_TIMESET: TypeAdapter[TimeSet] = TypeAdapter(cast(Any, TimeSet))
_EASING: TypeAdapter[Any] = TypeAdapter(cast(Any, Easing))


@dataclass(frozen=True)
class _Fire:
    ticks: int
    frame: int
    position: int
    action: Mapping[str, Any]


@dataclass
class Compiled:
    job: GfxJob
    tracks: dict[str, np.ndarray[Any, Any]] = field(default_factory=dict[str, "np.ndarray[Any, Any]"])


class _Compiler:
    def __init__(self, scene: Mapping[str, Any], context: TimeContext, media: MediaPath, probe: Probe) -> None:
        self.scene = scene
        self.context = context
        self.media = media
        self.probe = probe
        self.seed = int(scene.get("seed") or 0)
        self.origin = context.start
        self.fps = self._fps()
        base = Timebase(context=context, origin=self.origin, seed=self.seed)
        self.length = self._duration(base)
        self.base = Timebase(context=context, origin=self.origin, length=self.length, seed=self.seed)
        self.frames = max(1, math.ceil(Fraction(self.length) * self.fps / TICKS_PER_SECOND))
        self.frame_ticks = [self._frame_tick(index) for index in range(self.frames)]
        self.seconds: Floats = np.asarray([ticks / TICKS_PER_SECOND for ticks in self.frame_ticks], dtype=np.float64)
        self.tracks: dict[str, np.ndarray[Any, Any]] = {}
        self.layers: list[JobLayer] = []
        self.fires: dict[str, list[_Fire]] = {}
        self._assets: dict[str, dict[str, Any]] = {}

    def _fps(self) -> Fraction:
        raw = self.scene.get("fps")
        if raw is not None:
            return parse_fps(raw)
        if self.context.fps is not None:
            return Fraction(self.context.fps)
        raise NodeFailure("time_unresolved", "the gfx scene sets no fps and its clock has none; set fps= on the scene")

    def _frame_tick(self, index: int) -> int:
        return math.floor(Fraction(index * TICKS_PER_SECOND) / self.fps)

    def _first_frame(self, ticks: int) -> int:
        if ticks <= 0:
            return 0
        return math.ceil(Fraction(ticks) * self.fps / TICKS_PER_SECOND)

    def _duration(self, base: Timebase) -> int:
        raw = self.scene.get("duration")
        if raw is not None:
            return base.local_ticks(_TIME.validate_python(raw))
        if self.context.end is not None:
            return self.context.end - self.origin
        ends = [base.local_ticks(_TIME.validate_python(layer["end"]))
                for layer in _walk(self.scene.get("layers") or ()) if layer.get("end") is not None]
        if ends and max(ends) > 0:
            return max(ends)
        raise NodeFailure("time_unresolved", "the gfx scene has no duration: set duration= on the scene, render it "
                          "on a clock with a duration or window, or give a layer an end")

    def compile(self) -> list[str]:
        self._collect_triggers()
        roots = [str(layer["id"]) for layer in self.scene.get("layers") or ()]
        for layer in self.scene.get("layers") or ():
            self._layer(layer, None)
        return roots

    def _collect_triggers(self) -> None:
        for position, trigger in enumerate(self.scene.get("triggers") or ()):
            layer = str(trigger["layer"])
            for ticks in self._trigger_ticks(trigger["at"], layer, position):
                if ticks >= self.length:
                    continue
                fire = _Fire(ticks, self._first_frame(ticks), position, trigger["action"])
                self.fires.setdefault(layer, []).append(fire)
        for fires in self.fires.values():
            fires.sort(key=lambda fire: (fire.ticks, fire.position))

    def _trigger_ticks(self, at: Any, layer: str, position: int) -> list[int]:
        if isinstance(at, Mapping) and "name" in at and "source" in at:
            source = cast(Mapping[str, Any], at)["source"]
            uri = source.get("uri") if isinstance(source, Mapping) else None
            name = cast(Mapping[str, Any], at)["name"]
            raise NodeFailure(
                "time_unresolved",
                f"trigger {position} on layer {layer!r} fires on event {name!r} of {uri or 'another render'}, but "
                "that render reports no event times, so the time of the event cannot be resolved; trigger on a time "
                "or a time set instead",
                detail={"trigger": position, "layer": layer, "event": name})
        if isinstance(at, Mapping):
            times = _TIMESET.validate_python(at)
            return [ticks - self.origin for ticks in self.context.resolve_set(times, seed=self.seed)]
        return [self.base.local_ticks(_TIME.validate_python(at))]

    def _layer(self, record: Mapping[str, Any], parent: str | None) -> None:
        layer_id = str(record["id"])
        kind = str(record["kind"])
        children = [cast(Mapping[str, Any], child) for child in record.get("children") or ()]
        base = {key: value for key, value in record.items() if key != "children"}
        fires = self.fires.get(layer_id, [])
        variants = self._variants(base, fires)
        job_variants = [JobVariant(start=start, record=rec, assets=self._asset(kind, rec)) for start, rec in variants]
        self.layers.append(JobLayer(id=layer_id, kind=kind, parent=parent,
                                    children=tuple(str(child["id"]) for child in children),
                                    variants=tuple(job_variants)))
        self._active(layer_id, variants)
        model = KIND_MODELS[kind]
        for prop in sorted((FLOAT_PROPS | COLOR_PROPS) & set(model.model_fields)):
            self._track(layer_id, prop, base, fires)
        for child in children:
            self._layer(child, layer_id)

    def _variants(self, base: dict[str, Any], fires: Sequence[_Fire]) -> list[tuple[int, dict[str, Any]]]:
        variants: list[tuple[int, dict[str, Any]]] = [(0, base)]
        for fire in fires:
            if fire.action.get("kind") != "set":
                continue
            prop = str(fire.action["prop"])
            current = dict(variants[-1][1])
            value = fire.action.get("value")
            if value is None:
                current.pop(prop, None)
            else:
                current[prop] = value
            if variants[-1][0] == fire.frame:
                variants[-1] = (fire.frame, current)
            else:
                variants.append((fire.frame, current))
        return variants

    def _active(self, layer_id: str, variants: Sequence[tuple[int, dict[str, Any]]]) -> None:
        active = np.zeros(self.frames, dtype=np.bool_)
        for index, (start_frame, record) in enumerate(variants):
            stop_frame = variants[index + 1][0] if index + 1 < len(variants) else self.frames
            start = self.base.local_ticks(_TIME.validate_python(record["start"])) if record.get("start") else 0
            end = self.base.local_ticks(_TIME.validate_python(record["end"])) if record.get("end") else self.length
            for frame in range(start_frame, min(stop_frame, self.frames)):
                tick = self.frame_ticks[frame]
                active[frame] = start <= tick < end
        self.tracks[track_key(layer_id, ACTIVE)] = active

    def _track(self, layer_id: str, prop: str, base: Mapping[str, Any], fires: Sequence[_Fire]) -> None:
        color = prop in COLOR_PROPS
        track: np.ndarray[Any, Any] = np.full((self.frames, 4) if color else (self.frames,), np.nan, dtype=np.float64)
        timebase = self.base.child(f"{layer_id}/{prop}")
        self._write(track, 0, base.get(prop), timebase, 0.0, color)
        for fire in fires:
            kind = fire.action.get("kind")
            if fire.action.get("prop") != prop or kind == "pulse":
                continue
            if kind == "set":
                self._write(track, fire.frame, fire.action.get("value"), timebase, 0.0, color)
            elif kind == "animate":
                local = Timebase(context=self.context, origin=self.origin + fire.ticks,
                                 length=self.length - fire.ticks, seed=timebase.seed, path=timebase.path)
                self._write(track, fire.frame, {"keys": fire.action["keys"]}, local, fire.ticks / TICKS_PER_SECOND,
                            color)
        for fire in fires:
            if fire.action.get("kind") == "pulse" and fire.action.get("prop", "scale") == prop and not color:
                self._pulse(track, fire)
        self.tracks[track_key(layer_id, prop)] = track

    def _write(self, track: np.ndarray[Any, Any], frame: int, value: Any, timebase: Timebase, shift: float,
               color: bool) -> None:
        if frame >= self.frames:
            return
        seconds = self.seconds[frame:] - shift
        if value is None or (isinstance(value, Mapping) and cast(Mapping[str, Any], value).get("kind") in GRADIENT_KINDS):
            track[frame:] = np.nan
            return
        if color:
            track[frame:] = colors(_COLOR.validate_python(value), timebase, seconds)
        else:
            track[frame:] = floats(_FLOAT.validate_python(value), timebase, seconds)

    def _pulse(self, track: np.ndarray[Any, Any], fire: _Fire) -> None:
        action = fire.action
        local = Timebase(context=self.context, origin=self.origin + fire.ticks, seed=self.seed)
        length = local.local_ticks(_TIME.validate_python(action["duration"]))
        if length <= 0:
            return
        frames = [frame for frame in range(fire.frame, self.frames) if self.frame_ticks[frame] < fire.ticks + length]
        if not frames:
            return
        phase = np.asarray([(self.frame_ticks[frame] - fire.ticks) / length for frame in frames], dtype=np.float64)
        easing = _EASING.validate_python(action["ease"]) if action.get("ease") is not None else None
        shape = 1.0 - ease(easing, phase)
        amount = float(action["amount"])
        track[frames] = track[frames] * (1.0 + (amount - 1.0) * shape)

    def _asset(self, kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
        if kind == "text":
            weight = int(record.get("weight", 600))
            font = cast(JsonValue, record.get("font", "IBM Plex Sans"))
            return {"font": str(font_path(font, weight, self.media))}
        if kind not in ("image", "video"):
            return {}
        path = self.media(cast(JsonValue, record["media"]))
        key = f"{kind}:{path}"
        known = self._assets.get(key)
        if known is not None:
            return known
        if kind == "image":
            found: dict[str, Any] = {"path": str(path), "svg": _is_svg(path)}
            self._assets[key] = found
            return found
        start = self.base.local_seconds(_TIME.validate_python(record["start"])) if record.get("start") else 0.0
        key = f"{key}:{start!r}:{record.get('src')!r}"
        known = self._assets.get(key)
        if known is not None:
            return known
        else:
            width, height = self.probe(path)
            found = {"path": str(path), "width": width, "height": height, "start": start}
            span = record.get("src")
            if isinstance(span, Mapping):
                source = Timebase(context=self.context)
                found["src_start"] = source.local_seconds(_TIME.validate_python(span["start"]))
                found["src_end"] = source.local_seconds(_TIME.validate_python(span["end"]))
        self._assets[key] = found
        return found


def _walk(layers: Sequence[Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for layer in layers:
        record = cast(Mapping[str, Any], layer)
        found.append(record)
        found.extend(_walk(record.get("children") or ()))
    return found


def _is_svg(path: Path) -> bool:
    if path.suffix.lower() == SVG_SUFFIX:
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(512).lstrip()
    except OSError:
        return False
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def compile_scene(scene: Mapping[str, Any], context: TimeContext, *, cpu: bool, media: MediaPath, probe: Probe,
                  workdir: Path) -> Compiled:
    try:
        compiler = _Compiler(scene, context, media, probe)
        roots = compiler.compile()
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"gfx scene: {error}") from error
    except (AnimateError, ValidationError) as error:
        raise NodeFailure("args_invalid", f"gfx scene: {error}") from error
    job = GfxJob(size=(int(scene["size"][0]), int(scene["size"][1])), fps=str(format_fps(compiler.fps)),
                 frames=compiler.frames, background=scene.get("background"), cpu=cpu,
                 output=str(workdir / "gfx.mov"), tracks=str(workdir / "tracks.npz"),
                 result=str(workdir / "result.json"), roots=tuple(roots), layers=tuple(compiler.layers))
    return Compiled(job=job, tracks=compiler.tracks)


__all__ = ["KIND_MODELS", "Compiled", "compile_scene"]
