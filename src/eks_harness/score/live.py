from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.ids import new_sid
from eks_harness.score.model import Score
from eks_harness.score.schedule import Event, Plan, Scheduled, plan

log = logging.getLogger("eks_harness.score.live")

TICK_SECONDS = 0.1
Broadcast = Callable[[dict[str, Any]], None]


@dataclass
class LiveSession:
    id: str
    score: Score
    base: Path
    plan: Plan
    devices: dict[str, Any] = field(default_factory=dict)
    state: str = "stopped"
    position: float = 0.0
    clock_zero: float = 0.0
    rate: float = 1.0
    fired: set[tuple[str, int, str]] = field(default_factory=set)
    dynamic: list[Scheduled] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    listeners: list[Broadcast] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread: threading.Thread | None = None
    stop_flag: threading.Event = field(default_factory=threading.Event)
    loops: bool = False
    previews: dict[str, dict[str, Any]] = field(default_factory=dict)

    def now(self) -> float:
        if self.state != "playing":
            return self.position
        return (time.time() - self.clock_zero) * self.rate

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.score.name, "state": self.state, "position": round(self.now(), 4),
                "duration": self.plan.duration, "fps": self.plan.context.fps, "loop": self.loops,
                "previews": {k: {kk: vv for kk, vv in v.items() if kk != "path"} for k, v in self.previews.items()},
                "audio": self.audio_info(),
                "devices": {k: getattr(v, "sid", None) for k, v in self.devices.items()},
                "plan": self.plan.as_dict()}

    def audio_file(self) -> Path | None:
        track = next((t for t in self.score.tracks if t.kind == "audio" and t.enabled), None)
        if track is None:
            return None
        path = Path(track.path).expanduser()
        path = path if path.is_absolute() else (self.base / path).resolve()
        return path if path.is_file() else None

    def audio_info(self) -> dict[str, Any] | None:
        track = next((t for t in self.score.tracks if t.kind == "audio" and t.enabled), None)
        if track is None or self.audio_file() is None:
            return None
        window = track.window or self.score.clock.window
        return {"track": track.id, "url": f"/api/scores/live/{self.id}/audio", "offset": window[0] if window else 0.0,
                "gainDb": track.gain_db}

    def broadcast(self, message: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                listener(message)
            except Exception as error:
                log.debug("live listener failed: %s", error)

    def play(self, start: float | None = None) -> None:
        with self.lock:
            if start is not None:
                self.position = max(0.0, float(start))
            self.fired = {key for key in self.fired if key[1] < round(self.position * self.plan.context.fps)}
            self.clock_zero = time.time() - self.position / self.rate
            self.state = "playing"
            if self.thread is None or not self.thread.is_alive():
                self.stop_flag.clear()
                self.thread = threading.Thread(target=self._loop, name=f"score-live-{self.id}", daemon=True)
                self.thread.start()
        self.broadcast({"type": "state", **self._state()})

    def pause(self) -> None:
        with self.lock:
            self.position = self.now()
            self.state = "paused"
        self.broadcast({"type": "state", **self._state()})

    def seek(self, position: float) -> None:
        with self.lock:
            playing = self.state == "playing"
            self.position = max(0.0, float(position))
            self.fired = {key for key in self.fired if key[1] < round(self.position * self.plan.context.fps)}
            self.dynamic = [a for a in self.dynamic if a.time < self.position]
            if playing:
                self.clock_zero = time.time() - self.position / self.rate
        self.broadcast({"type": "seek", "position": self.position})
        self.broadcast({"type": "state", **self._state()})

    def stop(self) -> None:
        self.stop_flag.set()
        with self.lock:
            self.state = "stopped"
            self.position = 0.0
            self.fired.clear()
            self.dynamic.clear()
        self.broadcast({"type": "state", **self._state()})

    def _state(self) -> dict[str, Any]:
        return {"state": self.state, "position": round(self.now(), 4), "at": time.time()}

    def observe(self, source: str, name: str, data: dict[str, Any] | None = None, at: float | None = None) -> Event:
        event = Event(time=self.now() if at is None else at, source=source, name=name, data=dict(data or {}))
        with self.lock:
            self.events.append(event)
            for index, rule in enumerate(self.score.rules):
                when = rule.when
                if when.event != name or (when.source and when.source != source):
                    continue
                if any(event.data.get(k) != v for k, v in when.where.items()):
                    continue
                rule_id = rule.id or f"rule{index + 1}"
                if when.once and any(a.rule == rule_id for a in self.dynamic):
                    continue
                due = event.time + when.offset
                for action in rule.do:
                    self.dynamic.append(Scheduled(time=due, frame=round(due * self.plan.context.fps), action=action,
                                                  rule=rule_id, cause=event))
        self.broadcast({"type": "event", **event.as_dict(self.plan.context.fps)})
        return event

    def due(self, until: float) -> list[Scheduled]:
        out = []
        with self.lock:
            for item in [*self.plan.actions, *self.dynamic]:
                key = (item.rule, item.frame, item.target)
                if key in self.fired or item.time > until:
                    continue
                if item.time < self.position - 0.25 and item not in self.dynamic:
                    self.fired.add(key)
                    continue
                self.fired.add(key)
                out.append(item)
        return sorted(out, key=lambda a: a.time)

    def _loop(self) -> None:
        while not self.stop_flag.is_set():
            if self.state != "playing":
                time.sleep(TICK_SECONDS)
                continue
            now = self.now()
            for item in self.due(now):
                self.fire(item)
            duration = self.plan.duration
            if duration and now >= duration:
                if self.loops:
                    self.seek(0.0)
                    self.play(0.0)
                else:
                    with self.lock:
                        self.position = duration
                        self.state = "paused"
                    self.broadcast({"type": "state", **self._state()})
            self.broadcast({"type": "tick", "position": round(now, 4)})
            time.sleep(TICK_SECONDS)

    def fire(self, item: Scheduled) -> None:
        target = item.target
        message = {"type": "fire", **item.as_dict()}
        track = next((t for t in self.score.tracks if t.id == target), None)
        if track is not None and track.kind == "device" and target in self.devices:
            session = self.devices[target]

            def run() -> None:
                try:
                    session.act(item.action.verb, **dict(item.action.args))
                    self.broadcast({**message, "ok": True})
                except Exception as error:
                    self.broadcast({**message, "ok": False, "error": str(error)[:300]})

            threading.Thread(target=run, daemon=True).start()
            return
        self.broadcast({**message, "type": "input", "time": item.time, "verb": item.action.verb,
                        "name": item.action.args.get("name"), "data": item.action.args.get("data"),
                        "prop": item.action.args.get("prop"), "value": item.action.args.get("value")})


PREVIEW_SCALE = 4


def to_webm(source: Path, target: Path) -> Path:
    import subprocess

    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    tmp = target.with_suffix(".part.webm")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-b:v", "0", "-crf", "36", "-deadline", "realtime", "-cpu-used", "8", "-an", str(tmp)],
                   check=True, timeout=1800)
    tmp.replace(target)
    return target


