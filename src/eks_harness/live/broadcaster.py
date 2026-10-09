from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eks_harness.pools.base import LiveSource

log = logging.getLogger("eks_harness.live")

SourceFactory = Callable[[], LiveSource]
STOP_TIMEOUT = 25.0


class Viewer:
    def __init__(self, resource: str, loop: asyncio.AbstractEventLoop | None = None, label: str = "") -> None:
        self.resource = resource
        self.label = label
        self.attached_at = time.time()
        self.frames_sent = 0
        self._loop = loop
        self._event = asyncio.Event() if loop is not None else None
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._latest: bytes | None = None
        self._closed = False
        self._error: str | None = None

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def error(self) -> str | None:
        with self._cond:
            return self._error

    @property
    def latest(self) -> bytes | None:
        with self._cond:
            return self._latest

    def _wake(self) -> None:
        self._cond.notify_all()
        if self._loop is not None and self._event is not None:
            try:
                self._loop.call_soon_threadsafe(self._event.set)
            except RuntimeError:
                pass

    def offer(self, frame: bytes) -> None:
        with self._cond:
            if self._closed:
                return
            self._frame = frame
            self._latest = frame
            self._wake()

    def close(self, error: str | None = None) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._error = error
            self._wake()

    def _take(self) -> tuple[bytes | None, bool]:
        with self._cond:
            frame, self._frame = self._frame, None
            return frame, self._closed

    async def next_frame(self, timeout: float) -> bytes | None:
        if self._event is None:
            raise RuntimeError("this viewer was attached without an event loop; use get()")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            frame, closed = self._take()
            if frame is not None:
                return frame
            if closed:
                return None
            self._event.clear()
            with self._cond:
                if self._frame is not None or self._closed:
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except TimeoutError:
                return None

    def get(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._frame is None and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            frame, self._frame = self._frame, None
            return frame


class Producer:
    def __init__(self, channel: "Channel", factory: SourceFactory) -> None:
        self.channel = channel
        self.factory = factory
        self.source: LiveSource | None = None
        self.started_at = time.time()
        self.frames = 0
        self.last_frame_at: float | None = None
        self.error: str | None = None
        self.state = "starting"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"live-{channel.resource}")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        source: LiveSource | None = None
        iterator = None
        try:
            source = self.factory()
            with self._lock:
                self.source = source
                stopped = self._stop.is_set()
            if stopped:
                return
            self.state = "running"
            iterator = source.frames()
            for frame in iterator:
                if self._stop.is_set():
                    break
                if not frame:
                    continue
                self.frames += 1
                self.last_frame_at = time.time()
                self.channel.publish(frame)
            else:
                if not self._stop.is_set():
                    self.error = "the stream ended"
        except Exception as error:
            if not self._stop.is_set():
                self.error = str(error) or type(error).__name__
                log.warning("live stream of %s failed: %s", self.channel.resource, self.error)
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:
                    log.exception("closing the live source of %s failed", self.channel.resource)
            if iterator is not None and hasattr(iterator, "close"):
                try:
                    iterator.close()
                except Exception:
                    log.exception("closing the live source of %s failed", self.channel.resource)
            self.state = "stopped" if self._stop.is_set() or self.error is None else "failed"
            self.channel.producer_ended(self)

    def stop(self, wait: bool = True, timeout: float = STOP_TIMEOUT) -> None:
        self._stop.set()
        with self._lock:
            source = self.source
        if source is not None:
            try:
                source.close()
            except Exception:
                log.exception("closing the live source of %s failed", self.channel.resource)
        if wait and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def join(self, timeout: float) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)


class Channel:
    def __init__(self, hub: "LiveHub", resource: str, factory: SourceFactory) -> None:
        self.hub = hub
        self.resource = resource
        self.factory = factory
        self.viewers: set[Viewer] = set()
        self.producer: Producer | None = None
        self.last_frame: bytes | None = None
        self.last_frame_at: float | None = None
        self.frames = 0
        self.created_at = time.time()
        self.idle_since: float | None = None
        self.idle_timer: threading.Timer | None = None
        self.last_error: str | None = None

    def publish(self, frame: bytes) -> None:
        with self.hub.lock:
            self.last_frame = frame
            self.last_frame_at = time.time()
            self.frames += 1
            viewers = list(self.viewers)
        for viewer in viewers:
            viewer.offer(frame)

    def producer_ended(self, producer: Producer) -> None:
        self.hub._producer_ended(self, producer)


@dataclass(frozen=True)
class ChannelInfo:
    resource: str
    viewers: int
    frames: int
    state: str
    error: str | None
    suspended: str | None
    created_at: float
    last_frame_at: float | None
    idle_since: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"resource": self.resource, "viewers": self.viewers, "frames": self.frames, "state": self.state,
                "error": self.error, "suspended": self.suspended, "createdAt": self.created_at,
                "lastFrameAt": self.last_frame_at, "idleSince": self.idle_since}


