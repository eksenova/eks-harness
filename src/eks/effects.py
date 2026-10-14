from __future__ import annotations

import collections.abc
from collections.abc import Mapping, Sequence
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    TypedDict,
    Union,
    Unpack,
    cast,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ValidationError

from eks.media import FontLike, MediaLike, as_font, as_media
from eks.ref import JsonLike, Ref, RefUsageError, encode
from eks.tracing import active
from eks.ui import Text
from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.curves import (
    LFO,
    BeatPulse,
    Bezier,
    Bounce,
    BoxEnv,
    CurveBase,
    EasingEnv,
    Envelope,
    ExpDecay,
    ExpDecayEnv,
    Lambda,
    LinearEnv,
    Noise,
    RandomChoice,
    Sine,
    Spring,
    Step,
)
from eks_harness.ir.catalog.durations import (
    ensure_positive_duration,
    optional_positive_duration,
)
from eks_harness.ir.catalog.easing import LEGACY_NAMES, EasingBase
from eks_harness.ir.catalog.effects import (
    LUT,
    VHS,
    AutoReframe,
    BiRefNetRemove,
    Blur,
    Brightness,
    Captions,
    ChromaKey,
    ColorGrade,
    Contrast,
    Crop,
    Datamosh,
    EffectBase,
    Fade,
    FilmGrain,
    Flash,
    Freeze,
    Glitch,
    Invert,
    LowerThird,
    MatAnyoneRemove,
    Mirror,
    Pan,
    Posterize,
    PunchIn,
    RGBSplit,
    SAM3Track,
    Saturation,
    Shake,
    Sharpen,
    SpeedRamp,
    StickerOverlay,
    TextOverlay,
    Vignette,
    Watermark,
    Zoom,
)
from eks_harness.ir.catalog.keys import Keyframe, Keys
from eks_harness.ir.catalog.markers import MarkerBase
from eks_harness.ir.catalog.transitions import (
    BeatFlash,
    BeatGlitch,
    BlurThrough,
    Crossfade,
    Cut,
    DipToBlack,
    DipToWhite,
    Direction,
    Dissolve,
    FadeGrays,
    FilmBurn,
    GlitchTransition,
    Iris,
    LightLeak,
    LumaWipe,
    Pixelize,
    Push,
    Radial,
    RGBShiftWipe,
    Slide,
    TransitionBase,
    WhipPan,
    Wipe,
    ZoomTransition,
)
from eks_harness.ir.ops.overlay import OverlayBase
from eks_harness.ir.registry import (
    Category,
    Registry,
    RegistryError,
    default_registry,
    unknown_name,
)
from eks_harness.ir.time import (
    Time,
    TimeSet,
    ensure_time,
    optional_time,
    t,
)
from eks_harness.ir.types import DEFAULT_FONT

EaseLike = str | EasingBase | None
AnimFloat = float | Keys[float] | Driven | CurveBase
AnimInt = int | Keys[int]


class CaptionOptions(TypedDict, total=False):
    font: FontLike
    size: int
    color: str
    stroke: str | None
    stroke_width: int
    position: Literal["top", "center", "bottom"]
    margin: int
    style: Literal["tiktok", "subtitle", "kinetic"]
    word_animation: Literal["pop", "fade", "none"]
    max_words_per_card: int

CATALOG_BASES: tuple[tuple[type[BaseModel], Category], ...] = (
    (EffectBase, "effect"),
    (TransitionBase, "transition"),
    (EasingBase, "easing"),
    (CurveBase, "curve"),
    (MarkerBase, "marker"),
    (OverlayBase, "overlay"),
)
ENVELOPES = (ExpDecayEnv, LinearEnv, BoxEnv, EasingEnv)
SEQUENCE_ORIGINS: tuple[object, ...] = (
    tuple, list, set, frozenset, collections.abc.Sequence, collections.abc.Set, collections.abc.MutableSequence,
)
MAPPING_ORIGINS: tuple[object, ...] = (dict, collections.abc.Mapping, collections.abc.MutableMapping)


def catalog_registry() -> Registry:
    ctx = active()
    return ctx.registry if ctx is not None else default_registry()


def _category_of(model: BaseModel) -> Category | None:
    return next((category for base, category in CATALOG_BASES if isinstance(model, base)), None)