def render_previews(session: LiveSession, workspace: Path) -> None:
    from eks_harness.score.render import _render_context, dependency_order, render_scene_track

    full = dependency_order(session.score)
    blender = [t for t in full if t.kind == "blender"]
    if not blender:
        return
    needed = {src for t in blender for src in (t.textures or {}).values()}
    order = [t for t in full if t.kind == "blender" or (t.kind == "web" and t.id in needed)]
    rctx = _render_context(session.score, session.plan, workspace, workspace / "preview.mp4")
    clips: dict[str, Path] = {}
    for track in order:
        if session.stop_flag.is_set():
            return
        width, height = track.size
        textures = {name: source for name, source in (track.textures or {}).items() if source in clips}
        skipped = sorted(set(track.textures or {}) - set(textures))
        update: dict[str, Any] = {"size": (max(64, width // PREVIEW_SCALE) // 2 * 2,
                                           max(64, height // PREVIEW_SCALE) // 2 * 2), "textures": textures}
        if track.kind == "blender":
            update.update({"engine": track.engine if track.engine != "CYCLES" else "BLENDER_EEVEE_NEXT", "samples": 4})
        small = track.model_copy(update=update)
        if track.kind == "web":
            try:
                clips[track.id], _ = render_scene_track(small, session.plan, clips, session.base, rctx)
            except Exception as error:
                log.warning("web texture %s for previews failed: %s", track.id, error)
            continue
        session.previews[track.id] = {"state": "rendering"}
        session.broadcast({"type": "preview", "track": track.id, "state": "rendering"})
        try:
            clip, _ = render_scene_track(small, session.plan, clips, session.base, rctx)
            clips[track.id] = clip
            web = to_webm(clip, workspace / f"{session.id}-{track.id}.webm")
        except Exception as error:
            session.previews[track.id] = {"state": "failed", "error": str(error)[:500]}
            session.broadcast({"type": "preview", "track": track.id, "state": "failed", "error": str(error)[:500]})
            continue
        url = f"/api/scores/live/{session.id}/preview/{track.id}"
        session.previews[track.id] = {"state": "ready", "path": str(web), "url": url,
                                      **({"withoutTextures": skipped} if skipped else {})}
        session.broadcast({"type": "preview", "track": track.id, "state": "ready", "url": url,
                           **({"withoutTextures": skipped} if skipped else {})})


class LiveRegistry:
    def __init__(self) -> None:
        self.sessions: dict[str, LiveSession] = {}
        self.lock = threading.Lock()

    def open(self, score: Score, base: Path, *, analyzer: Any = None, devices: dict[str, Any] | None = None,
             loop: bool = False, previews: Path | None = None) -> LiveSession:
        current = plan(score, base=base, analyzer=analyzer)
        session = LiveSession(id=new_sid(), score=score, base=base, plan=current, devices=dict(devices or {}),
                              loops=loop)
        for track_id, device in session.devices.items():
            events = getattr(device, "events", None)
            if callable(events):
                threading.Thread(target=_pump, args=(session, track_id, device), daemon=True).start()
        with self.lock:
            self.sessions[session.id] = session
        if previews is not None:
            threading.Thread(target=render_previews, args=(session, previews), daemon=True,
                             name=f"score-previews-{session.id}").start()
        return session

    def get(self, session_id: str) -> LiveSession | None:
        return self.sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session.stop()
        return True

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.snapshot().items() if k != "plan"} for s in self.sessions.values()]


def _pump(session: LiveSession, track_id: str, device: Any) -> None:
    while not session.stop_flag.is_set() and session.id:
        try:
            for payload in device.events(timeout=None):
                name = payload.get("name") or payload.get("type")
                if name and name not in ("hello", "heartbeat") and session.state == "playing":
                    session.observe(track_id, str(name), dict(payload.get("data") or {}))
        except Exception as error:
            log.debug("device events for %s ended: %s", track_id, error)
        if session.stop_flag.wait(2.0):
            return


async def pump_websocket(websocket: Any, session: LiveSession, *, role: str, track: str | None) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)

    def listener(message: dict[str, Any]) -> None:
        if role == "scene" and message.get("type") not in ("input", "seek", "state"):
            return
        if role == "scene" and message.get("type") == "input" and track and message.get("target") != track:
            return

        def put() -> None:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

        loop.call_soon_threadsafe(put)

    session.listeners.append(listener)
    await websocket.send_text(json.dumps({"type": "hello", **session.snapshot()}, default=str))

    async def writer() -> None:
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message, default=str))

    task = asyncio.create_task(writer())
    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "play":
                session.play(message.get("position"))
            elif kind == "pause":
                session.pause()
            elif kind == "seek":
                session.seek(float(message.get("position") or 0))
            elif kind == "stop":
                session.stop()
            elif kind == "emit" and message.get("name"):
                session.observe(message.get("track") or track or "ui", str(message["name"]),
                                dict(message.get("data") or {}))
            elif kind == "loop":
                session.loops = bool(message.get("value"))
    except Exception as error:
        if type(error).__name__ not in ("WebSocketDisconnect",):
            log.debug("live socket ended: %s", error)
    finally:
        task.cancel()
        if listener in session.listeners:
            session.listeners.remove(listener)
