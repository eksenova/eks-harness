from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from eks_harness.review import media
from eks_harness.review.media import VideoInfo

_LOG = logging.getLogger(__name__)
DEFAULT_TOLERANCE = 2
STATUSES = ("pass", "fail", "skip", "error")


class ExpectationError(ValueError):
    pass


@dataclass
class Expectation:
    index: int
    kind: str
    t: float | None = None
    params: dict = field(default_factory=dict)
    tolerance_frames: int = DEFAULT_TOLERANCE
    label: str = ""

    def as_dict(self) -> dict:
        return {"index": self.index, "kind": self.kind, "t": self.t, "params": self.params,
                "tolerance_frames": self.tolerance_frames, "label": self.label}


@dataclass
class Expectations:
    items: list[Expectation]
    base: Path | None = None
    fps: float | None = None


@dataclass
class CheckResult:
    index: int
    kind: str
    label: str
    status: str
    expected: float | None = None
    measured: float | None = None
    delta_frames: int | None = None
    tolerance_frames: int | None = None
    detail: str = ""
    data: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "skip")

    def as_dict(self) -> dict:
        return {"index": self.index, "kind": self.kind, "label": self.label, "status": self.status,
                "expected": _round(self.expected), "measured": _round(self.measured),
                "deltaFrames": self.delta_frames, "toleranceFrames": self.tolerance_frames, "detail": self.detail,
                "data": self.data}


@dataclass
class Report:
    video: VideoInfo
    results: list[CheckResult]
    elapsed: float = 0.0
    detected: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def counts(self) -> dict[str, int]:
        return {status: sum(1 for r in self.results if r.status == status) for status in STATUSES}

    def as_dict(self) -> dict:
        return {"ok": self.ok, "video": self.video.as_dict(), "counts": self.counts(),
                "elapsedSeconds": round(self.elapsed, 2), "results": [r.as_dict() for r in self.results],
                "detected": self.detected}

    def text(self) -> str:
        lines = [f"{'PASS' if self.ok else 'FAIL'} {self.video.path.name}: "
                 + ", ".join(f"{n} {s}" for s, n in self.counts().items() if n)]
        for r in self.results:
            expected = "" if r.expected is None else f"{r.expected:.3f}s"
            measured = "" if r.measured is None else f"{r.measured:.3f}s"
            delta = "" if r.delta_frames is None else f"{r.delta_frames:+d}f/{r.tolerance_frames}f"
            name = f"{r.kind} {r.label}".strip()
            lines.append(f"{r.status.upper():5} #{r.index:<3} {name[:40]:40} {expected:>9} {measured:>9} {delta:>9} "
                         f"{r.detail}".rstrip())
        return "\n".join(lines)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


class Check(Protocol):
    kind: str

    def run(self, ctx: "Context", item: Expectation) -> CheckResult: ...


CheckFn = Callable[["Context", Expectation], CheckResult]
_CHECKS: dict[str, CheckFn] = {}


def register(kind: str, check: Any) -> None:
    if callable(getattr(check, "run", None)):
        _CHECKS[kind] = check.run
    elif callable(check):
        _CHECKS[kind] = check
    else:
        raise TypeError(f"check {kind} is neither callable nor has run()")


def check(kind: str) -> Callable[[CheckFn], CheckFn]:
    def wrap(fn: CheckFn) -> CheckFn:
        register(kind, fn)
        return fn
    return wrap


def kinds() -> list[str]:
    _builtins()
    return sorted(_CHECKS)


def _builtins() -> None:
    from eks_harness.review.checks import audio, frames, text  # noqa: F401


def load_plugin_checks(host: Any = None, tree: Path | None = None) -> list[str]:
    _builtins()
    if host is None:
        try:
            from eks_harness.config import Config
            from eks_harness.paths import resolve_paths
            from eks_harness.plugins.host import PluginHost
            from eks_harness.plugins.project import find_tree

            paths = resolve_paths()
            host = PluginHost(paths, Config(paths))
            tree = tree or find_tree(Path.cwd())
        except Exception:
            _LOG.debug("no plugin host for video checks", exc_info=True)
            return []
    loaded = []
    for contribution, instance in host.load_all("video_check", tree):
        kind = str(getattr(instance, "kind", "") or contribution.id)
        try:
            register(kind, instance)
            loaded.append(kind)
        except TypeError as error:
            _LOG.warning("%s: %s", contribution.key, error)
    return loaded


