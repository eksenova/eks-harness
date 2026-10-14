from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from fractions import Fraction

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.plan import Clock, Node, Plan
from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    Time,
    TimeContext,
    TimeError,
    Word,
    parse_time,
)

MARKERS_OP = "media.markers"
BEAT_STREAM = "beat"
DOWNBEAT_STREAM = "downbeat"
WORDS_KEY = "words"

ValueOf = Callable[[str], JsonValue]


def _ticks(value: JsonValue, where: str) -> int:
    if isinstance(value, str):
        time = parse_time(value)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        return round(Fraction(repr(float(value))) * TICKS_PER_SECOND)
    else:
        raise NodeFailure("time_unresolved", f"{where}: {value!r} is not a time")
    if not time.is_concrete:
        raise NodeFailure("time_unresolved", f"{where}: {value!r} is not a concrete time")
    return time.ticks


def stream_name(source: str, stream: str) -> str:
    return source if stream == source else f"{source}.{stream}"


def marker_streams(output: JsonValue, where: str) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[Word, ...]]]:
    markers: dict[str, tuple[int, ...]] = {}
    words: dict[str, tuple[Word, ...]] = {}
    if not isinstance(output, Mapping):
        raise NodeFailure("time_unresolved", f"{where}: the markers output is not an object")
    for source, streams in output.items():
        if not isinstance(streams, Mapping):
            continue
        for stream, values in streams.items():
            if stream == WORDS_KEY and isinstance(values, list):
                found: list[Word] = []
                for item in values:
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                        found.append(Word(text=str(item["text"]),
                                          ticks=_ticks(item.get("start"), f"{where} {source} word")))
                words[source] = tuple(found)
            elif isinstance(values, list):
                markers[stream_name(source, stream)] = tuple(
                    sorted(_ticks(value, f"{where} {source}.{stream}") for value in values))
    return markers, words


def _tempo_grid(output: JsonValue, where: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    beats: tuple[int, ...] = ()
    downbeats: tuple[int, ...] = ()
    if isinstance(output, Mapping):
        for streams in output.values():
            if not isinstance(streams, Mapping):
                continue
            found = streams.get(BEAT_STREAM)
            if isinstance(found, list) and not beats:
                beats = tuple(sorted(_ticks(value, f"{where} beat") for value in found))
            found = streams.get(DOWNBEAT_STREAM)
            if isinstance(found, list) and not downbeats:
                downbeats = tuple(sorted(_ticks(value, f"{where} downbeat") for value in found))
    if len(beats) < 2:
        raise NodeFailure("time_unresolved", f"{where}: the tempo source found fewer than 2 beats")
    return beats, downbeats


def run_time_context(plan: Plan, clock: Clock, providers: Sequence[str], value_of: ValueOf, *,
                     fps: Fraction | None = None, end: int | None = None) -> TimeContext:
    where = "clock"
    tempo = clock.tempo
    rate = fps if fps is not None else clock.fps
    try:
        if tempo is not None and tempo.source and tempo.source not in providers:
            context = TimeContext(fps=rate, meter=tempo.meter, meters=tempo.meters, end=end)
        elif tempo is not None and tempo.source:
            base = TimeContext(fps=rate, meter=tempo.meter, meters=tempo.meters, end=end)
            beats, downbeats = _tempo_grid(value_of(tempo.source), f"tempo source {tempo.source}")
            context = replace(base, beats=beats)
            context = replace(context, downbeats=downbeats or context._bar_starts)
        else:
            context = TimeContext.static(fps=rate, tempo=tempo, end=end)
        markers: dict[str, tuple[int, ...]] = {}
        words: dict[str, tuple[Word, ...]] = {}
        nodes = {node.id: node for node, _ancestors in plan.walk()}
        for provider in providers:
            node = nodes.get(provider)
            if node is None or node.op != MARKERS_OP:
                continue
            found, spoken = marker_streams(value_of(provider), f"{where} provider {provider}")
            markers.update(found)
            words.update(spoken)
        context = replace(context, markers=markers, words=words,
                          default_words=next(iter(words)) if len(words) == 1 else None)
        if end is None and clock.duration is not None:
            context = replace(context, end=context.resolve(clock.duration))
        cues: dict[str, int] = {}
        for name, time in clock.cues.items():
            cues[name] = context.resolve(time)
        return replace(context, cues=cues)
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"the clock cannot be built: {error}") from error
    except KeyError as error:
        raise NodeFailure("time_unresolved", f"a clock provider has no value yet: {error}") from error


def resolve(context: TimeContext, time: Time, where: str) -> int:
    try:
        return context.resolve(time)
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"{where}: {error}") from error


def node_providers(dependencies: Mapping[str, Sequence[str]], node: Node) -> tuple[str, ...]:
    return tuple(dependencies.get(node.id, ()))


__all__ = ["marker_streams", "node_providers", "resolve", "run_time_context", "stream_name"]
