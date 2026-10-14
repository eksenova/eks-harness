from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import partial
from types import UnionType
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

from pydantic import BaseModel

from eks.audio import AudioMix
from eks.effects import (
    MAPPING_ORIGINS,
    SEQUENCE_ORIGINS,
    AnimFloat,
    animated,
    check_times,
    require_catalogs,
)
from eks.media import MediaLike, as_media, source
from eks.ref import Media, Ref, encode, node_of
from eks.scenes import embed_scene
from eks.tracing import TraceContext, TraceError
from eks_harness.ir.catalog.animated import AFloat
from eks_harness.ir.catalog.durations import ensure_positive_duration
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.catalog.transitions import TransitionBase
from eks_harness.ir.entries import EditOptions
from eks_harness.ir.ops.check import DEFAULT_OCR_LANGUAGES, OcrEngine
from eks_harness.ir.ops.edit import (
    TRACK_NAME,
    Blend,
    Clip,
    Encoder,
    Fit,
    RenderSettings,
    Timeline,
    Track,
)
from eks_harness.ir.ops.overlay import Caption, OverlayBase, Title
from eks_harness.ir.registry import EntrySpec, Registry
from eks_harness.ir.target import (
    Target,
    TargetError,
    check_target,
    ensure_target,
    validate_args,
)
from eks_harness.ir.time import Anchor, Span, Time, ensure_time, format_fps, t


class ClipFields(TypedDict, total=False):
    media: MediaLike
    start: Time
    src: Span | None
    speed: AnimFloat
    opacity: AnimFloat
    x: AnimFloat
    y: AnimFloat
    scale: AnimFloat
    rotation: AnimFloat
    fit: Fit
    audio: bool
    gain: AnimFloat
    effects: Sequence[EffectBase]
    transition_in: TransitionBase | None
    transition_out: TransitionBase | None


class RenderSettingsFields(TypedDict, total=False):
    encoder: Encoder | None
    threads: int
    preset: str
    quality: Literal["preview", "draft", "final"]
    color_primaries: str
    color_trc: str
    colorspace: str
    pix_fmt: str
    bitrate_kbps: int | None
    crf: int | None


RENDER_OP = "edit.render"
RENDER_ID_KEY = "final:edit.render"
CHECK_OP = "check.media"
FINALIZE_ORDER = 100
VIDEO_SURFACE = "video"
DERIVED_LABEL = "derived"
BEAT_ANCHORS = frozenset({"beat", "downbeat", "bar"})
DERIVED_STREAM = "beat"

_TRACK_NAME = re.compile(TRACK_NAME)
_ANIMATED_CLIP_FIELDS = frozenset({"speed", "opacity", "x", "y", "scale", "rotation", "gain"})


@dataclass(frozen=True, slots=True)
class _Expectation:
    kind: str
    params: dict[str, Any]
    at: Time | Span | None
    tolerance: Time | None
    label: str | None
    soft: bool
    id_key: str


@dataclass(frozen=True, slots=True)
class _Derive:
    cuts: bool
    captions: bool
    beats: bool
    loudness: bool


def _label(kind: str, index: int) -> str:
    return f"{DERIVED_LABEL}:{kind}:{index}"


def _continuous(previous: Clip, following: Clip) -> bool:
    return (previous.media == following.media and previous.src is not None and following.src is not None
            and previous.src.end == following.src.start)


def _hard_cuts(timeline: Timeline) -> list[Time]:
    tracks = [track for track in timeline.tracks if track.clips]
    if not tracks:
        return []
    lowest = min(tracks, key=lambda track: track.z)
    cuts: list[Time] = []
    for previous, following in zip(lowest.clips, lowest.clips[1:], strict=False):
        if previous.transition_out is not None or following.transition_in is not None:
            continue
        if following.start == previous.start or _continuous(previous, following):
            continue
        cuts.append(following.start)
    return cuts


def _beat_anchored(timeline: Timeline) -> bool:
    return any(clip.start.anchor is not None and clip.start.anchor.kind in BEAT_ANCHORS
               for track in timeline.tracks for clip in track.clips)


def _cut_span(cuts: Sequence[Time]) -> Span:
    try:
        return Span(start=cuts[0], end=cuts[-1])
    except ValueError:
        return Span(start=cuts[-1], end=cuts[0])


def _half(value: Fraction) -> Fraction:
    return value / 2


