from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from eks_harness.engine.clock import resolve
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    MediaFile,
    ResolveContext,
    builtin_provenance,
)
from eks_harness.ir.ops.check import (
    DEFAULT_OCR_LANGUAGES,
    BeatsCheck,
    BlackCheck,
    CutCheck,
    FrozenCheck,
    LoudnessCheck,
    MediaCheck,
    MotionCheck,
    OnsetCheck,
    SafeAreaCheck,
    TemplateCheck,
    TextCheck,
)
from eks_harness.ir.registry import CheckSpec, Registry, implementation
from eks_harness.ir.target import Target
from eks_harness.ir.time import (
    TICKS_PER_SECOND,
    Span,
    Time,
    TimeContext,
    TimeError,
    TimeSet,
    format_time,
)
from eks_harness.ir.types import MediaInfo
from eks_harness.ops import BUILTIN_EXECUTOR_VERSION
from eks_harness.review import ocr
from eks_harness.review.checks import (
    DEFAULT_TOLERANCE,
    CheckResult,
    Expectation,
    Expectations,
    run_checks,
)
from eks_harness.review.media import MediaError, VideoInfo, probe

MEDIA_CHECK_OP = "check.media"
MEDIA_CHECK_EXECUTOR = "eks.check.media"
MEDIA_CHECK_VERSION = BUILTIN_EXECUTOR_VERSION
OCR_KINDS = frozenset({"text", "safe_area"})
AUDIO_KINDS = frozenset({"loudness", "onset", "beats"})
WORKER_SCRIPTS = (".js", ".mjs", ".ts")
DEFAULT_FPS = 30.0
SAFE_EPSILON = 0.005


@dataclass(frozen=True, slots=True)
class MediaCheckCall:
    call: ExecContext
    kind: str
    path: Path
    media: MediaInfo
    params: Mapping[str, JsonValue]
    at: float | None
    span: tuple[float, float] | None
    tolerance_frames: int
    fps: float
    label: str | None = None


@dataclass(frozen=True, slots=True)
class _Where:
    at: float | None
    span: tuple[float, float] | None
    tolerance: int


@dataclass(frozen=True, slots=True)
class _Job:
    kind: str
    params: dict[str, Any]
    t: float | None = None


@dataclass(frozen=True, slots=True)
class _Direct:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict[str, Any])


Adapter = Callable[["_Adapting"], "_Job | _Direct"]


@dataclass(frozen=True, slots=True)
class _Adapting:
    call: ExecContext
    args: MediaCheck
    params: Mapping[str, JsonValue]
    info: VideoInfo
    where: _Where
    engine: str | None



def _plain(value: Any) -> JsonValue:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in cast("Mapping[Any, Any]", value).items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in cast("list[Any]", value)]
    item = getattr(value, "item", None)
    if callable(item):
        return _plain(item())
    return str(value)


def _invalid(kind: str, message: str) -> NodeFailure:
    return NodeFailure("args_invalid", f"check.media {kind}: {message}", detail={"kind": kind})


def _model[M: BaseModel](model: type[M], kind: str, params: Mapping[str, JsonValue]) -> M:
    try:
        return model.model_validate(dict(params))
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "(params)"
        raise _invalid(kind, f"params {where}: {first['msg']}") from error


def _seconds(ticks: int) -> float:
    return ticks / TICKS_PER_SECOND


def _box(kind: str, region: Target | None, info: VideoInfo) -> list[float] | None:
    if region is None:
        return None
    if region.by != "rect":
        raise _invalid(kind, f"region by.{region.by} needs a box map of the media, and rendered media has none; "
                             "give the region as by.rect(x, y, w, h) in frame pixels")
    if info.width <= 0 or info.height <= 0:
        raise _invalid(kind, "a region needs a video stream")
    x, y, w, h = (float(cast("float", region.args[name])) for name in ("x", "y", "w", "h"))
    return [x / info.width, y / info.height, w / info.width, h / info.height]


def _window(adapting: _Adapting) -> list[float]:
    where = adapting.where
    if where.span is not None:
        return [where.span[0], where.span[1]]
    if where.at is not None:
        return [where.at, where.at]
    return [0.0, adapting.info.duration]


def _presence(adapting: _Adapting, params: dict[str, Any], kind: str) -> _Job:
    where = adapting.where
    if where.at is not None:
        return _Job(kind, {**params, "mode": "present"}, where.at)
    return _Job(kind, {**params, "mode": "appears", "window": _window(adapting)})