def require_catalogs(value: object) -> None:
    ctx = active()
    registry = ctx.registry if ctx is not None else default_registry()
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, BaseModel):
            category = _category_of(item)
            if category is not None:
                spec = registry.catalog(category, str(getattr(item, "kind", "")))
                if not isinstance(item, spec.model):
                    raise RegistryError(
                        f"{type(item).__name__} says it is the {category} kind {spec.kind!r}, which is registered "
                        f"by {spec.model.__module__}.{spec.model.__qualname__}; register the model with "
                        f"@eks.{category} or use a different kind"
                    )
                if ctx is not None:
                    ctx.require(spec.provenance)
            pending.extend(getattr(item, name) for name in type(item).model_fields)
        elif isinstance(item, list | tuple):
            pending.extend(cast("Sequence[object]", item))
        elif isinstance(item, Mapping):
            pending.extend(cast("Mapping[object, object]", item).values())


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{what} expects a number, got {type(value).__name__} {value!r}")
    return value


def _trigger(value: object, what: str) -> Time | TimeSet:
    if isinstance(value, TimeSet):
        return value
    return ensure_time(value, what)


def _text(value: object, what: str) -> Any:
    if isinstance(value, str | Ref):
        return encode(value)
    raise TypeError(f"{what} expects text (a str, an f-string with refs, or a Ref), got {type(value).__name__} {value!r}")


def _easing(value: EaseLike, what: str) -> EasingBase | None:
    if value is None:
        return None
    if isinstance(value, EasingBase):
        require_catalogs(value)
        return value
    if not isinstance(value, str):
        raise TypeError(
            f"{what} expects an easing name such as 'out_cubic' or an easing model, got {type(value).__name__} {value!r}"
        )
    registry = catalog_registry()
    name = LEGACY_NAMES.get(value, value)
    kinds = [spec.kind for spec in registry.catalogs("easing")]
    if name not in kinds:
        raise unknown_name("easing", value, [*kinds, *LEGACY_NAMES])
    spec = registry.catalog("easing", name)
    try:
        model = spec.model.model_validate({"kind": name})
    except ValidationError as error:
        required = [field for field, info in spec.model.model_fields.items() if info.is_required()]
        raise RegistryError(
            f"easing {value!r} takes parameters ({', '.join(required)}); build it with "
            f"{spec.model.__name__}({', '.join(f'{field}=...' for field in required)})"
        ) from error
    if not isinstance(model, EasingBase):
        raise RegistryError(f"easing kind {name!r} is registered with {spec.model.__name__}, which is not an easing model")
    require_catalogs(model)
    return model


def ease(value: EaseLike) -> EasingBase | None:
    return _easing(value, "eks.ease()")


def keys(points: Mapping[Time, JsonLike], *, ease: EaseLike = None) -> Keys[Any]:
    if not isinstance(points, Mapping):
        raise TypeError(
            f"eks.keys() takes a mapping of times to values such as {{t('0s'): 1.0, t('1s'): 2.0}}, "
            f"got {type(points).__name__} {points!r}"
        )
    if not points:
        raise ValueError("eks.keys() needs at least one keyframe, for example {t('0s'): 1.0}")
    easing = _easing(ease, "eks.keys(ease=)")
    frames: list[Keyframe[Any]] = []
    for index, (time, value) in enumerate(points.items()):
        if isinstance(value, Ref):
            raise RefUsageError(
                f"keyframe value {value!r} is a Ref; keyframes hold values known when the plan is built"
            )
        frames.append(Keyframe[Any](t=ensure_time(time, "keyframe time"), v=value, ease=easing if index else None))
    result = Keys[Any](keys=tuple(frames))
    require_catalogs(result)
    return result


def driven(curve: CurveBase, *, scale: float = 1.0, offset: float = 0.0) -> Driven:
    if not isinstance(curve, CurveBase):
        raise TypeError(f"eks.driven() expects a curve such as eks.curves.sine(2.0), got {type(curve).__name__} {curve!r}")
    require_catalogs(curve)
    return Driven(curve=curve, scale=_number(scale, "driven scale"), offset=_number(offset, "driven offset"))


