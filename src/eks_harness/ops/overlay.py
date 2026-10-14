from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    builtin_provenance,
)
from eks_harness.ir.ops.overlay import BUILTIN, Annotate, Image, OverlayBase, Step
from eks_harness.ir.registry import Location
from eks_harness.media.overlays.boxmap import (
    Box,
    BoxMap,
    BoxMapError,
    RecordingBoxes,
    image_size,
    load,
)
from eks_harness.media.overlays.draw import (
    DEFAULT_STYLE,
    Drawing,
    ResolvedStyle,
    draw,
    rasterize,
    resolve_style,
    svg_document,
    versions,
)
from eks_harness.media.overlays.targets import overlay_targets, resolve_on_boxmap

OVERLAY_EXECUTOR = "eks.overlay"
OVERLAY_EXECUTOR_VERSION = "1"
ANNOTATE_OP = "overlay.annotate"
RENDERER = "screenshot"
HUB: frozenset[Location] = frozenset({cast(Location, "hub")})

MetaReader = Callable[[str], Mapping[str, object] | None]


def artifact_meta(call: ExecContext, artifact_id: str) -> Mapping[str, object]:
    store: object = call.artifacts.store
    reader: object = getattr(store, "meta", None)
    if callable(reader):
        found = cast(MetaReader, reader)(artifact_id)
        return found or {}
    database: object = getattr(store, "db", None)
    if database is not None:
        from eks_harness.db import Database
        from eks_harness.db.repos import artifacts as artifacts_repo

        if isinstance(database, Database):
            row = artifacts_repo.get(database.conn(), artifact_id)
            return cast(Mapping[str, object], row.meta) if row is not None else {}
    return {}


def _source_id(value: JsonValue) -> str:
    if isinstance(value, Mapping):
        data = cast(Mapping[str, JsonValue], value)
        found = data.get("id") or data.get("artifact")
        if isinstance(found, str) and found:
            return found
        shots = data.get("shots")
        if isinstance(shots, list) and shots and isinstance(shots[0], str):
            return shots[0]
    raise NodeFailure("content_unavailable", f"overlay.annotate needs a screenshot artifact, got {value!r}")


def _boxmap(call: ExecContext, meta: Mapping[str, object], size: tuple[int, int]) -> tuple[BoxMap | None, str | None]:
    boxes_id = meta.get("boxes")
    if not isinstance(boxes_id, str) or not boxes_id:
        return None, None
    path = call.artifacts.file(boxes_id)
    if path is None:
        raise NodeFailure("content_unavailable", f"the box map artifact {boxes_id} has no stored file",
                          detail={"boxes": boxes_id})
    try:
        loaded = load(path)
    except BoxMapError as error:
        raise NodeFailure("content_unavailable", f"the box map artifact {boxes_id} is not a box map: {error}",
                          detail={"boxes": boxes_id}) from error
    if isinstance(loaded, RecordingBoxes):
        raise NodeFailure("content_unavailable", f"the box map artifact {boxes_id} belongs to a recording, not a "
                          "screenshot", detail={"boxes": boxes_id})
    if loaded.size != size:
        raise NodeFailure("content_unavailable",
                          f"the box map {boxes_id} is {loaded.size[0]}x{loaded.size[1]}, the screenshot is "
                          f"{size[0]}x{size[1]}", detail={"boxes": boxes_id})
    return loaded, boxes_id


def _check_renderer(call: ExecContext, overlay: OverlayBase) -> None:
    spec = call.registry.catalog("overlay", overlay.kind)
    renderer = spec.renderers.get(RENDERER)
    if renderer is None:
        raise NodeFailure("args_invalid", f"the {overlay.kind} overlay does not render on screenshots",
                          detail={"kind": overlay.kind})
    if renderer != BUILTIN:
        raise NodeFailure("executor_missing",
                          f"the {overlay.kind} overlay renders on screenshots through {renderer}, which this "
                          "executor does not run", detail={"kind": overlay.kind, "renderer": renderer})


def _blurred(image: object, blurs: list[tuple[Box, int]]) -> object:
    from PIL import Image as PILImage
    from PIL import ImageFilter

    canvas = cast(PILImage.Image, image)
    for (x, y, w, h), radius in blurs:
        left, top = max(0, int(x)), max(0, int(y))
        right, bottom = min(canvas.width, int(x + max(1.0, w))), min(canvas.height, int(y + max(1.0, h)))
        if right <= left or bottom <= top:
            continue
        region = canvas.crop((left, top, right, bottom)).filter(ImageFilter.GaussianBlur(radius=radius))
        canvas.paste(region, (left, top))
    return canvas