def _require_time(adapting: _Adapting) -> float:
    if adapting.where.at is None:
        shown = "a span" if adapting.where.span is not None else "no time"
        raise _invalid(adapting.args.kind, f"needs at= as one time, got {shown}")
    return adapting.where.at


def _cut(adapting: _Adapting) -> _Job:
    model = _model(CutCheck, "cut", adapting.params)
    return _Job("cut", {"threshold": model.threshold}, _require_time(adapting))


def _onset(adapting: _Adapting) -> _Job:
    model = _model(OnsetCheck, "onset", adapting.params)
    return _Job("onset", {"sensitivity": model.sensitivity, "require_audio": True}, _require_time(adapting))


def _beats(adapting: _Adapting) -> _Job | _Direct:
    model = _model(BeatsCheck, "beats", adapting.params)
    if adapting.where.at is not None:
        raise _invalid("beats", "at= is a span the stream times are taken from, not one time")
    context = adapting.call.time()
    try:
        ticks = context.resolve_set(TimeSet(stream=model.stream))
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"check.media beats: stream {model.stream!r}: {error}") from error
    origin = _origin(adapting.call, context)
    low, high = adapting.where.span if adapting.where.span is not None else (0.0, adapting.info.duration)
    times = [seconds for seconds in (_seconds(value - origin) for value in ticks)
             if low - 1e-9 <= seconds <= high + 1e-9 and 0.0 <= seconds <= adapting.info.duration]
    if not times:
        return _Direct(False, f"stream {model.stream!r} has no times in {low:.3f}-{high:.3f}s of the media",
                       {"stream": model.stream, "window": [low, high]})
    return _Job("beats", {"times": times, "min_ratio": model.min_ratio, "stream": model.stream})


def _loudness(adapting: _Adapting) -> _Job:
    model = _model(LoudnessCheck, "loudness", adapting.params)
    if adapting.where.at is not None or adapting.where.span is not None:
        raise _invalid("loudness", "measures the whole media; at= is not supported")
    return _Job("loudness", {"lufs": model.lufs, "lufs_tolerance": model.tolerance,
                             "true_peak_max": model.true_peak, "require_audio": True})


def _text(adapting: _Adapting) -> _Job:
    model = _model(TextCheck, "text", adapting.params)
    params: dict[str, Any] = {"text": model.text, "engine": adapting.engine or model.engine,
                              "languages": list(model.languages)}
    box = _box("text", model.region, adapting.info)
    if box is not None:
        params["box"] = box
    return _presence(adapting, params, "text")


def _motion(adapting: _Adapting) -> _Job:
    model = _model(MotionCheck, "motion", adapting.params)
    if model.min is None and model.max is None:
        raise _invalid("motion", "needs min or max")
    if adapting.where.at is not None:
        raise _invalid("motion", "at= is a span, not one time")
    params: dict[str, Any] = {"min": model.min, "max": model.max}
    if adapting.where.span is not None:
        params["window"] = list(adapting.where.span)
    return _Job("motion_level", params)


def _longest_frames(longest: Time | None, info: VideoInfo, call: ExecContext) -> int | None:
    if longest is None:
        return None
    seconds = _length_seconds(call, longest)
    return math.floor(seconds * info.fps + 1e-6)


def _stretch(adapting: _Adapting, kind: str, longest: Time | None) -> _Job:
    if adapting.where.at is not None:
        raise _invalid(kind, "at= is a span, not one time")
    params: dict[str, Any] = {}
    limit = _longest_frames(longest, adapting.info, adapting.call)
    if limit is not None:
        params["max_frames"] = limit
    if adapting.where.span is not None:
        params["window"] = list(adapting.where.span)
    return _Job(kind, params)


def _black(adapting: _Adapting) -> _Job:
    return _stretch(adapting, "black", _model(BlackCheck, "black", adapting.params).longest)


def _frozen(adapting: _Adapting) -> _Job:
    return _stretch(adapting, "frozen", _model(FrozenCheck, "frozen", adapting.params).longest)