def animated(value: AnimFloat) -> float | Keys[float] | Driven:
    if isinstance(value, CurveBase):
        return driven(value)
    if isinstance(value, Keys | Driven):
        require_catalogs(value)
        return cast("Keys[float] | Driven", value)
    return _number(value, "an animated value")


def _custom[B: BaseModel](category: Category, kind: str, params: Mapping[str, Any], base: type[B]) -> B:
    spec = catalog_registry().catalog(category, kind)
    fields = spec.model.model_fields
    values: dict[str, Any] = {"kind": kind}
    for name, value in params.items():
        info = fields.get(name)
        if info is not None:
            check_times(info.annotation, value, f"{category} {kind!r} field {name!r}")
        values[name] = _plain(value)
    model = spec.model.model_validate(values)
    if not isinstance(model, base):
        raise RegistryError(f"{category} kind {kind!r} is registered with {spec.model.__name__}, not a {base.__name__}")
    require_catalogs(model)
    return model


def _unwrap(annotation: object) -> object:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def check_times(annotation: object, value: object, what: str) -> None:
    annotation = _unwrap(annotation)
    if value is None or isinstance(value, Time | TimeSet):
        return
    if annotation is Time:
        ensure_time(value, what)
        return
    origin = get_origin(annotation)
    arguments = tuple(_unwrap(argument) for argument in get_args(annotation))
    if origin is Union or origin is UnionType:
        _check_union(arguments, value, what)
    elif isinstance(value, list | tuple) and origin in SEQUENCE_ORIGINS:
        items = cast("Sequence[object]", value)
        if origin is tuple and not (len(arguments) == 2 and arguments[1] is Ellipsis):
            for index, (argument, item) in enumerate(zip(arguments, items, strict=False)):
                check_times(argument, item, f"{what}[{index}]")
        elif arguments:
            for index, item in enumerate(items):
                check_times(arguments[0], item, f"{what}[{index}]")
    elif isinstance(value, Mapping) and origin in MAPPING_ORIGINS and len(arguments) == 2:
        for key, item in cast("Mapping[object, object]", value).items():
            check_times(arguments[1], item, f"{what}[{key!r}]")


def _accepts(member: object, value: object) -> bool:
    if member is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(member, type):
        return isinstance(value, member)
    if get_origin(member) is Literal:
        return value in get_args(member)
    return True


def _check_union(members: tuple[object, ...], value: object, what: str) -> None:
    containers = isinstance(value, list | tuple | Mapping)
    others = [member for member in members if member not in (Time, TimeSet, NoneType)]
    if Time in members and not containers and not any(_accepts(member, value) for member in others):
        ensure_time(value, what)
        return
    for member in members:
        origin = get_origin(member)
        if (isinstance(value, list | tuple) and origin in SEQUENCE_ORIGINS) or (
            isinstance(value, Mapping) and origin in MAPPING_ORIGINS
        ):
            check_times(member, value, what)
            return


def _plain(value: object) -> object:
    if isinstance(value, Ref | str):
        return encode(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in cast("Mapping[str, object]", value).items()}
    return value


