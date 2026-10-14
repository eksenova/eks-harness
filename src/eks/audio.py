from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, TypedDict, Unpack, cast
from urllib.parse import unquote, urlparse

from pydantic import ValidationError

from eks.effects import AnimFloat, animated
from eks.media import SOURCE_OP, MediaLike, as_media
from eks.media import markers as media_markers
from eks.ref import Json, Media, Ref, encode, node_of, path_of
from eks.tracing import TraceContext, TraceError
from eks_harness.ir.catalog.durations import (
    ensure_positive_duration,
    optional_positive_duration,
)
from eks_harness.ir.catalog.markers import (
    BeatBackend,
    BeatStream,
    BeatTracker,
    MarkerBase,
)
from eks_harness.ir.ops.audio import (
    TRACK_NAME,
    AudioTrack,
    Compressor,
    Denoise,
    Duck,
    Eq,
    EqBand,
    Fades,
    Master,
    Mix,
    Reverb,
    Stem,
)
from eks_harness.ir.time import Span, TempoMap, Time, ensure_time


class AudioTrackFields(TypedDict, total=False):
    media: MediaLike
    start: Time
    src: Span | None
    gain: AnimFloat
    pan: AnimFloat
    mute: bool
    stems: Sequence[Stem] | None
    eq: Eq | None
    compressor: Compressor | None
    ducks: Sequence[Duck]
    fades: Fades | None
    reverb: Reverb | None
    denoise: Denoise | None


MIX_OP = "audio.mix"
MIX_ID_KEY = "final:audio.mix"
DEFAULT_BEAT_STREAMS: tuple[BeatStream, ...] = ("beat", "downbeat")
TEMPO_STREAM: BeatStream = "beat"

_TRACK_NAME = re.compile(TRACK_NAME)
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_MARKER_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_ANIMATED_FIELDS = frozenset({"gain", "pan"})
_DOTLESS = str.maketrans({"\u0131": "i", "\u0130": "I"})


@dataclass
class _Track:
    name: str
    fields: dict[str, Any]
    ducks: list[tuple[Duck, str]] = field(default_factory=lambda: list[tuple[Duck, str]]())


def _span(value: object, what: str) -> Span:
    if not isinstance(value, Span):
        raise TypeError(f"{what} expects a Span such as t.span(t('2s'), t('14s')), got {type(value).__name__} {value!r}")
    return value


def _stem(uri: str) -> str | None:
    if uri.startswith("file:"):
        path = uri.removeprefix("file:")
    elif uri.startswith(("http://", "https://")):
        path = unquote(urlparse(uri).path)
    else:
        return None
    stem = _NAME_UNSAFE.sub("_", fold_ascii(PurePosixPath(path).stem)).lstrip(".-")[:64]
    return stem or None


def fold_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.translate(_DOTLESS))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def marker_name(text: str) -> str:
    name = _MARKER_UNSAFE.sub("_", fold_ascii(text))
    return name if name and not name[0].isdigit() else f"_{name}"


