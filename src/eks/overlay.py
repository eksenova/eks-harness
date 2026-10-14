from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, Literal, get_args

from eks.effects import check_times
from eks.media import MediaLike, as_media
from eks.ref import Ref, encode, is_item, node_of
from eks.tracing import Bound, TraceContext, TraceError, active, current
from eks.ui import TargetLike, Text, coerce_target_arg, to_text
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.ops.overlay import (
    Arrow,
    Blur,
    Callout,
    Caption,
    Emphasis,
    Highlight,
    Icon,
    Image,
    OverlayBase,
    Placement,
    Redact,
    Side,
    Spotlight,
    Step,
    Title,
)
from eks_harness.ir.registry import Registry, RegistryError, default_registry
from eks_harness.ir.target import (
    Target,
    TargetError,
    check_target,
    describe,
    ensure_target,
)
from eks_harness.ir.time import Span, Time, ensure_time, t
from eks_harness.ir.types import Artifact

Window = Time | Span

TEXT_FIELDS = ("text", "subtitle", "kicker")
SIDES: tuple[str, ...] = get_args(Side)


def _registry() -> Registry:
    ctx = active()
    return ctx.registry if ctx is not None else default_registry()


def _anchor(value: TargetLike, what: str) -> Target:
    return ensure_target(value, what, _registry().target_kinds())


def _window(at: Window | None, hold: Time | None, what: str) -> Span | None:
    match at:
        case None:
            return None
        case Span():
            if hold is not None:
                raise TypeError(
                    f"{what}: at= is a Span, which already ends the overlay; drop hold= or pass at= as a Time"
                )
            return at
        case Time():
            return Span(start=at, end=at + hold if hold is not None else t.end)
        case _:
            raise TypeError(
                f"{what} at= takes a Time such as t('1s') or a Span such as t.span(t('1s'), t('4s')), "
                f"got {type(at).__name__} {at!r}"
            )


def _build[O: OverlayBase](
    model: type[O], what: str, *, at: Window | None, hold: Time | None, **fields: Any
) -> O:
    checked_hold = ensure_positive_duration(hold, f"{what} hold") if hold is not None else None
    placement = fields.get("placement")
    if isinstance(placement, str):
        if placement not in SIDES:
            raise TypeError(
                f"eks.overlay.{what} placement= takes a side ({', '.join(SIDES)}) or a Placement, got {placement!r}"
            )
        fields["placement"] = Placement.model_validate({"side": placement})
    texts = {
        name: to_text(fields[name], f"eks.overlay.{what} {name}")
        for name in TEXT_FIELDS
        if fields.get(name) is not None
    }
    data = {
        **fields,
        **texts,
        "window": _window(at, checked_hold, what),
        "hold": checked_hold,
    }
    return model.model_validate(encode({name: value for name, value in data.items() if value is not None}))


def title(
    text: Text, *, subtitle: Text | None = None, kicker: Text | None = None, at: Window | None = None,
    hold: Time | None = None, keep: bool = False, key: str | None = None, style: str | None = None,
    emphasis: Emphasis = "none",
) -> Title:
    return _build(Title, "title", at=at, hold=hold, text=text, subtitle=subtitle, kicker=kicker, keep=keep, key=key,
                  style=style, emphasis=emphasis)


def caption(
    text: Text, *, position: Literal["top", "bottom"] = "bottom", step: int | None = None,
    at: Window | None = None, hold: Time | None = None, keep: bool = False, key: str | None = None,
    placement: Placement | Side | None = None, style: str | None = None, emphasis: Emphasis = "none",
) -> Caption:
    return _build(Caption, "caption", at=at, hold=hold, text=text, position=position, step=step, keep=keep, key=key,
                  placement=placement, style=style, emphasis=emphasis)


def callout(
    anchor: TargetLike, text: Text, *, at: Window | None = None, hold: Time | None = None, keep: bool = False,
    key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
    emphasis: Emphasis = "none",
) -> Callout:
    return _build(Callout, "callout", at=at, hold=hold, anchor=_anchor(anchor, "callout anchor"), text=text,
                  keep=keep, key=key, placement=placement, style=style, emphasis=emphasis)