class Fx:
    def brightness(self, amount: AnimFloat) -> Brightness:
        return Brightness(amount=animated(amount))

    def contrast(self, amount: AnimFloat) -> Contrast:
        return Contrast(amount=animated(amount))

    def saturation(self, amount: AnimFloat) -> Saturation:
        return Saturation(amount=animated(amount))

    def crop(self, x: AnimInt, y: AnimInt, w: AnimInt, h: AnimInt) -> Crop:
        return Crop(x=x, y=y, w=w, h=h)

    def mirror(self, axis: Literal["horizontal", "vertical", "both"] = "horizontal") -> Mirror:
        return Mirror(axis=axis)

    def zoom(
        self, start: float, end: float | None = None, *, ease: EaseLike = None, cx: AnimFloat = 0.5,
        cy: AnimFloat = 0.5,
    ) -> Zoom:
        first = _number(start, "fx.zoom start")
        if end is None:
            if ease is not None:
                raise TypeError("fx.zoom(start) is a constant zoom; pass an end scale to animate it with ease=")
            return Zoom(scale=first, cx=animated(cx), cy=animated(cy))
        scale = keys({t.start: first, t.end: _number(end, "fx.zoom end")}, ease=ease)
        return Zoom(scale=cast("Keys[float]", scale), cx=animated(cx), cy=animated(cy))

    def pan(self, dx: AnimInt, dy: AnimInt) -> Pan:
        return Pan(dx=dx, dy=dy)

    def speed(self, factor: AnimFloat) -> SpeedRamp:
        return SpeedRamp(factor=animated(factor))

    def freeze(self, at: Time, hold: Time) -> Freeze:
        return Freeze(at=ensure_time(at, "fx.freeze at"), hold=ensure_positive_duration(hold, "fx.freeze hold"))

    def chroma_key(self, color: str = "#00ff00", *, similarity: float = 0.1, blend: float = 0.0) -> ChromaKey:
        return ChromaKey(color=color, similarity=similarity, blend=blend)

    def color_grade(
        self, *, lut: MediaLike | None = None, exposure: AnimFloat = 0.0, temperature: AnimFloat = 0.0,
        tint: AnimFloat = 0.0,
    ) -> ColorGrade:
        return ColorGrade.model_validate({
            "lut": None if lut is None else encode(as_media(lut, kind="file")), "exposure": animated(exposure),
            "temperature": animated(temperature), "tint": animated(tint)})

    def fade(self, direction: Literal["in", "out"], duration: Time) -> Fade:
        return Fade(direction=direction, duration=ensure_positive_duration(duration, "fx.fade duration"))

    def blur(self, radius: AnimFloat) -> Blur:
        return Blur(radius=animated(radius))

    def sharpen(self, amount: AnimFloat) -> Sharpen:
        return Sharpen(amount=animated(amount))

    def vignette(self, *, angle: float = 0.628, x0: float = 0.5, y0: float = 0.5) -> Vignette:
        return Vignette(angle=angle, x0=x0, y0=y0)

    def film_grain(self, intensity: AnimFloat, *, seed: int | None = None) -> FilmGrain:
        return FilmGrain(intensity=animated(intensity), seed=seed)

    def lut(self, path: MediaLike) -> LUT:
        return LUT.model_validate({"path": encode(as_media(path, kind="file"))})

    def invert(self) -> Invert:
        return Invert()

    def posterize(self, levels: AnimInt) -> Posterize:
        return Posterize(levels=levels)

    def text(
        self, text: Text, *, font: FontLike = DEFAULT_FONT, size: int = 48, color: str = "#ffffffff", x: AnimInt = 100,
        y: AnimInt = 100, start: Time | None = None, duration: Time | None = None,
    ) -> TextOverlay:
        fields: dict[str, Any] = {
            "text": _text(text, "fx.text text"), "font": as_font(font, "fx.text font"), "size": size, "color": color, "x": x, "y": y,
            "start": optional_time(start, "fx.text start"), "duration": optional_positive_duration(duration, "fx.text duration"),
        }
        return TextOverlay.model_validate({key: value for key, value in fields.items() if value is not None})

    def lower_third(
        self, title: Text, subtitle: Text = "", *, font: FontLike = DEFAULT_FONT,
        position: Literal["left", "center", "right"] = "left", margin: int = 120,
    ) -> LowerThird:
        return LowerThird.model_validate({
            "title": _text(title, "fx.lower_third title"), "subtitle": _text(subtitle, "fx.lower_third subtitle"),
            "font": as_font(font, "fx.lower_third font"), "position": position, "margin": margin})

    def watermark(
        self, path: MediaLike, *, x: AnimInt = 20, y: AnimInt = 20, opacity: AnimFloat = 1.0,
        scale: AnimFloat = 1.0,
    ) -> Watermark:
        return Watermark.model_validate({"path": encode(as_media(path, kind="image")), "x": x, "y": y,
                                         "opacity": animated(opacity), "scale": animated(scale)})

    def sticker(
        self, path: MediaLike, *, x: AnimInt = 0, y: AnimInt = 0, scale: AnimFloat = 1.0,
        rotation: AnimFloat = 0.0,
    ) -> StickerOverlay:
        return StickerOverlay.model_validate({"path": encode(as_media(path, kind="image")), "x": x, "y": y,
                                              "scale": animated(scale), "rotation": animated(rotation)})

    def flash(
        self, at: Time | TimeSet, duration: Time, *, color: str = "#ffffff", curve: CurveBase | None = None
    ) -> Flash:
        if curve is not None:
            if not isinstance(curve, CurveBase):
                raise TypeError(f"fx.flash curve expects a curve such as eks.curves.exp_decay(...), got {curve!r}")
            require_catalogs(curve)
        return Flash(
            at=_trigger(at, "fx.flash at"), duration=ensure_positive_duration(duration, "fx.flash duration"), color=color,
            curve=curve)

    def rgb_split(self, offset_x: AnimInt, offset_y: AnimInt = 0) -> RGBSplit:
        return RGBSplit(offset_x=offset_x, offset_y=offset_y)

    def glitch(self, intensity: AnimFloat, *, block_size: int = 16, seed: int | None = None) -> Glitch:
        return Glitch(intensity=animated(intensity), block_size=block_size, seed=seed)

    def shake(
        self, intensity: AnimFloat, *, freq_hz: float = 6.0, decay: float = 0.0, seed: int | None = None
    ) -> Shake:
        return Shake(intensity=animated(intensity), freq_hz=freq_hz, decay=decay, seed=seed)

    def vhs(self, *, scanlines: bool = True, chroma_blur: float = 1.5, noise: float = 0.05) -> VHS:
        return VHS(scanlines=scanlines, chroma_blur=chroma_blur, noise=noise)

    def datamosh(self, intensity: AnimFloat = 0.5) -> Datamosh:
        return Datamosh(intensity=animated(intensity))

    def captions(self, source: str, **options: Unpack[CaptionOptions]) -> Captions:
        if "kind" in options:
            raise TypeError("fx.captions() sets kind itself; leave kind= out")
        fields: dict[str, Any] = {"source": source, **options}
        if "font" in options:
            fields["font"] = as_font(options["font"], "fx.captions font")
        return Captions.model_validate(fields)

    def matanyone_remove(self, *, background: str = "#00000000") -> MatAnyoneRemove:
        return MatAnyoneRemove(background=background)

    def birefnet_remove(self, *, background: str = "#00000000") -> BiRefNetRemove:
        return BiRefNetRemove(background=background)

    def sam3_track(
        self, prompt: str, *, output: Literal["mask", "matte", "highlight"] = "mask",
        confidence_threshold: float = 0.5,
    ) -> SAM3Track:
        return SAM3Track(prompt=prompt, output=output, confidence_threshold=confidence_threshold)

    def auto_reframe(
        self, *, target_aspect: tuple[int, int] = (9, 16), tracker: Literal["face", "salience"] = "face",
        smoothing: float = 0.85,
    ) -> AutoReframe:
        return AutoReframe(target_aspect=target_aspect, tracker=tracker, smoothing=smoothing)

    def punch_in(
        self, *, zoom: AnimFloat = 1.2, trigger: Time | TimeSet | None = None, track_speaker: bool = True
    ) -> PunchIn:
        return PunchIn(
            zoom=animated(zoom), trigger=None if trigger is None else _trigger(trigger, "fx.punch_in trigger"),
            track_speaker=track_speaker)

    def custom(self, kind: str, **params: Any) -> EffectBase:
        return _custom("effect", kind, params, EffectBase)


