from __future__ import annotations

import json
import math
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import JsonValue

BOXES_SCHEMA = "eks/boxes@1"
RECORDING_SCHEMA = "eks/recording-boxes@1"
BOXES_SUFFIX = ".boxes.json"
MAX_NODES = 3000
SOURCES = ("web", "app", "native")
TEXT_FIELDS = ("testId", "role", "name", "label", "placeholder", "text", "kind")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SOLVE_STEPS = 60

Box = tuple[float, float, float, float]


class BoxMapError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoxNode:
    box: Box
    test_id: str | None = None
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    kind: str | None = None
    parent: int | None = None

    @property
    def centre(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {}
        for key, value in (("testId", self.test_id), ("role", self.role), ("name", self.name), ("label", self.label),
                           ("placeholder", self.placeholder), ("text", self.text), ("kind", self.kind)):
            if value is not None:
                data[key] = value
        data["box"] = [_round(part) for part in self.box]
        data["parent"] = self.parent
        return data

    def scaled(self, factor: float, dx: float = 0.0, dy: float = 0.0) -> BoxNode:
        x, y, w, h = self.box
        return replace(self, box=((x - dx) * factor, (y - dy) * factor, w * factor, h * factor))


@dataclass(frozen=True, slots=True)
class BoxMap:
    size: tuple[int, int]
    scale: float
    nodes: tuple[BoxNode, ...]
    source: str = "web"

    def to_json(self) -> dict[str, JsonValue]:
        return {"schema": BOXES_SCHEMA, "size": [self.size[0], self.size[1]], "scale": _round(self.scale),
                "source": self.source, "nodes": [node.to_json() for node in self.nodes]}

    def ancestors(self, index: int) -> list[int]:
        found: list[int] = []
        parent = self.nodes[index].parent
        while parent is not None and parent not in found:
            found.append(parent)
            parent = self.nodes[parent].parent
        return found

    @staticmethod
    def from_json(raw: object) -> BoxMap:
        data = _mapping(raw, "box map")
        if data.get("schema") != BOXES_SCHEMA:
            raise BoxMapError(f"box map schema is {data.get('schema')!r}, expected {BOXES_SCHEMA}")
        size = data.get("size")
        if not isinstance(size, list) or len(cast(list[object], size)) != 2:
            raise BoxMapError("box map size must be [width, height]")
        width, height = (_number(part, "box map size") for part in cast(list[object], size))
        scale = _number(data.get("scale", 1), "box map scale")
        source = data.get("source", "web")
        if source not in SOURCES:
            raise BoxMapError(f"box map source {source!r} is not one of {', '.join(SOURCES)}")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list):
            raise BoxMapError("box map nodes must be a list")
        items = cast(list[object], raw_nodes)
        if len(items) > MAX_NODES:
            raise BoxMapError(f"box map has {len(items)} nodes, more than {MAX_NODES}")
        nodes = tuple(_node(item, index, len(items)) for index, item in enumerate(items))
        return BoxMap(size=(int(width), int(height)), scale=scale, nodes=nodes, source=str(source))


@dataclass(frozen=True, slots=True)
class RecordingStep:
    t: float
    boxes: BoxMap
    action: str | None = None


@dataclass(frozen=True, slots=True)
class RecordingBoxes:
    steps: tuple[RecordingStep, ...]
    video: str | None = None

    def at(self, seconds: float) -> BoxMap | None:
        found: BoxMap | None = None
        for step in self.steps:
            if step.t <= seconds + 1e-9:
                found = step.boxes
            else:
                break
        return found

    def to_json(self) -> dict[str, JsonValue]:
        steps: list[JsonValue] = [{"t": _round(step.t), "action": step.action, "boxes": step.boxes.to_json()}
                                  for step in self.steps]
        return {"schema": RECORDING_SCHEMA, "video": self.video, "steps": steps}

    @staticmethod
    def from_json(raw: object) -> RecordingBoxes:
        data = _mapping(raw, "recording box maps")
        if data.get("schema") != RECORDING_SCHEMA:
            raise BoxMapError(f"recording box maps schema is {data.get('schema')!r}, expected {RECORDING_SCHEMA}")
        if data.get("units") is not None:
            raise BoxMapError("the recording box maps are not normalized to video pixels yet")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise BoxMapError("recording box maps steps must be a list")
        steps: list[RecordingStep] = []
        for item in cast(list[object], raw_steps):
            step = _mapping(item, "recording step")
            action = step.get("action")
            steps.append(RecordingStep(t=_number(step.get("t"), "recording step t"),
                                       boxes=BoxMap.from_json(step.get("boxes")),
                                       action=action if isinstance(action, str) else None))
        steps.sort(key=lambda step: step.t)
        video = data.get("video")
        return RecordingBoxes(steps=tuple(steps), video=video if isinstance(video, str) else None)