def _middle(window: Span) -> Time | Span:
    start, end = window.start, window.end
    anchor = start.anchor
    frames = _half(start.frames + end.frames)
    if start.anchor != end.anchor:
        first, last = start.anchor, end.anchor
        if first is None or last is None or first.kind != last.kind or first.index is None or last.index is None:
            return window
        if first.kind == "frame":
            whole = (first.index + last.index) // 2
            frames += _half(first.index + last.index) - whole
            anchor = Anchor(kind="frame", index=Fraction(whole))
        elif first.kind in BEAT_ANCHORS:
            anchor = Anchor(kind=first.kind, index=_half(first.index + last.index))
        else:
            return window
    return Time(anchor=anchor, bars=_half(start.bars + end.bars), beats=_half(start.beats + end.beats),
                frames=frames, ticks=(start.ticks + end.ticks) // 2)


def _maybe_length(value: object, what: str) -> Time | None:
    return None if value is None else ensure_positive_duration(value, what)


def _span(value: object, what: str) -> Span:
    if not isinstance(value, Span):
        raise TypeError(f"{what} expects a Span such as t.span(t('2s'), t('14s')), got {type(value).__name__} {value!r}")
    return value


def _time_or_span(value: object, what: str) -> Time | Span:
    return value if isinstance(value, Span) else ensure_time(value, what)


def _unwrap(annotation: object) -> object:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _accepts_text(member: object, value: str) -> bool:
    if member is str:
        return True
    return get_origin(member) is Literal and value in get_args(member)


def _effect(value: object) -> EffectBase:
    if not isinstance(value, EffectBase):
        raise TypeError(f"clip effects are eks.fx.* values or effect models, got {type(value).__name__} {value!r}")
    require_catalogs(value)
    return value


def _transition(value: object, what: str) -> TransitionBase:
    if not isinstance(value, TransitionBase):
        raise TypeError(f"{what} expects an eks.tx.* value or a transition model, got {type(value).__name__} {value!r}")
    require_catalogs(value)
    return value


class ClipBuilder:
    def __init__(self, track: TrackBuilder, index: int) -> None:
        self._track = track
        self._index = index
        self._fields: dict[str, Any] = {}
        self._effects: list[EffectBase] = []

    @property
    def id(self) -> str:
        return f"{self._track.name}/{self._index}"

    def effect(self, *effects: EffectBase) -> ClipBuilder:
        self._track.edit_builder.require_open("clip.effect()")
        return self._commit(effects=[*self._effects, *(_effect(item) for item in effects)])

    def transition_in(self, transition: TransitionBase) -> ClipBuilder:
        self._track.edit_builder.require_open("clip.transition_in()")
        return self._commit(fields={**self._fields, "transition_in": _transition(transition, "clip.transition_in()")})

    def transition_out(self, transition: TransitionBase) -> ClipBuilder:
        self._track.edit_builder.require_open("clip.transition_out()")
        return self._commit(
            fields={**self._fields, "transition_out": _transition(transition, "clip.transition_out()")})

    def set(self, **fields: Unpack[ClipFields]) -> ClipBuilder:
        self._track.edit_builder.require_open("clip.set()")
        known = set(Clip.model_fields) - {"id"}
        changed = dict(self._fields)
        effects = list(self._effects)
        for key, value in fields.items():
            if key not in known:
                raise TypeError(
                    f"clip field {key!r} cannot be set; settable fields: {', '.join(sorted(known))}"
                    + ("; the clip id is '<track>/<index>'" if key == "id" else "")
                )
            if key == "effects":
                effects = [_effect(item) for item in cast("Sequence[Any]", value)]
            else:
                changed[key] = self._convert(key, value)
        return self._commit(fields=changed, effects=effects)

    def payload(self) -> dict[str, Any]:
        return self._payload(self._fields, self._effects)

    def model(self) -> Clip:
        return Clip.model_validate(encode(self.payload()))

    def _payload(self, fields: dict[str, Any], effects: list[EffectBase]) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, **fields}
        if effects:
            data["effects"] = list(effects)
        return data

    def _commit(
        self, *, fields: dict[str, Any] | None = None, effects: list[EffectBase] | None = None
    ) -> ClipBuilder:
        chosen_fields = self._fields if fields is None else fields
        chosen_effects = self._effects if effects is None else effects
        Clip.model_validate(encode(self._payload(chosen_fields, chosen_effects)))
        self._fields, self._effects = chosen_fields, chosen_effects
        return self

    def _convert(self, key: str, value: Any) -> Any:
        if key == "media":
            return as_media(value)
        if key == "start":
            return ensure_time(value, "clip start")
        if key == "src":
            return None if value is None else _span(value, "clip src")
        if key in _ANIMATED_CLIP_FIELDS:
            return animated(value)
        if key in {"transition_in", "transition_out"}:
            return None if value is None else _transition(value, f"clip {key}")
        return value