class AudioTrackBuilder:
    def __init__(self, mix: AudioMix, index: int) -> None:
        self._mix = mix
        self._index = index

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def media(self) -> Media:
        return cast("Media", self._state.fields["media"])

    @property
    def _state(self) -> _Track:
        return self._mix._tracks[self._index]

    def beats(
        self, *streams: BeatStream, bpm: float | None = None, backend: BeatBackend = "auto", name: str | None = None
    ) -> AudioTrackBuilder:
        self._mix._require_open("track.beats()")
        tracker = BeatTracker(
            name=name if name is not None else marker_name(self.name), streams=streams or DEFAULT_BEAT_STREAMS,
            bpm=bpm, backend=backend)
        ctx = self._mix._ctx
        sets_tempo = ctx.clock.tempo is None
        if sets_tempo and TEMPO_STREAM not in tracker.streams:
            raise TraceError(
                f"track.beats({', '.join(repr(stream) for stream in tracker.streams)}) on {self.name!r} would make "
                f"it the clock's tempo source, but a tempo needs the {TEMPO_STREAM!r} stream; add it, as in "
                f".beats('beat', {', '.join(repr(stream) for stream in tracker.streams)}), or analyse without a tempo "
                f"through .markers(eks.media.BeatTracker(name=..., streams=(...)))")
        ref = media_markers(self.media, tracker)
        if sets_tempo:
            ctx.update_clock(tempo=TempoMap(source=node_of(ref)))
        return self

    def markers(self, *sources: MarkerBase) -> Ref[Json]:
        self._mix._require_open("track.markers()")
        return media_markers(self.media, *sources)

    def duck(
        self, under: str, *, amount_db: float = -12.0, threshold_db: float = -30.0, attack: Time | None = None,
        release: Time | None = None,
    ) -> AudioTrackBuilder:
        self._mix._require_open("track.duck()")
        if under == self.name:
            raise TraceError(f"audio track {self.name!r} cannot duck under itself; name another track of the mix")
        values: dict[str, Any] = {
            "under": under, "amount_db": amount_db, "threshold_db": threshold_db,
            "attack": optional_positive_duration(attack, "duck attack"), "release": optional_positive_duration(release, "duck release")}
        duck = Duck.model_validate({k: v for k, v in values.items() if v is not None})
        return self._commit(ducks=[*self._state.ducks, (duck, self._mix.called(f"track.duck({under!r})"))])

    def eq(self, *bands: EqBand) -> AudioTrackBuilder:
        if not bands:
            raise TypeError("track.eq() needs at least one EqBand(frequency_hz=..., gain_db=...)")
        for band in bands:
            if not isinstance(band, EqBand):
                raise TypeError(f"track.eq() takes EqBand(...) values, got {type(band).__name__} {band!r}")
        return self._configure("eq", Eq(bands=bands), "track.eq()")

    def compress(
        self, *, threshold_db: float = -18.0, ratio: float = 4.0, attack: Time | None = None,
        release: Time | None = None, makeup_db: float = 0.0,
    ) -> AudioTrackBuilder:
        values: dict[str, Any] = {
            "threshold_db": threshold_db, "ratio": ratio, "makeup_db": makeup_db,
            "attack": optional_positive_duration(attack, "compress attack"),
            "release": optional_positive_duration(release, "compress release")}
        compressor = Compressor.model_validate({k: v for k, v in values.items() if v is not None})
        return self._configure("compressor", compressor, "track.compress()")

    def fade(self, *, fade_in: Time | None = None, fade_out: Time | None = None) -> AudioTrackBuilder:
        if fade_in is None and fade_out is None:
            raise TypeError("track.fade() needs fade_in=, fade_out= or both, for example fade_in=t('500ms')")
        fades = Fades(fade_in=optional_positive_duration(fade_in, "fade_in"), fade_out=optional_positive_duration(fade_out, "fade_out"))
        return self._configure("fades", fades, "track.fade()")

    def reverb(self, *, wet: AnimFloat = 0.3, room_size: float = 0.5, damping: float = 0.5) -> AudioTrackBuilder:
        return self._configure(
            "reverb", Reverb(wet=animated(wet), room_size=room_size, damping=damping), "track.reverb()")

    def denoise(self, *, attenuation_limit_db: float = 60.0) -> AudioTrackBuilder:
        return self._configure("denoise", Denoise(attenuation_limit_db=attenuation_limit_db), "track.denoise()")

    def stems(self, *stems: Stem) -> AudioTrackBuilder:
        if not stems:
            raise TypeError("track.stems() needs at least one stem: 'vocals', 'drums', 'bass' or 'other'")
        return self._configure("stems", tuple(stems), "track.stems()")

    def set(self, **fields: Unpack[AudioTrackFields]) -> AudioTrackBuilder:
        self._mix._require_open("track.set()")
        known = set(AudioTrack.model_fields) - {"name"}
        changed = dict(self._state.fields)
        ducks = list(self._state.ducks)
        for key, value in fields.items():
            if key not in known:
                raise TypeError(
                    f"audio track field {key!r} cannot be set; settable fields: {', '.join(sorted(known))}"
                    + ("; the name is fixed when the track is created" if key == "name" else "")
                )
            if key == "ducks":
                site = self._mix.called("track.set(ducks=...)")
                ducks = [(Duck.model_validate(encode(item)), site) for item in cast("Sequence[Any]", value)]
                if any(duck.under == self.name for duck, _ in ducks):
                    raise TraceError(f"audio track {self.name!r} cannot duck under itself")
            else:
                changed[key] = self._mix._convert(key, value)
        return self._commit(fields=changed, ducks=ducks)

    def _configure(self, key: str, value: Any, what: str) -> AudioTrackBuilder:
        self._mix._require_open(what)
        return self._commit(fields={**self._state.fields, key: value})

    def _commit(
        self, *, fields: dict[str, Any] | None = None, ducks: list[tuple[Duck, str]] | None = None
    ) -> AudioTrackBuilder:
        state = self._state
        candidate = _Track(
            name=state.name, fields=state.fields if fields is None else fields,
            ducks=state.ducks if ducks is None else ducks)
        self._mix._validate(candidate)
        state.fields = candidate.fields
        state.ducks = candidate.ducks
        return self