def spotlight(
    anchor: TargetLike, text: Text | None = None, *, dim: float | None = None, at: Window | None = None,
    hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
    style: str | None = None, emphasis: Emphasis = "none",
) -> Spotlight:
    return _build(Spotlight, "spotlight", at=at, hold=hold, anchor=_anchor(anchor, "spotlight anchor"), text=text,
                  dim=dim, keep=keep, key=key, placement=placement, style=style, emphasis=emphasis)


def highlight(
    anchor: TargetLike, *, shape: Literal["box", "outline", "rounded"] = "box", at: Window | None = None,
    hold: Time | None = None, keep: bool = False, key: str | None = None, style: str | None = None,
    emphasis: Emphasis = "none",
) -> Highlight:
    return _build(Highlight, "highlight", at=at, hold=hold, anchor=_anchor(anchor, "highlight anchor"), shape=shape,
                  keep=keep, key=key, style=style, emphasis=emphasis)


def arrow(
    anchor: TargetLike, to: TargetLike, text: Text | None = None, *, at: Window | None = None,
    hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
    style: str | None = None, emphasis: Emphasis = "none",
) -> Arrow:
    return _build(Arrow, "arrow", at=at, hold=hold, anchor=_anchor(anchor, "arrow anchor"),
                  to=_anchor(to, "arrow to"), text=text, keep=keep, key=key, placement=placement, style=style,
                  emphasis=emphasis)


def blur(
    anchor: TargetLike, *, radius: int | None = None, at: Window | None = None, hold: Time | None = None,
    keep: bool = False, key: str | None = None,
) -> Blur:
    return _build(Blur, "blur", at=at, hold=hold, anchor=_anchor(anchor, "blur anchor"), radius=radius, keep=keep,
                  key=key)


def redact(
    anchor: TargetLike, *, at: Window | None = None, hold: Time | None = None, keep: bool = False,
    key: str | None = None,
) -> Redact:
    return _build(Redact, "redact", at=at, hold=hold, anchor=_anchor(anchor, "redact anchor"), keep=keep, key=key)


def icon(
    anchor: TargetLike, name: str, *, at: Window | None = None, hold: Time | None = None, keep: bool = False,
    key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
) -> Icon:
    return _build(Icon, "icon", at=at, hold=hold, anchor=_anchor(anchor, "icon anchor"), name=name, keep=keep,
                  key=key, placement=placement, style=style)


def image(
    anchor: TargetLike, media: MediaLike, *, width: int | None = None, at: Window | None = None,
    hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
) -> Image:
    return _build(Image, "image", at=at, hold=hold, anchor=_anchor(anchor, "image anchor"),
                  image=as_media(media, kind="image"), width=width, keep=keep, key=key, placement=placement)


def step(
    anchor: TargetLike, index: int | None = None, *, at: Window | None = None, hold: Time | None = None,
    keep: bool = False, key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
) -> Step:
    return _build(Step, "step", at=at, hold=hold, anchor=_anchor(anchor, "step anchor"), index=index, keep=keep,
                  key=key, placement=placement, style=style)


def custom(kind: str, *, at: Window | None = None, hold: Time | None = None, **fields: Any) -> OverlayBase:
    if "kind" in fields:
        raise TypeError(f"eks.overlay.custom({kind!r}) sets kind itself; drop kind= from the fields")
    registry = _registry()
    spec = registry.catalog("overlay", kind)
    model_fields = spec.model.model_fields
    kinds = registry.target_kinds()
    what = f"eks.overlay.custom({kind!r})"
    values: dict[str, Any] = {}
    for name, value in fields.items():
        info = model_fields.get(name)
        if info is not None:
            value = coerce_target_arg(value, info.annotation, f"{what} {name}", kinds)
            check_times(info.annotation, value, f"overlay {kind!r} field {name!r}")
        values[name] = value
    model = spec.model
    if not issubclass(model, OverlayBase):
        raise RegistryError(f"overlay kind {kind!r} is registered with {model.__name__}, not an OverlayBase")
    return _build(model, f"custom({kind!r})", at=at, hold=hold, kind=kind, **values)


def _overlay_targets(overlay: OverlayBase) -> Iterator[tuple[str, Target]]:
    for name in type(overlay).model_fields:
        value = getattr(overlay, name)
        if isinstance(value, Target):
            yield name, value
        elif isinstance(value, tuple | list):
            for index, item in enumerate(value):
                if isinstance(item, Target):
                    yield f"{name}[{index}]", item


