"""Structured progress reporting for the render pipeline.

The orchestrator, frame pipeline and marker extractors emit lifecycle events
(step boundaries, sub-step boundaries, frame counters, log lines) through a
:class:`ProgressReporter`. The abstraction lets us keep the SDK code-path
agnostic to delivery: the MCP render driver attaches a
:class:`JsonProgressReporter` so every event becomes a JSON line on stdout,
while a human running the CLI directly gets a :class:`TqdmProgressReporter`
that draws a live bar on stderr without disturbing the JSON channel.

Event shape (all events carry an ``event`` discriminator + ``ts`` timestamp):

``step_start`` / ``step_end``
    Major pipeline phases: ``load_project``, ``extract_markers``,
    ``plan_segments``, ``render_segments``, ``mux_audio``, ``finalize``.
``substep_start`` / ``substep_end``
    Per-source marker extractions, per-segment renders, per-effect work.
``frame``
    Throttled frame counter inside ``render_segments`` with observed fps
    and ETA in seconds.
``log``
    Free-form info/warning messages from extractors and the pipeline.
``done``
    Terminal success event with the output path and total elapsed.
``error`` / ``cancelled``
    Terminal failure events; the driver also exits with a matching code.

ETA: a simple, predictable estimate. Inside a frame-counted step the ETA is
``(remaining_frames / fps_observed)``. The overall ETA reported on each
``frame`` event extrapolates from the current segment's frame counter and
adds a small fixed budget for mux + finalize so the user-visible bar does
not snap to 100% the instant the last frame encodes.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Any, ClassVar, Iterator, Protocol, runtime_checkable

try:  # tqdm is optional - the SDK works without it.
    from tqdm import tqdm as _tqdm
except Exception:  # pragma: no cover - import guard
    _tqdm = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from tqdm import tqdm as TqdmType

__all__ = [
    "JsonProgressReporter",
    "NoopProgressReporter",
    "ProgressEvent",
    "ProgressReporter",
    "TOTAL_STEPS",
    "TqdmProgressReporter",
    "build_default_reporter",
]

_LOG = logging.getLogger(__name__)


# Stable ordering used by both the orchestrator and any reporter that wants
# to render a global progress bar. Keep in sync with the boundaries emitted
# by the orchestrator. The orchestrator emits ``index`` values that are
# **0-based** (load_project=0 ... finalize=5); UI consumers display
# ``index + 1`` when surfacing "step N of M" to humans.
STEP_ORDER: tuple[str, ...] = (
    "load_project",
    "extract_markers",
    "plan_segments",
    "render_segments",
    "mux_audio",
    "finalize",
)

TOTAL_STEPS: int = len(STEP_ORDER)


@dataclass
class ProgressEvent:
    """Structured progress event.

    Reporters serialize this verbatim (after dropping ``None`` fields) into
    whatever transport they expose. ``payload`` carries event-specific extras
    so future fields can be added without churning the dataclass.
    """

    event: str
    ts: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event, "ts": round(self.ts, 3)}
        for key, value in self.payload.items():
            if value is None:
                continue
            out[key] = value
        return out


@runtime_checkable
class ProgressReporter(Protocol):
    """Sink for structured render progress events.

    Implementations must be safe to call from any thread; the frame pipeline
    in particular emits ``frame`` events from a worker thread. Implementations
    that mutate shared state should guard it with their own lock.
    """

    def emit(self, event: ProgressEvent) -> None:
        ...

    def close(self) -> None:
        ...


class NoopProgressReporter:
    """Default reporter used when none is provided. Discards every event."""

    def emit(self, event: ProgressEvent) -> None:  # noqa: D401 - protocol impl
        return None

    def close(self) -> None:
        return None


class JsonProgressReporter:
    """Writes one JSON line per event to ``stream`` (default ``sys.stdout``).

    Thread-safe: writes are guarded by a lock so concurrent emitters do not
    interleave bytes within a line. Each line ends with ``\\n`` and the
    stream is flushed immediately so consumers reading line-buffered get
    events promptly.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: ProgressEvent) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                # The downstream pipe was closed; nothing we can do.
                _LOG.debug("progress stream write failed", exc_info=True)

    def close(self) -> None:
        return None