class Tx:
    def cut(self) -> Cut:
        return Cut()

    def crossfade(self, duration: Time) -> Crossfade:
        return Crossfade(duration=ensure_positive_duration(duration, "tx.crossfade duration"))

    def dip_to_black(self, duration: Time) -> DipToBlack:
        return DipToBlack(duration=ensure_positive_duration(duration, "tx.dip_to_black duration"))

    def dip_to_white(self, duration: Time) -> DipToWhite:
        return DipToWhite(duration=ensure_positive_duration(duration, "tx.dip_to_white duration"))

    def wipe(self, duration: Time, *, direction: Direction = "right") -> Wipe:
        return Wipe(duration=ensure_positive_duration(duration, "tx.wipe duration"), direction=direction)

    def slide(self, duration: Time, *, direction: Direction = "right") -> Slide:
        return Slide(duration=ensure_positive_duration(duration, "tx.slide duration"), direction=direction)

    def push(self, duration: Time, *, direction: Direction = "left") -> Push:
        return Push(duration=ensure_positive_duration(duration, "tx.push duration"), direction=direction)

    def glitch(self, duration: Time, *, intensity: float = 0.6) -> GlitchTransition:
        return GlitchTransition(duration=ensure_positive_duration(duration, "tx.glitch duration"), intensity=intensity)

    def zoom(self, duration: Time) -> ZoomTransition:
        return ZoomTransition(duration=ensure_positive_duration(duration, "tx.zoom duration"))

    def dissolve(self, duration: Time) -> Dissolve:
        return Dissolve(duration=ensure_positive_duration(duration, "tx.dissolve duration"))

    def blur_through(self, duration: Time) -> BlurThrough:
        return BlurThrough(duration=ensure_positive_duration(duration, "tx.blur_through duration"))

    def iris(self, duration: Time, *, mode: Literal["open", "close"] = "open") -> Iris:
        return Iris(duration=ensure_positive_duration(duration, "tx.iris duration"), mode=mode)

    def radial(self, duration: Time) -> Radial:
        return Radial(duration=ensure_positive_duration(duration, "tx.radial duration"))

    def pixelize(self, duration: Time) -> Pixelize:
        return Pixelize(duration=ensure_positive_duration(duration, "tx.pixelize duration"))

    def fade_grays(self, duration: Time) -> FadeGrays:
        return FadeGrays(duration=ensure_positive_duration(duration, "tx.fade_grays duration"))

    def luma_wipe(self, duration: Time, *, direction: Direction = "right") -> LumaWipe:
        return LumaWipe(duration=ensure_positive_duration(duration, "tx.luma_wipe duration"), direction=direction)

    def rgb_shift_wipe(self, duration: Time, *, direction: Direction = "right", intensity: float = 0.6) -> RGBShiftWipe:
        return RGBShiftWipe(
            duration=ensure_positive_duration(duration, "tx.rgb_shift_wipe duration"), direction=direction, intensity=intensity)

    def whip_pan(self, duration: Time, *, direction: Direction = "right", intensity: float = 0.6) -> WhipPan:
        return WhipPan(duration=ensure_positive_duration(duration, "tx.whip_pan duration"), direction=direction, intensity=intensity)

    def light_leak(self, duration: Time, *, intensity: float = 0.7) -> LightLeak:
        return LightLeak(duration=ensure_positive_duration(duration, "tx.light_leak duration"), intensity=intensity)

    def film_burn(self, duration: Time, *, intensity: float = 0.7) -> FilmBurn:
        return FilmBurn(duration=ensure_positive_duration(duration, "tx.film_burn duration"), intensity=intensity)

    def beat_flash(self, duration: Time, *, stream: str = "beat", intensity: float = 0.7) -> BeatFlash:
        return BeatFlash(duration=ensure_positive_duration(duration, "tx.beat_flash duration"), stream=stream, intensity=intensity)

    def beat_glitch(self, duration: Time, *, stream: str = "beat", intensity: float = 0.7) -> BeatGlitch:
        return BeatGlitch(duration=ensure_positive_duration(duration, "tx.beat_glitch duration"), stream=stream, intensity=intensity)

    def custom(self, kind: str, **params: Any) -> TransitionBase:
        return _custom("transition", kind, params, TransitionBase)


