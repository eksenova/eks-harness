from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.score.model import Action, AudioTrack, Rule, Score
from eks_harness.score.timeref import TimeContext, TimeRefError, grid_from_bpm, parse_time_ref

Analyzer = Callable[[Path, tuple[float, float] | None], dict[str, list[float]]]


@dataclass(frozen=True)
class Event:
    time: float
    source: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, fps: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"time": round(self.time, 6), "source": self.source, "name": self.name}
        if fps:
            out["frame"] = int(round(self.time * fps))
        if self.data:
            out["data"] = self.data
        return out


@dataclass(frozen=True)
class Scheduled:
    time: float
    frame: int
    action: Action
    rule: str
    cause: Event | None = None

    @property
    def target(self) -> str:
        return self.action.target

    def as_dict(self) -> dict[str, Any]:
        out = {"time": round(self.time, 6), "frame": self.frame, "target": self.action.target,
               "verb": self.action.verb, "args": self.action.args, "rule": self.rule}
        if self.cause:
            out["cause"] = self.cause.as_dict()
        return out


@dataclass
class Plan:
    score: Score
    context: TimeContext
    events: list[Event]
    actions: list[Scheduled]
    warnings: list[str]
    track_spans: dict[str, tuple[float, float]]

    def for_track(self, track_id: str) -> list[Scheduled]:
        return [a for a in self.actions if a.target == track_id]

    def inputs(self, track_id: str) -> list[dict[str, Any]]:
        start = self.track_spans.get(track_id, (0.0, 0.0))[0]
        out = []
        for item in self.for_track(track_id):
            entry = item.as_dict()
            entry["local"] = round(item.time - start, 6)
            entry["localFrame"] = int(round((item.time - start) * self.context.fps))
            out.append(entry)
        return out

    @property
    def duration(self) -> float:
        return self.context.duration or 0.0

    def as_dict(self) -> dict[str, Any]:
        fps = self.context.fps
        return {
            "fps": fps,
            "duration": self.context.duration,
            "beats": [round(b, 6) for b in self.context.beats],
            "downbeats": [round(b, 6) for b in self.context.downbeats],
            "markers": self.context.markers,
            "tracks": {k: {"start": round(v[0], 6), "end": round(v[1], 6)} for k, v in self.track_spans.items()},
            "events": [e.as_dict(fps) for e in self.events],
            "actions": [a.as_dict() for a in self.actions],
            "warnings": self.warnings,
        }


def build_context(score: Score, base: Path | None = None, analyzer: Analyzer | None = None) -> TimeContext:
    clock = score.clock
    tempo = clock.tempo
    beats = list(tempo.beats or [])
    downbeats = list(tempo.downbeats or [])
    duration = clock.duration
    if clock.window and duration is None:
        duration = clock.window[1] - clock.window[0]
    audio = next((t for t in score.tracks if isinstance(t, AudioTrack) and t.enabled), None)
    if not beats and tempo.bpm is None and audio is not None and audio.analyze and analyzer is not None:
        path = Path(audio.path)
        if base is not None and not path.is_absolute():
            path = base / path
        window = audio.window or clock.window
        found = analyzer(path, window)
        beats = list(found.get("beats") or [])
        downbeats = list(found.get("downbeats") or [])
        if duration is None and found.get("duration"):
            duration = float(found["duration"][0])
    if not beats and tempo.bpm is not None:
        span = duration if duration is not None else 600.0
        beats, downbeats = grid_from_bpm(tempo.bpm, tempo.offset, span, tempo.beats_per_bar)
    if beats and not downbeats:
        downbeats = beats[:: max(tempo.beats_per_bar, 1)]
    return TimeContext(fps=clock.fps, beats=beats, downbeats=downbeats, markers=dict(clock.markers),
                       duration=duration)


def clock_events(context: TimeContext) -> list[Event]:
    events = [Event(t, "score", "beat", {"index": i}) for i, t in enumerate(context.beats)]
    events += [Event(t, "score", "downbeat", {"index": i}) for i, t in enumerate(context.downbeats)]
    events += [Event(t, "score", "cue", {"name": name}) for name, t in sorted(context.markers.items(),
                                                                             key=lambda kv: kv[1])]
    return events


def _matches(rule: Rule, event: Event) -> bool:
    when = rule.when
    if when.event != event.name:
        return False
    if when.source and when.source != event.source:
        return False
    return all(event.data.get(k) == v for k, v in when.where.items())


def plan(score: Score, *, observed: Iterable[Event] = (), base: Path | None = None,
         analyzer: Analyzer | None = None, context: TimeContext | None = None) -> Plan:
    context = context or build_context(score, base, analyzer)
    warnings: list[str] = []
    spans: dict[str, tuple[float, float]] = {}
    for track in score.tracks:
        try:
            start = context.resolve(track.start)
            end = context.resolve(track.end) if track.end else (context.duration or start)
        except TimeRefError as error:
            warnings.append(f"{track.id}: {error}")
            start, end = 0.0, context.duration or 0.0
        spans[track.id] = (start, max(start, end))
    events = sorted([*clock_events(context), *observed], key=lambda e: (e.time, e.source, e.name))
    actions: list[Scheduled] = []
    for index, rule in enumerate(score.rules):
        name = rule.id or f"rule{index + 1}"
        times: list[tuple[float, Event | None]] = []
        if rule.when.at is not None:
            try:
                times.append((context.resolve(parse_time_ref(rule.when.at)) + rule.when.offset, None))
            except TimeRefError as error:
                warnings.append(f"{name}: {error}")
                continue
        else:
            for event in events:
                if _matches(rule, event):
                    times.append((event.time + rule.when.offset, event))
                    if rule.when.once:
                        break
        for seconds, cause in times:
            if seconds < 0 or (context.duration is not None and seconds > context.duration + 1e-9):
                warnings.append(f"{name}: fires at {seconds:.3f}s, outside the score")
                continue
            for action in rule.do:
                actions.append(Scheduled(time=context.snap(seconds), frame=context.frame(seconds), action=action,
                                         rule=name, cause=cause))
    actions.sort(key=lambda a: (a.time, a.target, a.rule))
    return Plan(score=score, context=context, events=events, actions=actions, warnings=warnings, track_spans=spans)