class TrackBuilder:
    def __init__(self, edit: EditBuilder, name: str) -> None:
        self._edit = edit
        self._name = name
        self._z = 0
        self._blend: Blend = "normal"
        self._opacity: AFloat = 1.0
        self._clips: list[ClipBuilder] = []
        self._overlays: list[OverlayBase] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def edit_builder(self) -> EditBuilder:
        return self._edit

    def clip(
        self, media: MediaLike, *, start: Time | None = None, src: Span | None = None, speed: AnimFloat = 1.0,
        opacity: AnimFloat = 1.0, fit: Fit = "cover", audio: bool = False, gain: AnimFloat = 0.0,
    ) -> ClipBuilder:
        self._edit.require_open("track.clip()")
        builder = ClipBuilder(self, len(self._clips))
        values: dict[str, Any] = {
            "media": media, "start": start, "src": src, "speed": speed, "opacity": opacity, "fit": fit,
            "audio": audio, "gain": gain}
        builder.set(**{key: value for key, value in values.items() if value is not None})
        self._clips.append(builder)
        return builder

    def overlay(self, *overlays: OverlayBase) -> TrackBuilder:
        self._edit.require_open("track.overlay()")
        converted = [self._edit.video_overlay(item) for item in overlays]
        self._overlays.extend(converted)
        return self

    def configure(
        self, *, z: int | None = None, blend: Blend | None = None, opacity: AnimFloat | None = None
    ) -> TrackBuilder:
        self._edit.require_open("track.configure()")
        if z is not None:
            if isinstance(z, bool) or not isinstance(z, int):
                raise TypeError(f"track z expects an int, got {type(z).__name__} {z!r}")
            self._z = z
        if blend is not None:
            self._blend = Track(name=self._name, blend=blend).blend
        if opacity is not None:
            self._opacity = animated(opacity)
        return self

    def payload(self) -> dict[str, Any]:
        return {
            "name": self._name, "z": self._z, "blend": self._blend, "opacity": self._opacity,
            "clips": [clip.payload() for clip in self._clips], "overlays": list(self._overlays)}