class Curves:
    def sine(self, freq_hz: float, *, phase: float = 0.0, amp: float = 1.0, offset: float = 0.0) -> Sine:
        return Sine(freq_hz=freq_hz, phase=phase, amp=amp, offset=offset)

    def lfo(
        self, shape: Literal["tri", "saw", "square"], freq_hz: float, *, amp: float = 1.0, offset: float = 0.0,
        phase: float = 0.0,
    ) -> LFO:
        return LFO(shape=shape, freq_hz=freq_hz, amp=amp, offset=offset, phase=phase)

    def noise(
        self, *, seed: int | None = None, min_value: float = 0.0, max_value: float = 1.0, hold: Time | None = None
    ) -> Noise:
        return Noise(seed=seed, min_value=min_value, max_value=max_value, hold=optional_positive_duration(hold, "curves.noise hold"))

    def random_choice(self, values: Sequence[float], hold: Time | None = None, *, seed: int | None = None) -> RandomChoice:
        checked_values = _values(values, "curves.random_choice values")
        if hold is None:
            return RandomChoice(values=checked_values, seed=seed)
        return RandomChoice(values=checked_values, hold=ensure_positive_duration(hold, "curves.random_choice hold"), seed=seed)

    def step(self, values: Sequence[float], hold: Time) -> Step:
        return Step(values=_values(values, "curves.step values"), hold=ensure_positive_duration(hold, "curves.step hold"))

    def spring(
        self, target: float, *, stiffness: float = 100.0, damping: float = 10.0, initial: float = 0.0
    ) -> Spring:
        return Spring(target=target, stiffness=stiffness, damping=damping, initial=initial)

    def bezier(self, p0: float, p1: float, p2: float, p3: float, duration: Time) -> Bezier:
        return Bezier(p0=p0, p1=p1, p2=p2, p3=p3, duration=ensure_positive_duration(duration, "curves.bezier duration"))

    def bounce(self, amplitude: float, period: Time, *, decay: float = 1.0) -> Bounce:
        return Bounce(amplitude=amplitude, period=ensure_positive_duration(period, "curves.bounce period"), decay=decay)

    def exp_decay(self, tau: Time, *, baseline: float = 0.0) -> ExpDecay:
        return ExpDecay(tau=ensure_positive_duration(tau, "curves.exp_decay tau"), baseline=baseline)

    def expr(self, expr: str) -> Lambda:
        return Lambda(expr=expr)

    def beat_pulse(
        self, trigger: TimeSet | Time, envelope: Envelope, *, intensity: float | Keys[float] = 1.0,
        baseline: float = 0.0,
    ) -> BeatPulse:
        times = trigger if isinstance(trigger, TimeSet) else t.set(ensure_time(trigger, "curves.beat_pulse trigger"))
        if not isinstance(envelope, ENVELOPES):
            raise TypeError(
                "curves.beat_pulse envelope expects curves.exp_decay_env(...), linear_env(...), box_env(...) or "
                f"easing_env(...), got {type(envelope).__name__} {envelope!r}"
            )
        level = intensity if isinstance(intensity, Keys) else _number(intensity, "curves.beat_pulse intensity")
        result = BeatPulse(trigger=times, envelope=envelope, intensity=level, baseline=baseline)
        require_catalogs(result)
        return result

    def exp_decay_env(self, tau: Time) -> ExpDecayEnv:
        return ExpDecayEnv(tau=ensure_positive_duration(tau, "curves.exp_decay_env tau"))

    def linear_env(self, rise: Time, fall: Time) -> LinearEnv:
        return LinearEnv(rise=ensure_positive_duration(rise, "curves.linear_env rise"), fall=ensure_positive_duration(fall, "curves.linear_env fall"))

    def box_env(self, width: Time) -> BoxEnv:
        return BoxEnv(width=ensure_positive_duration(width, "curves.box_env width"))

    def easing_env(self, duration: Time, ease: EaseLike, *, direction: Literal["in", "out"] = "out") -> EasingEnv:
        easing = _easing(ease, "curves.easing_env ease")
        if easing is None:
            raise TypeError("curves.easing_env needs an easing such as 'out_elastic'")
        return EasingEnv(duration=ensure_positive_duration(duration, "curves.easing_env duration"), easing=easing, direction=direction)

    def custom(self, kind: str, **params: Any) -> CurveBase:
        return _custom("curve", kind, params, CurveBase)


def _values(values: Sequence[float], what: str) -> tuple[float, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise TypeError(f"{what} expects a sequence of numbers, got {type(values).__name__} {values!r}")
    return tuple(_number(value, what) for value in values)


fx = Fx()
tx = Tx()
curves = Curves()

__all__ = [
    "CATALOG_BASES",
    "MAPPING_ORIGINS",
    "SEQUENCE_ORIGINS",
    "AnimFloat",
    "AnimInt",
    "CaptionOptions",
    "Curves",
    "EaseLike",
    "Fx",
    "Tx",
    "animated",
    "catalog_registry",
    "check_times",
    "curves",
    "driven",
    "ease",
    "fx",
    "keys",
    "require_catalogs",
    "tx",
]