class LiveHub:
    def __init__(self, idle_seconds: Callable[[], float] | float = 10.0) -> None:
        self.lock = threading.RLock()
        self._idle_seconds = idle_seconds
        self._channels: dict[str, Channel] = {}
        self._suspended: dict[str, str] = {}
        self._closed = False

    @classmethod
    def from_config(cls, config) -> "LiveHub":
        return cls(lambda: float(config["live.idleStopSeconds"]))

    def idle_seconds(self) -> float:
        value = self._idle_seconds() if callable(self._idle_seconds) else self._idle_seconds
        return max(0.0, float(value))

    def attach(self, resource: str, factory: SourceFactory, loop: asyncio.AbstractEventLoop | None = None,
               label: str = "") -> Viewer:
        viewer = Viewer(resource, loop, label)
        with self.lock:
            if self._closed:
                viewer.close("the daemon is shutting down")
                return viewer
            channel = self._channels.get(resource)
            if channel is None:
                channel = Channel(self, resource, factory)
                self._channels[resource] = channel
            else:
                channel.factory = factory
            if channel.idle_timer is not None:
                channel.idle_timer.cancel()
                channel.idle_timer = None
            channel.idle_since = None
            channel.viewers.add(viewer)
            if channel.last_frame is not None:
                viewer.offer(channel.last_frame)
            if channel.producer is None and resource not in self._suspended:
                self._start(channel)
        return viewer

    def _start(self, channel: Channel) -> None:
        producer = Producer(channel, channel.factory)
        channel.producer = producer
        channel.last_error = None
        producer.start()

    def detach(self, viewer: Viewer) -> None:
        viewer.close()
        with self.lock:
            channel = self._channels.get(viewer.resource)
            if channel is None or viewer not in channel.viewers:
                return
            channel.viewers.discard(viewer)
            if channel.viewers:
                return
            channel.idle_since = time.time()
            idle = self.idle_seconds()
            timer = threading.Timer(idle, self._idle_expired, args=(channel,))
            timer.daemon = True
            channel.idle_timer = timer
        timer.start()

    def _idle_expired(self, channel: Channel) -> None:
        with self.lock:
            if channel.viewers or self._channels.get(channel.resource) is not channel:
                return
            del self._channels[channel.resource]
            channel.idle_timer = None
            producer, channel.producer = channel.producer, None
        if producer is not None:
            log.info("live stream of %s stopped: no viewers for %.0fs", channel.resource, self.idle_seconds())
            producer.stop(wait=True)

    def _producer_ended(self, channel: Channel, producer: Producer) -> None:
        with self.lock:
            if channel.producer is not producer:
                return
            channel.producer = None
            if producer.stopping:
                return
            if channel.resource in self._suspended:
                return
            channel.last_error = producer.error or "the stream ended"
            viewers = list(channel.viewers)
            channel.viewers.clear()
            if self._channels.get(channel.resource) is channel:
                del self._channels[channel.resource]
            if channel.idle_timer is not None:
                channel.idle_timer.cancel()
                channel.idle_timer = None
        for viewer in viewers:
            viewer.close(channel.last_error)

    def suspend(self, resource: str, reason: str) -> bool:
        with self.lock:
            self._suspended[resource] = reason
            channel = self._channels.get(resource)
            producer = channel.producer if channel is not None else None
            if channel is not None:
                channel.producer = None
        if producer is not None:
            log.info("live stream of %s suspended: %s", resource, reason)
            producer.stop(wait=True)
            return True
        return False

    def resume(self, resource: str) -> bool:
        with self.lock:
            self._suspended.pop(resource, None)
            channel = self._channels.get(resource)
            if channel is None or channel.producer is not None or not channel.viewers or self._closed:
                return False
            self._start(channel)
            return True

    def suspended(self, resource: str) -> str | None:
        with self.lock:
            return self._suspended.get(resource)

    def stop(self, resource: str, reason: str = "stopped") -> bool:
        with self.lock:
            channel = self._channels.pop(resource, None)
            if channel is None:
                return False
            if channel.idle_timer is not None:
                channel.idle_timer.cancel()
            producer, channel.producer = channel.producer, None
            viewers = list(channel.viewers)
            channel.viewers.clear()
        for viewer in viewers:
            viewer.close(reason)
        if producer is not None:
            producer.stop(wait=True)
        return True

    def active(self, resource: str) -> bool:
        with self.lock:
            channel = self._channels.get(resource)
            return channel is not None and channel.producer is not None

    def last_frame(self, resource: str) -> bytes | None:
        with self.lock:
            channel = self._channels.get(resource)
            return channel.last_frame if channel is not None else None

    def info(self, resource: str) -> ChannelInfo | None:
        with self.lock:
            channel = self._channels.get(resource)
            return self._info(channel) if channel is not None else None

    def _info(self, channel: Channel) -> ChannelInfo:
        producer = channel.producer
        if producer is not None:
            state = producer.state
        elif channel.resource in self._suspended:
            state = "suspended"
        else:
            state = "idle"
        return ChannelInfo(channel.resource, len(channel.viewers), channel.frames, state,
                           producer.error if producer is not None else channel.last_error,
                           self._suspended.get(channel.resource), channel.created_at, channel.last_frame_at,
                           channel.idle_since)

    def snapshot(self) -> list[ChannelInfo]:
        with self.lock:
            return [self._info(c) for c in self._channels.values()]

    def shutdown(self, reason: str = "the daemon is shutting down") -> None:
        with self.lock:
            self._closed = True
            resources = list(self._channels)
        threads = []
        for resource in resources:
            thread = threading.Thread(target=self.stop, args=(resource, reason), daemon=True)
            thread.start()
            threads.append(thread)
        deadline = time.monotonic() + STOP_TIMEOUT
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