def _require_overlay(value: object, what: str) -> OverlayBase:
    if not isinstance(value, OverlayBase):
        raise TypeError(
            f"{what} takes overlay models such as eks.overlay.highlight('#total'), got {type(value).__name__} {value!r}"
        )
    return value


def _require_renderer(ctx: TraceContext, overlay: OverlayBase, renderer: str, what: str) -> None:
    spec = ctx.registry.catalog("overlay", overlay.kind)
    renderers = spec.renderers
    if renderer not in renderers:
        available = ", ".join(sorted(renderers)) or "none"
        raise TraceError(
            f"{what}: overlay kind {overlay.kind!r} has no {renderer} renderer (it renders on {available}); "
            "pick another overlay kind"
        )
    ctx.require(spec.provenance)


_IMAGE_PRODUCERS = frozenset({"capture.shot", "check.checkpoint", "overlay.annotate"})


def _require_image(ctx: TraceContext, artifact: Ref[Artifact]) -> None:
    if is_item(artifact):
        return
    producer = ctx.node(node_of(artifact)).op
    if producer not in _IMAGE_PRODUCERS:
        raise TypeError(
            f"eks.annotate draws on screenshots, but this Ref comes from {producer!r}; annotate the Ref returned "
            "by s.shot(...), s.checkpoint(...) or an earlier eks.annotate(...)"
        )


def annotate(artifact: Ref[Artifact], overlays: Sequence[OverlayBase], *, style: str | None = None) -> Ref[Artifact]:
    ctx = current()
    if not isinstance(artifact, Ref):
        raise TypeError(
            f"eks.annotate takes the screenshot Ref returned by s.shot(...) or s.checkpoint(...), "
            f"got {type(artifact).__name__} {artifact!r}"
        )
    _require_image(ctx, artifact)
    if isinstance(overlays, str | OverlayBase) or not isinstance(overlays, Sequence):
        raise TypeError(f"eks.annotate takes a list of overlays, got {type(overlays).__name__}; wrap it in [...]")
    items = tuple(_require_overlay(item, "eks.annotate") for item in overlays)
    kinds = ctx.registry.target_kinds()
    for item in items:
        _require_renderer(ctx, item, "screenshot", "eks.annotate")
        for field, target in _overlay_targets(item):
            problems = check_target(target, "screenshot", kinds)
            if problems:
                raise TargetError(
                    f"eks.annotate: {item.kind} {field} {describe(target, kinds)} cannot be placed on a screenshot: "
                    + "; ".join(problems)
                )
    args: dict[str, Any] = {"artifact": artifact, "overlays": items}
    if style is not None:
        args["style"] = style
    return ctx.record("overlay.annotate", args)