class EditExpect:
    def __init__(self, edit: EditBuilder) -> None:
        self._edit = edit

    def use(
        self, kind: str, *, at: Time | Span | None = None, tolerance: Time | None = None, label: str | None = None,
        soft: bool = False, **params: Any,
    ) -> None:
        self._edit.queue_check(
            kind, params, at=None if at is None else _time_or_span(at, f"expect.{kind} at"),
            tolerance=tolerance, label=label, soft=soft)

    def __getattr__(self, kind: str) -> Callable[..., None]:
        if kind.startswith("_"):
            raise AttributeError(kind)
        media_kinds = [spec.kind for spec in self._edit.registry.checks("media")]
        if kind not in media_kinds:
            suggestions = difflib.get_close_matches(kind, media_kinds, n=3, cutoff=0.6)
            hint = (f"did you mean {' or '.join(repr(name) for name in suggestions)}?" if suggestions
                    else f"media checks: {', '.join(media_kinds)}")
            raise AttributeError(f"edit.expect has no media check {kind!r}; {hint}")
        return partial(self.use, kind)

    def cut(self, *, at: Time, tolerance: Time | None = None, threshold: float = 0.1, label: str | None = None,
            soft: bool = False) -> None:
        self._edit.queue_check("cut", {"threshold": threshold}, at=ensure_time(at, "expect.cut at"),
                               tolerance=tolerance, label=label, soft=soft)

    def beats(self, stream: str = "beat", *, at: Span | None = None, min_ratio: float = 0.8,
              tolerance: Time | None = None, label: str | None = None, soft: bool = False) -> None:
        self._edit.queue_check("beats", {"stream": stream, "min_ratio": min_ratio},
                               at=None if at is None else _span(at, "expect.beats at"), tolerance=tolerance,
                               label=label, soft=soft)

    def onset(self, *, at: Time, sensitivity: float = 0.5, tolerance: Time | None = None, label: str | None = None,
              soft: bool = False) -> None:
        self._edit.queue_check("onset", {"sensitivity": sensitivity}, at=ensure_time(at, "expect.onset at"),
                               tolerance=tolerance, label=label, soft=soft)

    def loudness(self, lufs: float, *, tolerance: float = 1.0, true_peak: float | None = None,
                 label: str | None = None, soft: bool = False) -> None:
        self._edit.queue_check("loudness", {"lufs": lufs, "tolerance": tolerance, "true_peak": true_peak},
                               at=None, tolerance=None, label=label, soft=soft)

    def text(self, text: str, *, at: Time | Span, region: Target | None = None, engine: OcrEngine = "auto",
             languages: Sequence[str] = DEFAULT_OCR_LANGUAGES, label: str | None = None, soft: bool = False) -> None:
        if isinstance(languages, str):
            raise TypeError(f"expect.text languages is a list of language tags such as ['tr-TR'], got {languages!r}")
        self._edit.queue_check("text", {"text": text, "region": self._edit.region(region), "engine": engine,
                                        "languages": list(languages)},
                               at=_time_or_span(at, "expect.text at"), tolerance=None, label=label, soft=soft)

    def motion(self, *, at: Span | None = None, min: float | None = None, max: float | None = None,
               label: str | None = None, soft: bool = False) -> None:
        self._edit.queue_check("motion", {"min": min, "max": max},
                               at=None if at is None else _span(at, "expect.motion at"), tolerance=None,
                               label=label, soft=soft)

    def black(self, *, longest: Time | None = None, at: Span | None = None, label: str | None = None,
              soft: bool = False) -> None:
        self._edit.queue_check("black", {"longest": _maybe_length(longest, "expect.black longest")},
                               at=None if at is None else _span(at, "expect.black at"), tolerance=None,
                               label=label, soft=soft)

    def frozen(self, *, longest: Time | None = None, at: Span | None = None, label: str | None = None,
               soft: bool = False) -> None:
        self._edit.queue_check("frozen", {"longest": _maybe_length(longest, "expect.frozen longest")},
                               at=None if at is None else _span(at, "expect.frozen at"), tolerance=None,
                               label=label, soft=soft)

    def safe_area(self, *, margins: tuple[float, float, float, float] = (0.1, 0.05, 0.2, 0.05),
                  region: Target | None = None, at: Span | None = None, engine: OcrEngine = "auto",
                  label: str | None = None, soft: bool = False) -> None:
        self._edit.queue_check("safe_area", {"margins": margins, "region": self._edit.region(region),
                                             "engine": engine},
                               at=None if at is None else _span(at, "expect.safe_area at"), tolerance=None,
                               label=label, soft=soft)

    def derive(self, *, cuts: bool = True, captions: bool = True, beats: bool = True, loudness: bool = True) -> None:
        self._edit.derive(cuts=cuts, captions=captions, beats=beats, loudness=loudness)

    def template(self, template: MediaLike, *, at: Time | Span, threshold: float = 0.8,
                 region: Target | None = None, label: str | None = None, soft: bool = False) -> None:
        self._edit.queue_check("template", {"template": as_media(template, kind="image"), "threshold": threshold,
                                            "region": self._edit.region(region)},
                               at=_time_or_span(at, "expect.template at"), tolerance=None, label=label, soft=soft)