class TqdmProgressReporter:
    """Renders a tqdm bar on stderr while delegating events to a JSON reporter.

    The visual is purely additive: every event still flows through the
    underlying :class:`JsonProgressReporter` so machine consumers stay
    intact. The bar shows the active step in its description, a frame-level
    counter while ``render_segments`` is in flight, and falls back to a
    step-level percentage everywhere else.
    """

    _STEP_WEIGHTS: ClassVar[dict[str, float]] = {
        "load_project": 0.01,
        "extract_markers": 0.20,
        "plan_segments": 0.01,
        "render_segments": 0.72,
        "mux_audio": 0.04,
        "finalize": 0.02,
    }

    def __init__(self, inner: ProgressReporter) -> None:
        if _tqdm is None:  # pragma: no cover - guarded by build_default_reporter
            raise RuntimeError("tqdm is not installed")
        self._inner = inner
        self._lock = threading.Lock()
        self._bar: TqdmType | None = None
        self._current_step: str | None = None
        self._completed_weight: float = 0.0
        self._step_started_at: float = time.monotonic()
        self._frame_total: int | None = None
        self._build_bar()

    def _build_bar(self) -> None:
        assert _tqdm is not None
        self._bar = _tqdm(
            total=1000,
            unit="",
            bar_format="{desc} {percentage:3.0f}%|{bar}| {postfix}",
            leave=True,
            dynamic_ncols=True,
            file=sys.stderr,
        )
        self._bar.set_description_str("starting")
        self._bar.set_postfix_str("")

    def emit(self, event: ProgressEvent) -> None:
        self._inner.emit(event)
        with self._lock:
            self._update_bar(event)

    def _update_bar(self, event: ProgressEvent) -> None:
        if self._bar is None:
            return
        kind = event.event
        if kind == "step_start":
            step = str(event.payload.get("step", ""))
            self._current_step = step
            self._step_started_at = time.monotonic()
            self._frame_total = event.payload.get("frame_total")
            self._bar.set_description_str(step)
        elif kind == "step_end":
            step = str(event.payload.get("step", ""))
            self._completed_weight += self._STEP_WEIGHTS.get(step, 0.0)
            pct = max(0.0, min(1.0, self._completed_weight))
            self._bar.n = int(pct * 1000)
            self._bar.refresh()
        elif kind == "frame":
            frame_idx = int(event.payload.get("frame_index", 0))
            frame_total = int(event.payload.get("frame_total") or self._frame_total or 0)
            fps = float(event.payload.get("fps_observed", 0.0))
            eta = float(event.payload.get("eta_s", 0.0))
            within = (frame_idx / frame_total) if frame_total else 0.0
            step_weight = self._STEP_WEIGHTS.get(self._current_step or "", 0.0)
            pct = max(0.0, min(1.0, self._completed_weight + within * step_weight))
            self._bar.n = int(pct * 1000)
            self._bar.set_postfix_str(
                f"{frame_idx}/{frame_total or '?'} @ {fps:.1f} fps eta {eta:.1f}s"
            )
            self._bar.refresh()
        elif kind == "log":
            level = str(event.payload.get("level", "info")).upper()
            message = str(event.payload.get("message", ""))
            assert _tqdm is not None
            _tqdm.write(f"[{level}] {message}", file=sys.stderr)
        elif kind in {"done", "error", "cancelled"}:
            if kind == "done":
                self._bar.n = 1000
            self._bar.set_description_str(kind)
            self._bar.set_postfix_str(str(event.payload.get("message", "")))
            self._bar.refresh()

    def close(self) -> None:
        with self._lock:
            if self._bar is not None:
                try:
                    self._bar.close()
                finally:
                    self._bar = None
        self._inner.close()


def build_default_reporter(
    *,
    stream: IO[str] | None = None,
    enable_tqdm: bool | None = None,
) -> ProgressReporter:
    """Build the reporter for the render driver.

    Always wires a :class:`JsonProgressReporter` so machine consumers see
    every event. When ``enable_tqdm`` is true (or ``None`` and stderr is a
    tty + tqdm is importable), wraps it with :class:`TqdmProgressReporter`
    so humans get a live bar on stderr.
    """

    json_reporter = JsonProgressReporter(stream=stream)
    if enable_tqdm is None:
        enable_tqdm = bool(_tqdm is not None and sys.stderr.isatty())
    if enable_tqdm and _tqdm is not None:
        try:
            return TqdmProgressReporter(json_reporter)
        except Exception:
            _LOG.debug("tqdm reporter init failed; falling back to JSON only", exc_info=True)
    return json_reporter


# ---------------------------------------------------------------------------
# Helpers for emitters
# ---------------------------------------------------------------------------


@contextmanager
def step(
    reporter: ProgressReporter,
    name: str,
    *,
    index: int,
    total: int = TOTAL_STEPS,
    extra: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Bracket a major step with ``step_start`` / ``step_end`` events."""

    start = time.monotonic()
    payload: dict[str, Any] = {"step": name, "index": index, "total": total}
    if extra:
        payload.update(extra)
    reporter.emit(ProgressEvent("step_start", payload=payload))
    try:
        yield
    finally:
        reporter.emit(
            ProgressEvent(
                "step_end",
                payload={"step": name, "elapsed_s": round(time.monotonic() - start, 3)},
            )
        )


@contextmanager
def substep(
    reporter: ProgressReporter,
    *,
    step_name: str,
    name: str,
    kind: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Bracket a sub-step. Yields a mutable ``detail`` dict the caller can fill in.

    The dict is attached to the ``substep_end`` event as the ``detail`` field
    so callers (e.g. the beat tracker) can surface backend identity or counts
    without having to call ``reporter.emit`` directly.
    """

    start = time.monotonic()
    payload: dict[str, Any] = {"step": step_name, "name": name}
    if kind is not None:
        payload["kind"] = kind
    if extra:
        payload.update(extra)
    reporter.emit(ProgressEvent("substep_start", payload=payload))
    detail: dict[str, Any] = {}
    try:
        yield detail
    finally:
        end_payload: dict[str, Any] = {
            "step": step_name,
            "name": name,
            "elapsed_s": round(time.monotonic() - start, 3),
        }
        if detail:
            end_payload["detail"] = detail
        reporter.emit(ProgressEvent("substep_end", payload=end_payload))


def emit_log(
    reporter: ProgressReporter,
    *,
    level: str,
    message: str,
    **fields: Any,
) -> None:
    """Emit a ``log`` event. Convenience wrapper used by the SDK call sites."""

    payload: dict[str, Any] = {"level": level, "message": message}
    payload.update(fields)
    reporter.emit(ProgressEvent("log", payload=payload))