def compose(clean: Path, drawings: list[tuple[Drawing, ResolvedStyle]], size: tuple[int, int]) -> bytes:
    from PIL import Image as PILImage

    with PILImage.open(clean) as opened:
        base = opened.convert("RGBA")
    blurs = [blur for drawing, _ in drawings for blur in drawing.blurs]
    base = cast(PILImage.Image, _blurred(base, blurs))
    fragments = [fragment for drawing, _ in drawings for fragment in drawing.fragments]
    fonts = list(dict.fromkeys(path for _, style in drawings for path in style.fonts))
    if fragments:
        overlay_png = rasterize(svg_document(fragments, size), size, fonts)
        with PILImage.open(io.BytesIO(overlay_png)) as rendered:
            base = PILImage.alpha_composite(base, rendered.convert("RGBA"))
    buffer = io.BytesIO()
    base.convert("RGB").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


class AnnotateExecutor:
    def run(self, call: ExecContext) -> ExecResult:
        args = call.args
        if not isinstance(args, Annotate):
            raise NodeFailure("args_invalid", f"{call.op} needs overlay.annotate arguments, got {type(args).__name__}")
        source_id = _source_id(call.data.get("artifact"))
        clean = call.artifacts.file(source_id)
        if clean is None:
            raise NodeFailure("content_unavailable", f"the screenshot artifact {source_id} has no stored file",
                              detail={"artifact": source_id})
        size = image_size(clean)
        boxes, boxes_id = _boxmap(call, artifact_meta(call, source_id), size)
        empty = BoxMap(size=size, scale=1.0, nodes=())
        raw_overlays = call.data.get("overlays")
        dumps = cast(list[JsonValue], raw_overlays) if isinstance(raw_overlays, list) else []
        styles: dict[str, ResolvedStyle] = {}
        drawings: list[tuple[Drawing, ResolvedStyle]] = []
        anchors: list[JsonValue] = []
        step = 0
        for index, overlay in enumerate(args.overlays):
            call.check_cancelled()
            _check_renderer(call, overlay)
            name = overlay.style or args.style or DEFAULT_STYLE
            style = styles.get(name)
            if style is None:
                style = styles[name] = resolve_style(name, registry=call.registry)
            resolved: dict[str, Box] = {}
            for field, target in overlay_targets(overlay).items():
                if boxes is None and target.by not in ("point", "rect"):
                    raise NodeFailure("content_unavailable",
                                      f"the {overlay.kind} {field} {target.by} target needs the screenshot's box map, "
                                      f"and artifact {source_id} has none; anchor by point or rect, or annotate a "
                                      "shot captured with box maps", detail={"artifact": source_id})
                resolved[field] = resolve_on_boxmap(target, boxes or empty)
            image: Path | None = None
            if isinstance(overlay, Image):
                item = dumps[index] if index < len(dumps) else None
                media = cast(Mapping[str, JsonValue], item).get("image") if isinstance(item, Mapping) else None
                image = call.media(media).path
            if isinstance(overlay, Step):
                step = overlay.index if overlay.index is not None else step + 1
            drawing = draw(overlay, key=f"o{index}", anchor=resolved.get("anchor"), to=resolved.get("to"),
                           image=image, style=style, size=size, step=step, renderer="screenshot")
            drawings.append((drawing, style))
            anchors.append({field: list(box) for field, box in resolved.items()})
        png = compose(clean, drawings, size)
        annotation: dict[str, JsonValue] = {
            "source": source_id,
            "overlays": dumps,
            "anchors": anchors,
            "style": args.style or DEFAULT_STYLE,
            "styles": {name: style.model.model_dump(mode="json") for name, style in styles.items()},
            "versions": {**versions(), "executor": OVERLAY_EXECUTOR_VERSION},
        }
        meta: dict[str, JsonValue] = {"annotation": annotation}
        if boxes_id is not None:
            meta["boxes"] = boxes_id
        stem = Path(clean.name).stem
        artifact = call.artifacts.write(png, filename=f"{stem}-annotated.png", kind="screenshot",
                                        caption=f"annotated {stem}", meta=meta)
        return ExecResult(value=artifact.model_dump(mode="json", by_alias=True),
                          detail={"overlays": len(drawings), "boxes": boxes_id})

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(OVERLAY_EXECUTOR, OVERLAY_EXECUTOR_VERSION, ops=(ANNOTATE_OP,), run=self.run,
                            locations=HUB, fonts=True, runtime_version=_runtime_version, provenance=builtin_provenance(),
                            description="Renders overlays onto a screenshot artifact using its box map")


def _runtime_version() -> str:
    return ",".join(f"{name}={value}" for name, value in sorted(versions().items()))


__all__ = ["ANNOTATE_OP", "OVERLAY_EXECUTOR", "OVERLAY_EXECUTOR_VERSION", "AnnotateExecutor", "artifact_meta",
           "compose"]