class AudioMix:
    def __init__(self, ctx: TraceContext) -> None:
        self._ctx = ctx
        self._scope_key = ctx.scope().key
        self._tracks: list[_Track] = []
        self._master: Master | None = None
        self._finalized = False
        self._result: Media | None = None

    @property
    def empty(self) -> bool:
        return not self._tracks

    def track(
        self, media: MediaLike, *, name: str | None = None, gain: AnimFloat = 0.0, pan: AnimFloat = 0.0,
        start: Time | None = None, src: Span | None = None, mute: bool = False,
    ) -> AudioTrackBuilder:
        self._require_open("audio.track()")
        ref = as_media(media)
        index = len(self._tracks)
        state = _Track(name=self._track_name(name, ref, index), fields={"media": ref})
        values = {"gain": gain, "pan": pan, "start": start, "src": src, "mute": mute}
        for key, value in values.items():
            if value is not None:
                state.fields[key] = self._convert(key, value)
        self._validate(state)
        self._tracks.append(state)
        return AudioTrackBuilder(self, index)

    def master(
        self, *, loudness: float | None = None, true_peak: float | None = None, lra: float | None = None,
        limiter: bool = True, gain: float = 0.0,
    ) -> None:
        self._require_open("audio.master()")
        self._master = Master(loudness=loudness, true_peak=true_peak, lra=lra, limiter=limiter, gain=gain)

    def finalize(self, *, duration: Time | None = None) -> Media | None:
        if self._finalized:
            return self._result
        self._require_scope("audio.finalize()")
        length = None if duration is None else ensure_positive_duration(duration, "mix duration")
        self._finalized = True
        if self.empty:
            return None
        self._check_ducks()
        mix: dict[str, Any] = {"tracks": [self._payload(track) for track in self._tracks]}
        if self._master is not None:
            mix["master"] = self._master
        if length is not None:
            mix["duration"] = length
        try:
            model = Mix.model_validate(encode(mix))
        except ValidationError as error:
            problems = "; ".join(str(item["msg"]).removeprefix("Value error, ") for item in error.errors())
            raise TraceError(f"the audio mix is invalid: {problems}") from error
        self._result = cast("Media", self._ctx.record(MIX_OP, {"mix": model}, id_key=MIX_ID_KEY))
        return self._result

    def called(self, what: str) -> str:
        site = self._ctx.call_site()
        return what if site is None else f"{what} at {site.src()}"

    def _payload(self, track: _Track) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": track.name, **track.fields}
        if track.ducks:
            payload["ducks"] = [duck for duck, _ in track.ducks]
        return payload

    def _check_ducks(self) -> None:
        names = [track.name for track in self._tracks]
        for track in self._tracks:
            others = [name for name in names if name != track.name]
            for duck, call in track.ducks:
                if duck.under in others:
                    continue
                raise TraceError(
                    f"{call}: audio track {track.name!r} ducks under {duck.under!r}, which is not another track of "
                    f"the mix; other tracks: {', '.join(repr(name) for name in others) or 'none'}"
                )

    def _validate(self, track: _Track) -> AudioTrack:
        return AudioTrack.model_validate(encode(self._payload(track)))

    def _convert(self, key: str, value: Any) -> Any:
        if key == "media":
            return as_media(value)
        if key in _ANIMATED_FIELDS:
            return animated(value)
        if key == "start":
            return ensure_time(value, f"audio track {key}")
        if key == "src":
            return _span(value, "audio track src")
        return value

    def _require_open(self, what: str) -> None:
        if self._finalized:
            raise TraceError(f"{what}: the audio mix is already finalized; configure the mix inside the entry function")
        self._require_scope(what)

    def _require_scope(self, what: str) -> None:
        if self._ctx.scope().key != self._scope_key:
            raise TraceError(
                f"{what}: an audio mix is static; build its tracks outside eks.when, eks.each and eks.dynamic "
                "bodies (use a Python if or for)"
            )

    def _track_name(self, name: str | None, ref: Media, index: int) -> str:
        taken = {track.name for track in self._tracks}
        if name is not None:
            if not isinstance(name, str) or not _TRACK_NAME.fullmatch(name):
                raise TraceError(
                    f"audio track name {name!r} must match {TRACK_NAME} (letters, digits, '_', '.', '-')")
            if name in taken:
                raise TraceError(f"the mix already has an audio track named {name!r}; pick another name=")
            return name
        base = self._stem_of(ref) or f"track{index}"
        candidate, suffix = base, 2
        while candidate in taken:
            candidate = f"{base[:60]}-{suffix}"
            suffix += 1
        return candidate

    def _stem_of(self, ref: Media) -> str | None:
        if path_of(ref):
            return None
        node = self._ctx.node(node_of(ref))
        return _stem(str(node.args.get("uri", ""))) if node.op == SOURCE_OP else None

__all__ = [
    "DEFAULT_BEAT_STREAMS",
    "MIX_ID_KEY",
    "MIX_OP",
    "TEMPO_STREAM",
    "AudioMix",
    "AudioTrackBuilder",
    "AudioTrackFields",
    "EqBand",
    "fold_ascii",
    "marker_name",
]