class Context:
    def __init__(self, info: VideoInfo, base: Path | None = None, analysis_width: int = 64) -> None:
        self.info = info
        self.base = base
        self.analysis_width = analysis_width
        self._cache: dict[str, Any] = {}

    @property
    def fps(self) -> float:
        return self.info.fps

    def cached(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def gray(self) -> np.ndarray:
        return self.cached("gray", lambda: media.gray_frames(self.info, self.analysis_width))

    def diffs(self) -> np.ndarray:
        def make() -> np.ndarray:
            frames = self.gray().astype(np.int16)
            if len(frames) < 2:
                return np.zeros(len(frames), dtype=np.float32)
            values = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) / 255.0
            return np.concatenate([[0.0], values]).astype(np.float32)
        return self.cached("diffs", make)

    def frame(self, number: int, width: int | None = None) -> Any:
        key = f"frame:{number}:{width}"
        return self.cached(key, lambda: media.frame_image(self.info, number, width))

    def audio(self, rate: int = 16000) -> np.ndarray:
        return self.cached(f"audio:{rate}", lambda: media.audio_samples(self.info, rate))

    def loudness(self) -> dict:
        return self.cached("loudness", lambda: media.loudness(self.info))

    def resolve(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self.base is not None:
            candidate = self.base / candidate
        return candidate

    def window(self, item: Expectation, default_seconds: float = 1.0) -> tuple[float, float]:
        given = item.params.get("window")
        if given is not None:
            a, b = float(given[0]), float(given[1])
            return max(0.0, a), min(self.info.duration, b)
        t = item.t or 0.0
        reach = max(default_seconds, 3 * item.tolerance_frames / max(self.fps, 1))
        return max(0.0, t - reach), min(self.info.duration, t + reach)

    def result(self, item: Expectation, *, measured: float | None = None, ok: bool | None = None,
               detail: str = "", data: dict | None = None, status: str | None = None) -> CheckResult:
        delta = None
        if measured is not None and item.t is not None:
            delta = round((measured - item.t) * self.fps)
            if ok is None:
                ok = abs(delta) <= item.tolerance_frames
        if status is None:
            status = "pass" if ok else "fail"
        return CheckResult(index=item.index, kind=item.kind, label=item.label, status=status, expected=item.t,
                           measured=measured, delta_frames=delta,
                           tolerance_frames=item.tolerance_frames if item.t is not None else None,
                           detail=detail, data=data or {})


def parse_expectations(data: Any, base: Path | None = None) -> Expectations:
    fps = None
    default_tolerance = DEFAULT_TOLERANCE
    if isinstance(data, dict):
        fps = data.get("fps")
        default_tolerance = int(data.get("tolerance_frames", data.get("toleranceFrames", DEFAULT_TOLERANCE)))
        raw = data.get("checks", data.get("expectations"))
        if raw is None:
            raise ExpectationError("expectations need a 'checks' list")
    elif isinstance(data, list):
        raw = data
    else:
        raise ExpectationError("expectations must be a list or an object with 'checks'")
    items = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not entry.get("kind"):
            raise ExpectationError(f"check #{index} needs a kind")
        t = entry.get("t")
        tolerance = entry.get("tolerance_frames", entry.get("toleranceFrames", default_tolerance))
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise ExpectationError(f"check #{index}: params must be an object")
        items.append(Expectation(index=index, kind=str(entry["kind"]), t=None if t is None else float(t),
                                 params=params, tolerance_frames=int(tolerance),
                                 label=str(entry.get("label") or entry.get("name") or "")))
    return Expectations(items=items, base=base, fps=float(fps) if fps else None)


def load_expectations(path: Path | str) -> Expectations:
    path = Path(path)
    return parse_expectations(json.loads(path.read_text(encoding="utf-8")), base=path.parent)


def run_checks(video: Path | VideoInfo, expectations: Expectations | dict | list, *,
               base: Path | None = None, plugins: bool = True, host: Any = None) -> Report:
    _builtins()
    if plugins:
        load_plugin_checks(host)
    if not isinstance(expectations, Expectations):
        expectations = parse_expectations(expectations, base=base)
    info = video if isinstance(video, VideoInfo) else media.probe(Path(video))
    if expectations.fps:
        info.fps = expectations.fps
    ctx = Context(info, base=expectations.base or base)
    started = time.monotonic()
    results = []
    for item in expectations.items:
        fn = _CHECKS.get(item.kind)
        if fn is None:
            results.append(CheckResult(index=item.index, kind=item.kind, label=item.label, status="error",
                                       expected=item.t, detail=f"no check named {item.kind!r} "
                                                               f"(known: {', '.join(sorted(_CHECKS))})"))
            continue
        try:
            results.append(fn(ctx, item))
        except Exception as error:
            _LOG.debug("check %s failed", item.kind, exc_info=True)
            results.append(CheckResult(index=item.index, kind=item.kind, label=item.label, status="error",
                                       expected=item.t, detail=f"{type(error).__name__}: {error}"[:500]))
    detected = {}
    if "cuts" in ctx._cache:
        detected["cuts"] = [round(t, 4) for t in ctx._cache["cuts"]]
    if "onsets" in ctx._cache:
        detected["onsets"] = [round(t, 4) for t in ctx._cache["onsets"]]
    return Report(video=info, results=results, elapsed=time.monotonic() - started, detected=detected)