class LiveOverlay(Bound):
    __slots__ = ()

    def show(self, overlay: OverlayBase, *, at: Window | None = None) -> Ref[None]:
        model = _require_overlay(overlay, "s.overlay.show")
        _require_renderer(self.session.ctx, model, "live", "s.overlay.show")
        match at:
            case None:
                pin = None
            case Span():
                if model.hold is not None:
                    raise TypeError(
                        "s.overlay.show: at= is a Span, which already ends the overlay; drop hold= from the overlay "
                        "or pass at= as a Time"
                    )
                model = model.model_copy(update={"window": at})
                pin = at.start
            case Time():
                pin = at
            case _:
                raise TypeError(
                    f"s.overlay.show at= takes a Time such as t('bar:2') or a Span, got {type(at).__name__} {at!r}"
                )
        return self.session.record_op("overlay.show", {"overlay": model}, at=pin)

    def title(
        self, text: Text, *, subtitle: Text | None = None, kicker: Text | None = None, at: Window | None = None,
        hold: Time | None = None, keep: bool = False, key: str | None = None, style: str | None = None,
        emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(title(text, subtitle=subtitle, kicker=kicker, hold=hold, keep=keep, key=key, style=style,
                               emphasis=emphasis), at=at)

    def caption(
        self, text: Text, *, position: Literal["top", "bottom"] = "bottom", step: int | None = None,
        at: Window | None = None, hold: Time | None = None, keep: bool = False, key: str | None = None,
        placement: Placement | Side | None = None, style: str | None = None, emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(caption(text, position=position, step=step, hold=hold, keep=keep, key=key,
                                 placement=placement, style=style, emphasis=emphasis), at=at)

    def callout(
        self, anchor: TargetLike, text: Text, *, at: Window | None = None, hold: Time | None = None,
        keep: bool = False, key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
        emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(callout(anchor, text, hold=hold, keep=keep, key=key, placement=placement, style=style,
                                 emphasis=emphasis), at=at)

    def spotlight(
        self, anchor: TargetLike, text: Text | None = None, *, dim: float | None = None, at: Window | None = None,
        hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
        style: str | None = None, emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(spotlight(anchor, text, dim=dim, hold=hold, keep=keep, key=key, placement=placement,
                                   style=style, emphasis=emphasis), at=at)

    def highlight(
        self, anchor: TargetLike, *, shape: Literal["box", "outline", "rounded"] = "box", at: Window | None = None,
        hold: Time | None = None, keep: bool = False, key: str | None = None, style: str | None = None,
        emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(highlight(anchor, shape=shape, hold=hold, keep=keep, key=key, style=style,
                                   emphasis=emphasis), at=at)

    def arrow(
        self, anchor: TargetLike, to: TargetLike, text: Text | None = None, *, at: Window | None = None,
        hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
        style: str | None = None, emphasis: Emphasis = "none",
    ) -> Ref[None]:
        return self.show(arrow(anchor, to, text, hold=hold, keep=keep, key=key, placement=placement, style=style,
                               emphasis=emphasis), at=at)

    def blur(
        self, anchor: TargetLike, *, radius: int | None = None, at: Window | None = None, hold: Time | None = None,
        keep: bool = False, key: str | None = None,
    ) -> Ref[None]:
        return self.show(blur(anchor, radius=radius, hold=hold, keep=keep, key=key), at=at)

    def redact(
        self, anchor: TargetLike, *, at: Window | None = None, hold: Time | None = None, keep: bool = False,
        key: str | None = None,
    ) -> Ref[None]:
        return self.show(redact(anchor, hold=hold, keep=keep, key=key), at=at)

    def icon(
        self, anchor: TargetLike, name: str, *, at: Window | None = None, hold: Time | None = None,
        keep: bool = False, key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
    ) -> Ref[None]:
        return self.show(icon(anchor, name, hold=hold, keep=keep, key=key, placement=placement, style=style), at=at)

    def image(
        self, anchor: TargetLike, media: MediaLike, *, width: int | None = None, at: Window | None = None,
        hold: Time | None = None, keep: bool = False, key: str | None = None, placement: Placement | Side | None = None,
    ) -> Ref[None]:
        return self.show(image(anchor, media, width=width, hold=hold, keep=keep, key=key, placement=placement), at=at)

    def step(
        self, anchor: TargetLike, index: int | None = None, *, at: Window | None = None, hold: Time | None = None,
        keep: bool = False, key: str | None = None, placement: Placement | Side | None = None, style: str | None = None,
    ) -> Ref[None]:
        return self.show(step(anchor, index, hold=hold, keep=keep, key=key, placement=placement, style=style), at=at)

    def remove(self, key: str, *, at: Time | None = None) -> Ref[None]:
        if not isinstance(key, str) or not key:
            raise TypeError(f"s.overlay.remove takes the key given to a kept overlay, got {key!r}")
        pin = ensure_time(at, "s.overlay.remove at") if at is not None else None
        return self.session.record_op("overlay.remove", {"key": key}, at=pin)

    def clear(self, kind: str | None = None, *, at: Time | None = None) -> Ref[None]:
        if kind is not None:
            ctx = self.session.ctx
            ctx.require(ctx.registry.catalog("overlay", kind).provenance)
        pin = ensure_time(at, "s.overlay.clear at") if at is not None else None
        return self.session.record_op("overlay.clear", {"kind": kind} if kind is not None else {}, at=pin)


__all__ = [
    "LiveOverlay",
    "Window",
    "annotate",
    "arrow",
    "blur",
    "callout",
    "caption",
    "custom",
    "highlight",
    "icon",
    "image",
    "redact",
    "spotlight",
    "step",
    "title",
]