def _safe_area(adapting: _Adapting) -> _Job | _Direct:
    model = _model(SafeAreaCheck, "safe_area", adapting.params)
    top, right, bottom, left = model.margins
    box = _box("safe_area", model.region, adapting.info)
    if box is not None:
        x, y, w, h = box
        inside = (x >= left - SAFE_EPSILON and y >= top - SAFE_EPSILON and x + w <= 1 - right + SAFE_EPSILON
                  and y + h <= 1 - bottom + SAFE_EPSILON)
        message = "the region is inside the safe area" if inside else "the region reaches into the unsafe margins"
        return _Direct(inside, message, {"region": box, "margins": list(model.margins)})
    params: dict[str, Any] = {"margins": list(model.margins), "engine": adapting.engine or model.engine,
                              "languages": list(DEFAULT_OCR_LANGUAGES)}
    if adapting.where.span is not None or adapting.where.at is not None:
        params["window"] = _window(adapting)
    return _Job("text_safe_area", params)


def _template(adapting: _Adapting) -> _Job:
    raw = adapting.params.get("template")
    if not isinstance(raw, Mapping):
        raise _invalid("template", "params template must be a media.source of kind image")
    template = adapting.call.media(cast("JsonValue", raw))
    threshold = adapting.params.get("threshold", TemplateCheck.model_fields["threshold"].default)
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not 0.0 <= threshold <= 1.0:
        raise _invalid("template", f"params threshold must be a number from 0 to 1, got {threshold!r}")
    region = adapting.params.get("region")
    try:
        target = None if region is None else Target.model_validate(region)
    except ValidationError as error:
        raise _invalid("template", f"params region: {error.errors(include_url=False)[0]['msg']}") from error
    params: dict[str, Any] = {"template": str(template.path), "threshold": float(threshold)}
    box = _box("template", target, adapting.info)
    if box is not None:
        params["box"] = box
    return _presence(adapting, params, "visual")


ADAPTERS: Mapping[str, Adapter] = {
    "cut": _cut,
    "beats": _beats,
    "onset": _onset,
    "loudness": _loudness,
    "text": _text,
    "motion": _motion,
    "black": _black,
    "frozen": _frozen,
    "safe_area": _safe_area,
    "template": _template,
}


def _origin(call: ExecContext, context: TimeContext) -> int:
    window = call.clock.window
    if window is None:
        return 0
    return resolve(context, window.start, "the clock window start")


def _length_seconds(call: ExecContext, length: Time) -> float:
    if length.anchor is not None:
        raise NodeFailure("time_unresolved", f"{format_time(length)!r} is a time, not a length")
    if length.bars == 0 and length.beats == 0 and length.frames == 0:
        return _seconds(length.ticks)
    return _seconds(resolve(call.time(), length, f"length {format_time(length)!r}"))


def tolerance_frames(call: ExecContext, tolerance: Time | None, fps: float) -> int:
    if tolerance is None:
        return DEFAULT_TOLERANCE
    if tolerance.anchor is not None:
        raise NodeFailure("time_unresolved", f"tolerance {format_time(tolerance)!r} is a time, not a length")
    frames = float(tolerance.frames)
    rest = Time(bars=tolerance.bars, beats=tolerance.beats, ticks=tolerance.ticks)
    seconds = _length_seconds(call, rest) if rest.bars or rest.beats or rest.ticks else 0.0
    return max(0, round(frames + seconds * fps))


def _where(call: ExecContext, args: MediaCheck, fps: float) -> _Where:
    tolerance = tolerance_frames(call, args.tolerance, fps)
    at = args.at
    if at is None:
        return _Where(None, None, tolerance)
    context = call.time()
    origin = _origin(call, context)
    if isinstance(at, Span):
        start = resolve(context, at.start, f"check.media at start {format_time(at.start)!r}")
        end = resolve(context, at.end, f"check.media at end {format_time(at.end)!r}")
        if end < start:
            raise NodeFailure("time_unresolved", f"check.media at {format_time(at.start)!r} to "
                                                 f"{format_time(at.end)!r} ends before it starts")
        return _Where(None, (_seconds(start - origin), _seconds(end - origin)), tolerance)
    return _Where(_seconds(resolve(context, at, f"check.media at {format_time(at)!r}") - origin), None, tolerance)


def _at_value(where: _Where) -> JsonValue:
    if where.span is not None:
        return {"start": round(where.span[0], 6), "end": round(where.span[1], 6)}
    return None if where.at is None else round(where.at, 6)