class EditBuilder:
    def __init__(
        self, ctx: TraceContext, *, fps: Fraction, size: tuple[int, int], seed: int = 0,
        duration: Time | None = None, background: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._scope_key = ctx.scope().key
        self._fps = fps
        self._size = size
        self._seed = seed
        self._duration = duration
        self._background = background
        self._audio = AudioMix(ctx)
        self._expect = EditExpect(self)
        self._tracks: dict[str, TrackBuilder] = {}
        self._settings: RenderSettings | None = None
        self._checks: list[_Expectation] = []
        self._derive: _Derive | None = None
        self._closing = False
        self._render: Media | None = None

    @property
    def audio(self) -> AudioMix:
        return self._audio

    @property
    def registry(self) -> Registry:
        return self._ctx.registry

    @property
    def expect(self) -> EditExpect:
        return self._expect

    @property
    def output(self) -> Media:
        if self._render is None:
            raise TraceError(
                "the edit output exists once the edit is finalized (after the entry function returns); "
                "refer to the render from a later step, or call finalize() when embedding the edit"
            )
        return self._render

    def media(self, uri: str) -> Media:
        self.require_open("edit.media()")
        return source(uri)

    def scene(self, scene: Callable[..., Any] | str, /, **inputs: Any) -> Media:
        self.require_open("edit.scene()")
        return embed_scene(self._ctx, scene, inputs)

    def gfx(self, scene: Callable[..., Any] | str, /, **inputs: Any) -> Media:
        self.require_open("edit.gfx()")
        return embed_scene(self._ctx, scene, inputs, kind="gfx")

    def track(
        self, name: str, *, z: int | None = None, blend: Blend | None = None, opacity: AnimFloat | None = None
    ) -> TrackBuilder:
        self.require_open("edit.track()")
        track = self._tracks.get(name)
        if track is None:
            if not isinstance(name, str) or not _TRACK_NAME.fullmatch(name):
                raise TraceError(
                    f"track name {name!r} must match {TRACK_NAME} (letters, digits, '_', '.', '-', at most 64)")
            track = TrackBuilder(self, name)
            track.configure(z=len(self._tracks))
            self._tracks[name] = track
        return track.configure(z=z, blend=blend, opacity=opacity)

    def cue(self, name: str, at: Time) -> Time:
        self.require_open("edit.cue()")
        anchor = t.cue(name)
        time = ensure_time(at, f"cue {name!r} time")
        if time.anchor is not None and time.anchor.kind == "cue":
            raise TraceError(f"cue {name!r} cannot be defined relative to another cue; anchor it on a time or beat")
        cues = dict(self._ctx.clock.cues)
        if name in cues and cues[name] != time:
            raise TraceError(f"cue {name!r} is already defined at {cues[name]}; pick another name")
        cues[name] = time
        self._ctx.update_clock(cues=cues)
        return anchor

    def render_settings(self, **fields: Unpack[RenderSettingsFields]) -> None:
        self.require_open("edit.render_settings()")
        current = self._settings.model_dump() if self._settings is not None else {}
        self._settings = RenderSettings.model_validate({**current, **fields})

    def finalize(self) -> Media:
        if self._render is not None:
            return self._render
        if self._closing:
            raise TraceError("the edit is being finalized; it cannot render itself")
        self._require_scope("edit.finalize()")
        self._closing = True
        audio = self._audio.finalize(duration=self._duration)
        timeline: dict[str, Any] = {
            "fps": format_fps(self._fps), "size": list(self._size), "seed": self._seed,
            "tracks": [track.payload() for track in self._tracks.values()]}
        if self._duration is not None:
            timeline["duration"] = self._duration
        if self._background is not None:
            timeline["background"] = self._background
        if audio is not None:
            timeline["audio"] = audio
        if self._settings is not None:
            timeline["render"] = self._settings
        model = Timeline.model_validate(encode(timeline))
        self._render = cast("Media", self._ctx.record(RENDER_OP, {"timeline": model}, id_key=RENDER_ID_KEY))
        derived = self._derived(model, audio) if self._derive is not None else []
        for check in [*self._checks, *derived]:
            args: dict[str, Any] = {
                "media": self._render, "kind": check.kind, "params": check.params, "at": check.at,
                "tolerance": check.tolerance, "label": check.label, "soft": check.soft}
            self._ctx.record(CHECK_OP, {key: value for key, value in args.items() if value is not None},
                             label=check.label, id_key=check.id_key)
        return self._render

    def derive(self, *, cuts: bool, captions: bool, beats: bool, loudness: bool) -> None:
        self.require_open("edit.expect.derive()")
        for name, value in (("cuts", cuts), ("captions", captions), ("beats", beats), ("loudness", loudness)):
            if not isinstance(value, bool):
                raise TypeError(f"expect.derive {name} expects a bool, got {type(value).__name__} {value!r}")
        self._derive = _Derive(cuts=cuts, captions=captions, beats=beats, loudness=loudness)

    def _derived(self, timeline: Timeline, audio: Media | None) -> list[_Expectation]:
        chosen = self._derive
        if chosen is None:
            return []
        found: list[_Expectation] = []
        cuts = _hard_cuts(timeline)
        if chosen.cuts:
            found.extend(self._expectation("cut", {}, at=time, tolerance=None, label=_label("cut", index),
                                           soft=False, id_key=_label("cut", index))
                         for index, time in enumerate(cuts))
        if chosen.captions:
            bounded = self._ctx.clock.window is not None or self._ctx.clock.duration is not None
            middles = [(_middle(overlay.window), overlay.text) for track in timeline.tracks
                       for overlay in track.overlays
                       if isinstance(overlay, Caption | Title) and isinstance(overlay.text, str)
                       and overlay.window is not None]
            texts = [(at, text) for at, text in middles if bounded or not at.needs().window]
            found.extend(self._expectation("text", {"text": text}, at=at, tolerance=None, label=_label("text", index),
                                           soft=True, id_key=_label("text", index))
                         for index, (at, text) in enumerate(texts))
        if chosen.beats and cuts and _beat_anchored(timeline):
            found.append(self._expectation("beats", {"stream": DERIVED_STREAM}, at=_cut_span(cuts), tolerance=None,
                                           label=_label("beats", 0), soft=False, id_key=_label("beats", 0)))
        if chosen.loudness and audio is not None:
            master = self._master(audio)
            if master is not None:
                found.append(self._expectation("loudness", master, at=None, tolerance=None,
                                               label=_label("loudness", 0), soft=False,
                                               id_key=_label("loudness", 0)))
        return found

    def _master(self, audio: Media) -> dict[str, Any] | None:
        mix = self._ctx.node(node_of(audio)).args.get("mix")
        master = mix.get("master") if isinstance(mix, dict) else None
        if not isinstance(master, dict):
            return None
        loudness = master.get("loudness")
        if not isinstance(loudness, int | float) or isinstance(loudness, bool):
            return None
        params: dict[str, Any] = {"lufs": float(loudness)}
        peak = master.get("true_peak")
        if isinstance(peak, int | float) and not isinstance(peak, bool):
            params["true_peak"] = float(peak)
        return params

    def require_open(self, what: str) -> None:
        if self._closing:
            raise TraceError(f"{what}: the edit is already finalized; build the edit inside the entry function")
        self._require_scope(what)

    def _require_scope(self, what: str) -> None:
        if self._ctx.scope().key != self._scope_key:
            raise TraceError(
                f"{what}: edits are static timelines; build clips, tracks, audio and expectations outside "
                "eks.when, eks.each and eks.dynamic bodies (use a Python if or for)"
            )

    def video_overlay(self, overlay: OverlayBase) -> OverlayBase:
        if not isinstance(overlay, OverlayBase):
            raise TypeError(
                f"track.overlay() takes overlay models such as Caption(text=...), got {type(overlay).__name__} "
                f"{overlay!r}"
            )
        registry = self._ctx.registry
        spec = registry.catalog("overlay", overlay.kind)
        if VIDEO_SURFACE not in spec.renderers:
            raise TraceError(
                f"overlay kind {overlay.kind!r} has no video renderer (renderers: {sorted(spec.renderers)}); "
                "it cannot be drawn in an edit track"
            )
        require_catalogs(overlay)
        kinds = registry.target_kinds()
        updates: dict[str, Target] = {}
        for name in type(overlay).model_fields:
            value = getattr(overlay, name)
            if isinstance(value, Target):
                target = validate_args(value, kinds)
                problems = check_target(target, VIDEO_SURFACE, kinds)
                if problems:
                    raise TargetError(f"{overlay.kind} overlay {name}: {'; '.join(problems)}")
                updates[name] = target
        return overlay.model_copy(update=updates) if updates else overlay

    def region(self, value: object) -> Target | None:
        return None if value is None else self.target(value, "region")

    def target(self, value: object, what: str) -> Target:
        kinds = self._ctx.registry.target_kinds()
        target = ensure_target(value, what, kinds)
        problems = check_target(target, VIDEO_SURFACE, kinds)
        if problems:
            raise TargetError(f"{what} {value!r}: {'; '.join(problems)}")
        return target

    def convert_targets(self, annotation: object, value: object, what: str) -> object:
        annotation = _unwrap(annotation)
        if value is None:
            return None
        if annotation is Target:
            return self.target(value, what)
        origin = get_origin(annotation)
        arguments = tuple(_unwrap(argument) for argument in get_args(annotation))
        if origin is Union or origin is UnionType:
            return self._convert_union(arguments, value, what)
        if isinstance(value, list | tuple) and origin in SEQUENCE_ORIGINS:
            items = list(cast("Sequence[object]", value))
            if origin is tuple and not (len(arguments) == 2 and arguments[1] is Ellipsis):
                return [
                    self.convert_targets(arguments[index], item, f"{what}[{index}]") if index < len(arguments) else item
                    for index, item in enumerate(items)]
            if arguments:
                return [self.convert_targets(arguments[0], item, f"{what}[{index}]") for index, item in enumerate(items)]
            return value
        if isinstance(value, Mapping) and origin in MAPPING_ORIGINS and len(arguments) == 2:
            return {key: self.convert_targets(arguments[1], item, f"{what}[{key!r}]")
                    for key, item in cast("Mapping[object, object]", value).items()}
        return value

    def _convert_union(self, members: tuple[object, ...], value: object, what: str) -> object:
        if Target in members and (isinstance(value, Target) or (
                isinstance(value, str) and not any(_accepts_text(member, value) for member in members))):
            return self.target(value, what)
        for member in members:
            origin = get_origin(member)
            if (isinstance(value, list | tuple) and origin in SEQUENCE_ORIGINS) or (
                isinstance(value, Mapping) and origin in MAPPING_ORIGINS
            ):
                return self.convert_targets(member, value, what)
        return value

    def prepare_params(self, kind: str, model: type[BaseModel], params: Mapping[str, Any]) -> dict[str, Any]:
        fields = model.model_fields
        prepared: dict[str, Any] = {}
        for name, value in params.items():
            if value is None:
                continue
            info = fields.get(name)
            if info is not None:
                what = f"expect.{kind} {name}"
                check_times(info.annotation, value, what)
                value = self.convert_targets(info.annotation, value, what)
            prepared[name] = encode(value) if isinstance(value, Ref) else value
        return prepared

    def queue_check(
        self, kind: str, params: dict[str, Any], *, at: Time | Span | None, tolerance: Time | None,
        label: str | None, soft: bool,
    ) -> None:
        self.require_open(f"edit.expect.{kind}()")
        site = self._ctx.call_site()
        where = (f"{site.file}:{site.qualname}:"
                 f"{hashlib.sha256((site.text or f'L{site.line}').encode('utf-8')).hexdigest()[:12]}"
                 if site is not None else "lib")
        self._checks.append(self._expectation(kind, params, at=at, tolerance=tolerance, label=label, soft=soft,
                                              id_key=f"expect:{kind}:{where}"))

    def _expectation(
        self, kind: str, params: Mapping[str, Any], *, at: Time | Span | None, tolerance: Time | None,
        label: str | None, soft: bool, id_key: str,
    ) -> _Expectation:
        checked_tolerance = _maybe_length(tolerance, f"expect.{kind} tolerance")
        spec = self._ctx.registry.check(kind)
        if spec.family != "media":
            raise TraceError(f"check {kind!r} is a {spec.family} check, not a media check")
        model = spec.args.model_validate(self.prepare_params(kind, spec.args, params))
        self._ctx.require(spec.provenance)
        return _Expectation(
            kind=kind, params=model.model_dump(mode="json", by_alias=True, exclude_none=True), at=at,
            tolerance=checked_tolerance, label=label, soft=soft, id_key=id_key)


Edit = EditBuilder


def open_edit(ctx: TraceContext, entry: EntrySpec) -> EditBuilder:
    options = entry.options
    if not isinstance(options, EditOptions):
        raise TraceError(f"{entry.kind} {entry.name!r} is not an edit; open_edit builds @eks.edit entries only")
    builder = EditBuilder(
        ctx, fps=options.fps, size=options.size, seed=options.seed, duration=options.duration,
        background=options.background)

    def finish(_ctx: TraceContext) -> None:
        builder.finalize()

    ctx.on_finalize(finish, order=FINALIZE_ORDER)
    return builder


__all__ = [
    "CHECK_OP",
    "FINALIZE_ORDER",
    "RENDER_ID_KEY",
    "RENDER_OP",
    "ClipBuilder",
    "ClipFields",
    "Edit",
    "EditBuilder",
    "EditExpect",
    "RenderSettingsFields",
    "TrackBuilder",
    "open_edit",
]
