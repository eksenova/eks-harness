from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from eks_harness.score.model import DeviceTrack
from eks_harness.score.schedule import Event, Plan

log = logging.getLogger("eks_harness.score.takes")

FAST_PACE = {"moveMs": 0, "dwellMs": 0, "typeMsPerChar": 0, "holdMs": 0, "screenHoldMs": 0, "mobilePressMs": 0}


class TakeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimeWarp:
    anchors: tuple[tuple[float, float], ...]

    @classmethod
    def from_anchors(cls, anchors: list[tuple[float, float]]) -> TimeWarp:
        cleaned: list[tuple[float, float]] = []
        for score_t, video_t in sorted(anchors):
            if cleaned and (score_t <= cleaned[-1][0] or video_t <= cleaned[-1][1]):
                continue
            cleaned.append((score_t, video_t))
        return cls(tuple(cleaned))

    def video_at(self, score_t: float) -> float:
        return self._map(score_t, 0, 1)

    def score_at(self, video_t: float) -> float:
        return self._map(video_t, 1, 0)

    def _map(self, value: float, a: int, b: int) -> float:
        if not self.anchors:
            return value
        xs = [p[a] for p in self.anchors]
        ys = [p[b] for p in self.anchors]
        if value <= xs[0]:
            return ys[0] + (value - xs[0])
        if value >= xs[-1]:
            return ys[-1] + (value - xs[-1])
        i = bisect.bisect_right(xs, value) - 1
        span = xs[i + 1] - xs[i]
        return ys[i] + (value - xs[i]) * (ys[i + 1] - ys[i]) / span


def retime(source: Path, output: Path, *, fps: float, duration: float, warp: TimeWarp,
           size: tuple[int, int] | None = None) -> Path:
    import av

    frames_out = max(1, round(duration * fps))
    targets = [warp.video_at(i / fps) for i in range(frames_out)]
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".part.mp4")
    with av.open(str(source)) as reader, av.open(str(tmp), "w") as writer:
        stream_in = reader.streams.video[0]
        width, height = size or (stream_in.codec_context.width, stream_in.codec_context.height)
        stream_out = writer.add_stream("libx264", rate=round(fps) if float(fps).is_integer() else fps)
        stream_out.width, stream_out.height = width - width % 2, height - height % 2
        stream_out.pix_fmt = "yuv420p"
        stream_out.options = {"crf": "16", "preset": "medium"}
        current = None
        current_t = float("-inf")
        pending = None
        decoder = reader.decode(stream_in)
        index = 0

        rate = Fraction(fps).limit_denominator(1001)
        stream_out.codec_context.time_base = 1 / rate

        def emit(frame_image: Any, out_index: int) -> None:
            frame = frame_image.reformat(width=stream_out.width, height=stream_out.height, format="yuv420p")
            frame.pts = out_index
            frame.time_base = 1 / rate
            for packet in stream_out.encode(frame):
                writer.mux(packet)

        for raw in decoder:
            t = float(raw.pts * stream_in.time_base) if raw.pts is not None else current_t + 1 / fps
            while index < frames_out and targets[index] < t:
                closer_to_previous = current is not None and (targets[index] - current_t) <= (t - targets[index])
                emit(current if closer_to_previous else raw, index)
                index += 1
            current, current_t = raw, t
            pending = raw
            if index >= frames_out:
                break
        while index < frames_out and pending is not None:
            emit(pending, index)
            index += 1
        for packet in stream_out.encode():
            writer.mux(packet)
    tmp.replace(output)
    return output


@dataclass
class FiredAction:
    planned: float
    wall_before: float
    wall_after: float
    verb: str
    rule: str
    ok: bool
    error: str | None = None


@dataclass
class TakeLog:
    clock_zero: float
    recording_started: float
    fired: list[FiredAction] = field(default_factory=list)
    observed: list[tuple[float, dict[str, Any]]] = field(default_factory=list)

    def anchors(self, span_start: float) -> list[tuple[float, float]]:
        return [(a.planned - span_start, a.wall_after - self.recording_started) for a in self.fired if a.ok]