def _ocr_engine(name: str) -> tuple[str, str]:
    try:
        chosen = ocr.engine(name)
        return chosen.name, chosen.version()
    except (ocr.OcrError, OSError) as error:
        what = "no OCR engine is available" if name == "auto" else f"the pinned OCR engine {name!r} is unavailable"
        raise NodeFailure("resolution_unavailable", f"check.media: {what} on this machine: {error}",
                          detail={"engine": name}) from error


def _engine_name(params: Mapping[str, JsonValue]) -> str:
    value = params.get("engine", "auto")
    return value if isinstance(value, str) else "auto"


def _check_spec(registry: Registry, kind: str) -> CheckSpec:
    try:
        spec = registry.check(kind)
    except KeyError as error:
        raise _invalid(kind, "is not a registered check kind") from error
    if spec.family != "media":
        raise _invalid(kind, f"is a {spec.family} check, not a media check")
    return spec


def _media_args(args: BaseModel) -> MediaCheck:
    if isinstance(args, MediaCheck):
        return args
    try:
        return MediaCheck.model_validate(args.model_dump(mode="python", by_alias=True), strict=False)
    except ValidationError as error:
        raise NodeFailure("args_invalid", f"check.media needs media check arguments: {error}") from error


def _params(data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    raw = data.get("params")
    return cast("Mapping[str, JsonValue]", raw) if isinstance(raw, Mapping) else {}


def _kind(data: Mapping[str, JsonValue]) -> str:
    kind = data.get("kind")
    if not isinstance(kind, str) or not kind:
        raise NodeFailure("args_invalid", "check.media needs a kind")
    return kind


def _impl(spec: CheckSpec) -> object:
    reference = spec.impl or ""
    if reference.endswith(WORKER_SCRIPTS):
        raise NodeFailure("executor_missing",
                          f"media check {spec.kind!r} names the worker script {reference}; media checks run on the "
                          "hub and need a Python implementation (module:qualname)")
    found = implementation(reference)
    if found is None:
        raise NodeFailure("executor_missing", f"media check {spec.kind!r}: {reference} cannot be loaded")
    return found() if inspect.isclass(found) else found


def _runner(target: object, kind: str) -> Callable[[MediaCheckCall], object]:
    method = getattr(target, "run", None)
    if callable(method):
        return cast("Callable[[MediaCheckCall], object]", method)
    if callable(target):
        return cast("Callable[[MediaCheckCall], object]", target)
    raise NodeFailure("executor_missing", f"media check {kind!r}: the implementation is not callable and has no run()")


def _resolve_context(call: ExecContext) -> ResolveContext:
    return ResolveContext(run=call.run, plan=call.plan, registry=call.registry, node=call.node, spec=call.spec,
                          args=call.args, data=call.data, resources=call.resources)


class MediaCheckExecutor:
    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(MEDIA_CHECK_EXECUTOR, MEDIA_CHECK_VERSION, ops=(MEDIA_CHECK_OP,), run=self.run,
                            resolver=self.resolve, provenance=builtin_provenance(),
                            description="Runs a media review check (cut, beats, loudness, text and more) on media")

    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        return self._resolution(context, _kind(context.data), _params(context.data)) or {}

    def run(self, call: ExecContext) -> ExecResult:
        args = _media_args(call.args)
        kind = _kind(call.data)
        spec = _check_spec(call.registry, kind)
        params = _params(call.data)
        resolution = self._resolution(_resolve_context(call), kind, params)
        media = call.media(call.data.get("media"))
        call.check_cancelled()
        if spec.impl is not None:
            return self._plugin(call, args, spec, media, params, resolution)
        adapter = ADAPTERS.get(kind)
        if adapter is None:
            raise NodeFailure("executor_missing", f"media check {kind!r} has no implementation on this hub")
        info = self._probe(media.path, kind, call)
        where = _where(call, args, info.fps)
        engine = cast("str", resolution["engine"]) if resolution is not None and kind in OCR_KINDS else None
        planned = adapter(_Adapting(call, args, params, info, where, engine))
        if isinstance(planned, _Direct):
            return self._finish(args, where, planned.ok, planned.message,
                                {"status": "pass" if planned.ok else "fail", "detail": planned.message,
                                 "data": _plain(planned.data)}, resolution)
        result = self._legacy(info, planned, where, args.label)
        detail = cast("dict[str, JsonValue]", _plain(result.as_dict()))
        detail["legacy"] = planned.kind
        if result.status == "error":
            raise NodeFailure("executor_error", f"check.media {kind}: the check could not run: {result.detail}",
                              detail={"kind": kind, "at": _at_value(where), "result": detail}, soft=args.soft)
        return self._finish(args, where, result.ok, result.detail, detail, resolution)

    def _probe(self, path: Path, kind: str, call: ExecContext) -> VideoInfo:
        fps = float(call.clock.fps) if call.clock.fps is not None else DEFAULT_FPS
        try:
            return probe(path, audio_only=kind in AUDIO_KINDS, fps=fps)
        except MediaError as error:
            if "no video stream" in str(error):
                raise _invalid(kind, f"needs a video stream: {error}") from error
            raise NodeFailure("content_unavailable", f"check.media {kind}: {error}") from error

    def _legacy(self, info: VideoInfo, job: _Job, where: _Where, label: str | None) -> CheckResult:
        item = Expectation(index=0, kind=job.kind, t=job.t, params=job.params, tolerance_frames=where.tolerance,
                           label=label or "")
        report = run_checks(info, Expectations(items=[item]), plugins=False)
        return report.results[0]

    def _finish(self, args: MediaCheck, where: _Where, ok: bool, message: str, detail: Mapping[str, JsonValue],
                resolution: dict[str, JsonValue] | None) -> ExecResult:
        at = _at_value(where)
        if not ok:
            name = args.label or args.kind
            raise NodeFailure("assertion", f"media check {name} failed: {message or 'no detail'}",
                              detail={"kind": args.kind, "at": at, "label": args.label, "result": dict(detail)},
                              soft=args.soft)
        value: dict[str, JsonValue] = {"ok": True, "kind": args.kind, "at": at, "detail": dict(detail)}
        if args.label is not None:
            value["label"] = args.label
        return ExecResult(value=value, resolution=resolution, detail={"kind": args.kind, "at": at})

    def _plugin(self, call: ExecContext, args: MediaCheck, spec: CheckSpec, media: MediaFile,
                params: Mapping[str, JsonValue], resolution: dict[str, JsonValue] | None) -> ExecResult:
        runner = _runner(_impl(spec), spec.kind)
        fps = float(media.info.fps) if media.info.fps is not None else (
            float(call.clock.fps) if call.clock.fps is not None else DEFAULT_FPS)
        where = _where(call, args, fps)
        outcome = runner(MediaCheckCall(call=call, kind=spec.kind, path=media.path, media=media.info, params=params,
                                        at=where.at, span=where.span, tolerance_frames=where.tolerance, fps=fps,
                                        label=args.label))
        if isinstance(outcome, bool):
            return self._finish(args, where, outcome, "" if outcome else "the check did not hold",
                                {"status": "pass" if outcome else "fail"}, resolution)
        if isinstance(outcome, Mapping):
            found = cast("Mapping[str, Any]", outcome)
            ok = found.get("ok")
            if not isinstance(ok, bool):
                raise NodeFailure("executor_error", f"media check {spec.kind!r} returned no boolean ok",
                                  detail={"returned": _plain(found)})
            message = str(found.get("message") or found.get("detail") or "")
            detail = _plain(found.get("detail", {}))
            shown: dict[str, JsonValue] = {"status": "pass" if ok else "fail",
                                           "data": detail if detail is not None else {}}
            return self._finish(args, where, ok, message, shown, resolution)
        raise NodeFailure("executor_error",
                          f"media check {spec.kind!r} returned {type(outcome).__name__}; return a bool or a mapping "
                          "with ok")

    def _resolution(self, context: ResolveContext, kind: str,
                    params: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
        spec = _check_spec(context.registry, kind)
        if spec.impl is not None:
            hook = getattr(_impl(spec), "resolution", None)
            if callable(hook):
                return dict(cast("Mapping[str, JsonValue]", hook(context)))
            return None
        if kind not in OCR_KINDS:
            return None
        name, version = _ocr_engine(_engine_name(params))
        return {"engine": name, "version": version, "ocr": {"engine": name, "version": version}}


__all__ = [
    "ADAPTERS",
    "MEDIA_CHECK_EXECUTOR",
    "MEDIA_CHECK_OP",
    "MEDIA_CHECK_VERSION",
    "MediaCheckCall",
    "MediaCheckExecutor",
    "tolerance_frames",
]