class NativeLike(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def ident(self) -> str: ...

    @property
    def value(self) -> str: ...

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def width(self) -> float: ...

    @property
    def height(self) -> float: ...

    @property
    def visible(self) -> bool: ...


def _round(value: float) -> float:
    rounded = round(float(value), 3)
    return 0.0 if rounded == 0 else rounded


def _mapping(raw: object, what: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise BoxMapError(f"{what} must be an object")
    return cast(Mapping[str, object], raw)


def _number(raw: object, what: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        raise BoxMapError(f"{what} must be a finite number")
    return float(raw)


def _text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise BoxMapError(f"box map text fields must be strings, got {type(raw).__name__}")
    cleaned = " ".join(raw.split())
    return cleaned or None


def _node(raw: object, index: int, count: int) -> BoxNode:
    data = _mapping(raw, f"box map node {index}")
    box = data.get("box")
    if not isinstance(box, list) or len(cast(list[object], box)) != 4:
        raise BoxMapError(f"box map node {index} box must be [x, y, w, h]")
    x, y, w, h = (_number(part, f"box map node {index} box") for part in cast(list[object], box))
    parent = data.get("parent")
    if parent is not None and (isinstance(parent, bool) or not isinstance(parent, int) or not 0 <= parent < count
                               or parent == index):
        raise BoxMapError(f"box map node {index} parent {parent!r} is not another node's index")
    return BoxNode(box=(x, y, max(0.0, w), max(0.0, h)), test_id=_text(data.get("testId")), role=_text(data.get("role")),
                   name=_text(data.get("name")), label=_text(data.get("label")),
                   placeholder=_text(data.get("placeholder")), text=_text(data.get("text")),
                   kind=_text(data.get("kind")), parent=parent)


def load(path: Path) -> BoxMap | RecordingBoxes:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BoxMapError(f"{path.name} is not readable JSON: {error}") from error
    schema = _mapping(raw, path.name).get("schema")
    if schema == RECORDING_SCHEMA:
        return RecordingBoxes.from_json(raw)
    return BoxMap.from_json(raw)


def dump(data: Mapping[str, JsonValue]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def write(path: Path, boxes: BoxMap | RecordingBoxes) -> Path:
    path.write_text(dump(boxes.to_json()), encoding="utf-8")
    return path


def sidecar_path(image: Path) -> Path:
    return image.with_name(image.stem + BOXES_SUFFIX)


def image_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        head = handle.read(24)
    if head[:8] == PNG_MAGIC and head[12:16] == b"IHDR":
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)
    from PIL import Image

    with Image.open(path) as image:
        return int(image.width), int(image.height)


def from_bridge(raw: object, size: tuple[int, int]) -> BoxMap:
    data = _mapping(raw, "bridge box map")
    window = _mapping(data.get("window"), "bridge box map window")
    window_width = _number(window.get("width"), "bridge window width")
    if window_width <= 0:
        raise BoxMapError("the bridge window has no width")
    factor = size[0] / window_width
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raise BoxMapError("bridge box map nodes must be a list")
    items = cast(list[object], raw_nodes)[:MAX_NODES]
    nodes = tuple(_node(item, index, len(items)).scaled(factor) for index, item in enumerate(items))
    return BoxMap(size=size, scale=factor, nodes=nodes, source="app")


def from_native(found: Sequence[NativeLike], size: tuple[int, int]) -> BoxMap:
    visible = [node for node in found if node.visible and node.width > 0 and node.height > 0]
    if not visible:
        return BoxMap(size=size, scale=1.0, nodes=(), source="native")
    root = max(visible, key=lambda node: node.width * node.height)
    factor = size[0] / root.width if root.width > 0 else 1.0
    nodes: list[BoxNode] = []
    stack: list[tuple[int, NativeLike]] = []
    for node in visible:
        while stack and not _contains(stack[-1][1], node):
            stack.pop()
        parent = stack[-1][0] if stack else None
        if len(nodes) >= MAX_NODES:
            break
        index = len(nodes)
        nodes.append(BoxNode(box=(node.x * factor, node.y * factor, node.width * factor, node.height * factor),
                             test_id=node.ident or None, kind=node.kind or None, text=_text(node.text),
                             label=_text(node.label), name=_text(node.label or node.text),
                             placeholder=_text(node.value) if node.value != node.text else None, parent=parent))
        stack.append((index, node))
    return BoxMap(size=size, scale=factor, nodes=tuple(nodes), source="native")


def _contains(outer: NativeLike, inner: NativeLike) -> bool:
    return (inner.x >= outer.x and inner.y >= outer.y and inner.x + inner.width <= outer.x + outer.width
            and inner.y + inner.height <= outer.y + outer.height)


def trim_mapper(events: Sequence[float], output_duration: float, input_duration: float | None = None
                ) -> Callable[[float], float]:
    from eks_harness.capture.trim import (
        TrimConfig,
        expected_duration,
        output_offset,
        plan,
    )

    config = TrimConfig.from_env()
    if input_duration is None:
        low, high = output_duration, output_duration * config.max_speedup + config.min_screen_sec + 1.0
        for _ in range(SOLVE_STEPS):
            middle = (low + high) / 2
            if expected_duration(plan(middle, list(events), config)) < output_duration:
                low = middle
            else:
                high = middle
        input_duration = high
    segments = plan(input_duration, list(events), config)

    def mapped(seconds: float) -> float:
        found = output_offset(segments, seconds)
        if found is not None:
            return found
        return 0.0 if seconds < 0 else expected_duration(segments)

    return mapped


def normalize_recording(raw: object, *, video_size: tuple[int, int], video_duration: float | None,
                        trimmed: bool, video: str | None = None) -> RecordingBoxes:
    data = _mapping(raw, "recording box maps")
    if data.get("schema") != RECORDING_SCHEMA:
        raise BoxMapError(f"recording box maps schema is {data.get('schema')!r}, expected {RECORDING_SCHEMA}")
    if data.get("units") is None:
        return RecordingBoxes.from_json(raw)
    crop = data.get("crop")
    dx = dy = 0.0
    region_width: float | None = None
    if isinstance(crop, Mapping):
        area = cast(Mapping[str, object], crop)
        dx, dy = _number(area.get("x"), "crop x"), _number(area.get("y"), "crop y")
        region_width = _number(area.get("width"), "crop width")
    events_raw = data.get("events")
    events = [_number(item, "trim event") for item in cast(list[object], events_raw)] \
        if isinstance(events_raw, list) else []
    mapper: Callable[[float], float] | None = None
    recorded = data.get("duration")
    input_duration = _number(recorded, "recording duration") if recorded is not None else None
    if trimmed and video_duration is not None and video_duration > 0:
        mapper = trim_mapper(events, video_duration, input_duration)
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise BoxMapError("recording box maps steps must be a list")
    steps: list[RecordingStep] = []
    for item in cast(list[object], raw_steps):
        step = _mapping(item, "recording step")
        boxes = _mapping(step.get("boxes"), "recording step boxes")
        if data.get("units") == "css":
            width = region_width if region_width is not None else _number(
                cast(list[object], boxes.get("size") or [0])[0], "recording viewport width")
            factor = video_size[0] / width if width > 0 else 1.0
            source = boxes.get("source", "web")
            parsed = BoxMap.from_json({**boxes, "size": list(video_size), "scale": 1})
            nodes = tuple(node.scaled(factor, dx, dy) for node in parsed.nodes)
            mapped_boxes = BoxMap(size=video_size, scale=factor, nodes=nodes, source=str(source))
        elif data.get("units") == "bridge":
            mapped_boxes = from_bridge(boxes, video_size)
        else:
            raise BoxMapError(f"unknown recording box map units {data.get('units')!r}")
        seconds = _number(step.get("t"), "recording step t")
        action = step.get("action")
        steps.append(RecordingStep(t=_round(mapper(seconds) if mapper is not None else max(0.0, seconds)),
                                   boxes=mapped_boxes, action=action if isinstance(action, str) else None))
    steps.sort(key=lambda step: step.t)
    return RecordingBoxes(steps=tuple(steps), video=video)


def as_boxmap(raw: BoxMap | Mapping[str, Any]) -> BoxMap:
    return raw if isinstance(raw, BoxMap) else BoxMap.from_json(raw)


__all__ = [
    "BOXES_SCHEMA",
    "BOXES_SUFFIX",
    "MAX_NODES",
    "RECORDING_SCHEMA",
    "Box",
    "BoxMap",
    "BoxMapError",
    "BoxNode",
    "NativeLike",
    "RecordingBoxes",
    "RecordingStep",
    "as_boxmap",
    "dump",
    "from_bridge",
    "from_native",
    "image_size",
    "load",
    "normalize_recording",
    "sidecar_path",
    "trim_mapper",
    "write",
]