def drive(session: Any, actions: list[Any], *, span_start: float, clock_zero: float, recording_started: float,
          events: Callable[[], Any] | None = None, tail: float = 0.0,
          sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time) -> TakeLog:
    take = TakeLog(clock_zero=clock_zero, recording_started=recording_started)
    stop = threading.Event()
    reader = None
    if events is not None:
        def listen() -> None:
            try:
                for event in events():
                    if stop.is_set():
                        return
                    take.observed.append((now(), event))
            except Exception as error:
                log.debug("event stream ended: %s", error)

        reader = threading.Thread(target=listen, daemon=True)
        reader.start()
    try:
        for item in actions:
            due = clock_zero + (item.time - span_start)
            wait = due - now()
            if wait > 0:
                sleep(wait)
            before = now()
            try:
                session.act(item.action.verb, **dict(item.action.args))
                fired = FiredAction(item.time, before, now(), item.action.verb, item.rule, True)
            except Exception as error:
                fired = FiredAction(item.time, before, now(), item.action.verb, item.rule, False, str(error)[:300])
            take.fired.append(fired)
        if tail > 0:
            sleep(tail)
    finally:
        stop.set()
    return take


def observed_events(take: TakeLog, warp: TimeWarp, *, track_id: str, span_start: float) -> list[Event]:
    out = []
    for wall, payload in take.observed:
        name = payload.get("name") or payload.get("type")
        if not name or name in ("hello", "heartbeat"):
            continue
        video_t = wall - take.recording_started
        out.append(Event(time=span_start + warp.score_at(video_t), source=track_id, name=str(name),
                         data=dict(payload.get("data") or {})))
    return out


def device_take_runner(track: DeviceTrack, current: Plan, workspace: Path, base: Path | None = None) -> Any:
    from eks_harness.cli.client import client_from_args
    from eks_harness.config import load as load_config
    from eks_harness.drivers.sessions import WorkerSession
    from eks_harness.flows.runner import FlowRequest, load_module, prepare_app
    from eks_harness.paths import resolve_paths
    from eks_harness.score.render import Take

    base = base or workspace.parent
    paths = resolve_paths()
    config = load_config(paths)
    start, end = current.track_spans[track.id]
    duration = end - start
    flow = (base / track.flow).resolve() if track.flow else None
    client = client_from_args(type("Args", (), {"url": None, "api_key": None})())
    request = FlowRequest(flow=flow, platform=track.platform, cwd=base, params=dict(track.options.get("params") or {}),
                          release=False)
    prepared = prepare_app(request, client=client, paths=paths, config=config)
    app = prepared.app
    session = WorkerSession(prepared.handle.client(), platform=prepared.platform, sid=prepared.sid)
    if flow is not None:
        module = load_module(flow, f"ehx_take_{flow.stem.replace('-', '_')}")
        setup = getattr(module, "setup", None) or getattr(module, "flow", None)
        if setup is not None:
            setup(app)
    actions = current.for_track(track.id)
    name = f"take-{track.id}"
    started = app.start_recording(name, pace=FAST_PACE, from_here=True, no_pointer=True, pointer=False)
    recording_started = time.time()
    if isinstance(started, dict) and started.get("startedAt"):
        recording_started = float(started["startedAt"]) / (1000.0 if started["startedAt"] > 1e11 else 1.0)
    clock_zero = time.time() + float(track.lead_in)
    take = drive(session, actions, span_start=start, clock_zero=clock_zero, recording_started=recording_started,
                 events=lambda: session.events(timeout=duration + 60), tail=max(0.0, (start + duration)
                                                                                 - (actions[-1].time if actions else start)))
    artifact = app.stop_recording(name, trim=False)
    if artifact is None or not artifact.raw:
        raise TakeError(f"{track.id}: the take produced no recording")
    raw = workspace / "takes" / f"{track.id}-raw.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    client.download(artifact.raw, raw)
    lead = clock_zero - recording_started
    anchors = take.anchors(start) or []
    warp = TimeWarp.from_anchors([(0.0, lead), *anchors] if track.warp else [(0.0, lead)])
    footage = retime(raw, workspace / "takes" / f"{track.id}.mp4", fps=current.context.fps, duration=duration,
                     warp=warp, size=tuple(track.size) if track.size else None)
    events = observed_events(take, warp, track_id=track.id, span_start=start)
    (workspace / "takes" / f"{track.id}.json").write_text(json.dumps({
        "fired": [vars(a) for a in take.fired], "anchors": list(warp.anchors),
        "events": [e.as_dict(current.context.fps) for e in events], "artifact": artifact.id}, indent=2),
        encoding="utf-8")
    failed = [a for a in take.fired if not a.ok]
    if failed:
        log.warning("%s: %d actions failed during the take: %s", track.id, len(failed),
                    "; ".join(f"{a.verb} at {a.planned:.2f}s: {a.error}" for a in failed[:5]))
    return Take(footage=footage, events=events)
