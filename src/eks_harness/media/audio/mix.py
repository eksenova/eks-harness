from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.ops.audio import AudioTrack, Mix
from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    Time,
    TimeContext,
    TimeError,
    format_time,
)
from eks_harness.media.animate import AnimateError, Timebase, floats
from eks_harness.media.audio.dsp import (
    EQ_BLOCK_SECONDS,
    compress,
    db_gain,
    duck_gain,
    fade,
    filter_bands,
    pan,
)
from eks_harness.media.audio.io import decode
from eks_harness.media.audio.limiter import limit
from eks_harness.media.audio.loudness import (
    Signal,
    integrated_loudness,
    to_db,
    true_peak,
)
from eks_harness.media.audio.reverb import freeverb, tail_samples

DEFAULT_CEILING_DB = 0.0

Prepare = Callable[[AudioTrack, Path], Path]
Check = Callable[[], None]


@dataclass(frozen=True)
class MixResult:
    samples: Signal
    rate: int
    loudness: float
    true_peak: float


@dataclass
class _Track:
    track: AudioTrack
    start: int
    start_ticks: int
    data: Signal
    timebase: Timebase


def _samples(ticks: int, rate: int) -> int:
    return round(ticks * rate / TICKS_PER_SECOND)


def _ticks(read: Callable[[], int], where: str) -> int:
    try:
        return read()
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"{where}: {error}") from error


def _animated(value: Any, base: Timebase, seconds: Signal, where: str) -> Signal:
    try:
        return floats(value, base, seconds)
    except (AnimateError, TimeError) as error:
        raise NodeFailure("args_invalid", f"{where}: {error}") from error


def _length(base: Timebase, duration: Time, rate: int, where: str) -> int:
    return _samples(_ticks(lambda: base.local_ticks(duration), where), rate)


def _seconds(base: Timebase, duration: Time, where: str) -> float:
    return _ticks(lambda: base.local_ticks(duration), where) / TICKS_PER_SECOND


def _load(track: AudioTrack, path: Path, *, mix: Mix, context: TimeContext, seed: int, directory: Path,
          prepare: Prepare | None) -> _Track:
    rate = mix.sample_rate
    where = f"audio track {track.name!r}"
    if (track.stems or track.denoise is not None) and prepare is None:
        raise NodeFailure("resolution_unavailable", f"{where} needs stems or denoise, and this renderer has no "
                                                    "inference backend")
    source = prepare(track, path) if prepare is not None else path
    data = decode(source, rate=rate, channels=mix.channels, directory=directory, what=where)
    media_ticks = round(data.shape[0] * TICKS_PER_SECOND / rate)
    if track.src is not None:
        span = track.src
        media = Timebase(context, origin=0, length=media_ticks)
        first = _samples(_ticks(lambda: media.local_ticks(span.start), f"{where} src start"), rate)
        last = _samples(_ticks(lambda: media.local_ticks(span.end), f"{where} src end"), rate)
        if first < 0 or first >= data.shape[0] or last <= first:
            raise NodeFailure("args_invalid",
                              f"{where}: src {format_time(span.start)} to {format_time(span.end)} selects nothing of "
                              f"its media, which is {data.shape[0] / rate:.3f}s long; give a span inside the media",
                              detail={"track": track.name})
        data = data[first:min(last, data.shape[0])]
    start_ticks = _ticks(lambda: context.resolve(track.start), f"{where} start")
    if start_ticks < 0:
        raise NodeFailure("args_invalid", f"{where} starts at {format_time(track.start)}, before the mix starts")
    base = Timebase(context, origin=start_ticks, length=round(data.shape[0] * TICKS_PER_SECOND / rate),
                    seed=derived_seed(seed, f"track:{track.name}"), path=f"track:{track.name}")
    return _Track(track=track, start=_samples(start_ticks, rate), start_ticks=start_ticks, data=data, timebase=base)


def _process(item: _Track, *, rate: int, end: int) -> Signal:
    track, base, data = item.track, item.timebase, item.data
    where = f"audio track {track.name!r}"
    count = data.shape[0]
    if track.eq is not None:
        block = max(1, round(EQ_BLOCK_SECONDS * rate))
        blocks = np.arange(-(-count // block), dtype=np.float64) * block / rate
        for index, band in enumerate(track.eq.bands):
            if band.frequency_hz >= rate / 2:
                raise NodeFailure("args_invalid", f"{where} eq band {index} is at {band.frequency_hz:g} Hz, at or above "
                                                  f"the Nyquist frequency of the {rate} Hz mix; lower it or raise "
                                                  "the mix sample rate")
            gains = _animated(band.gain_db, base.child(f"eq/{index}"), blocks, f"{where} eq band {index} gain_db")
            data = filter_bands(data, rate, band.type, band.frequency_hz, band.q, gains, block)
    if track.compressor is not None:
        comp = track.compressor
        data = compress(data, rate, threshold_db=comp.threshold_db, ratio=comp.ratio,
                        attack=_seconds(base, comp.attack, f"{where} compressor attack"),
                        release=_seconds(base, comp.release, f"{where} compressor release"),
                        makeup_db=comp.makeup_db)
    if track.fades is not None:
        fades = track.fades
        fade_in = _length(base, fades.fade_in, rate, f"{where} fade_in") if fades.fade_in is not None else 0
        fade_out = _length(base, fades.fade_out, rate, f"{where} fade_out") if fades.fade_out is not None else 0
        data = data * fade(count, fade_in, fade_out)[:, None]
    if track.reverb is not None:
        verb = track.reverb
        tail = max(0, min(tail_samples(rate, verb.room_size), end - item.start - count))
        dry = np.concatenate([data, np.zeros((tail, data.shape[1]))])
        wet = freeverb(dry, rate, room_size=verb.room_size, damping=verb.damping,
                       seed=derived_seed(base.seed, "reverb"))
        amount = np.clip(_animated(verb.wet, base.child("reverb"), np.arange(dry.shape[0], dtype=np.float64) / rate,
                                   f"{where} reverb wet"), 0.0, 1.0)[:, None]
        data = (1.0 - amount) * dry + amount * wet
    seconds = np.arange(data.shape[0], dtype=np.float64) / rate
    data = data * db_gain(_animated(track.gain, base, seconds, f"{where} gain"))[:, None]
    if data.shape[1] == 2:
        data = pan(data, _animated(track.pan, base.child("pan"), seconds, f"{where} pan"))
    if track.mute:
        data = np.zeros_like(data)
    return data


def _order(tracks: Sequence[AudioTrack]) -> list[int]:
    index = {track.name: position for position, track in enumerate(tracks)}
    state: dict[int, int] = {}
    order: list[int] = []

    def visit(position: int, path: tuple[str, ...]) -> None:
        mark = state.get(position)
        if mark == 2:
            return
        name = tracks[position].name
        if mark == 1:
            cycle = " -> ".join([*path[path.index(name):], name])
            raise NodeFailure("args_invalid", f"audio tracks duck under each other in a cycle ({cycle}); remove one "
                                              "of the ducks")
        state[position] = 1
        for duck in tracks[position].ducks:
            visit(index[duck.under], (*path, name))
        state[position] = 2
        order.append(position)

    for position in range(len(tracks)):
        visit(position, ())
    return order


def render_mix(mix: Mix, files: Sequence[Path], *, context: TimeContext, seed: int, directory: Path,
               prepare: Prepare | None = None, check: Check | None = None) -> MixResult:
    rate = mix.sample_rate
    if mix.master.lra is not None:
        raise NodeFailure("args_invalid", "master.lra is not supported by the audio.mix renderer: it normalises "
                                          "integrated loudness and limits true peak only; remove lra from the master")
    if len(files) != len(mix.tracks):
        raise ValueError(f"the mix has {len(mix.tracks)} tracks and {len(files)} files")
    loaded: list[_Track] = []
    for track, path in zip(mix.tracks, files, strict=True):
        if check is not None:
            check()
        loaded.append(_load(track, path, mix=mix, context=context, seed=seed, directory=directory, prepare=prepare))
    if mix.duration is not None:
        total = _length(Timebase(context), mix.duration, rate, "mix duration")
    else:
        total = max((item.start + item.data.shape[0] for item in loaded), default=0)
    if total <= 0:
        raise NodeFailure("args_invalid", "the mix is empty: it has no tracks with audio and no duration; add a track "
                                          "or set duration")
    placed: list[Signal] = []
    for item in loaded:
        if check is not None:
            check()
        processed = _process(item, rate=rate, end=total)
        buffer = np.zeros((total, mix.channels), dtype=np.float64)
        stop = min(total, item.start + processed.shape[0])
        if stop > item.start:
            buffer[item.start:stop] = processed[: stop - item.start]
        placed.append(buffer)
    names = {track.name: position for position, track in enumerate(mix.tracks)}
    for position in _order(mix.tracks):
        track = mix.tracks[position]
        base = loaded[position].timebase
        for duck in track.ducks:
            where = f"audio track {track.name!r} duck under {duck.under!r}"
            gain = duck_gain(placed[names[duck.under]], rate, amount_db=duck.amount_db,
                             threshold_db=duck.threshold_db, attack=_seconds(base, duck.attack, f"{where} attack"),
                             release=_seconds(base, duck.release, f"{where} release"))
            placed[position] = placed[position] * gain[:, None]
    master = mix.master
    out = np.sum(placed, axis=0) if placed else np.zeros((total, mix.channels), dtype=np.float64)
    out = out * float(db_gain(master.gain))
    if master.loudness is not None:
        measured = integrated_loudness(out, rate)
        if math.isfinite(measured):
            out = out * float(db_gain(master.loudness - measured))
    if master.limiter:
        out = limit(out, rate, master.true_peak if master.true_peak is not None else DEFAULT_CEILING_DB)
    elif master.true_peak is not None:
        ceiling = float(db_gain(master.true_peak))
        peak = true_peak(out)
        if peak > ceiling:
            out = out * (ceiling / peak)
    return MixResult(samples=out, rate=rate, loudness=integrated_loudness(out, rate), true_peak=to_db(true_peak(out)))


__all__ = ["MixResult", "Prepare", "render_mix"]
