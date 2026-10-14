from __future__ import annotations

import importlib
import inspect
import platform
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import numpy as np
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    ResolveContext,
    builtin_provenance,
)
from eks_harness.ir.catalog.markers import (
    BeatTracker,
    EnergyMarkers,
    FaceMarkers,
    MarkerBase,
    MomentumMarkers,
    MotionMarkers,
    OnsetMarkers,
    SceneMarkers,
    SilenceMarkers,
    STTMarkers,
)
from eks_harness.ir.time import TICKS_PER_SECOND, Time, TimeError
from eks_harness.ir.types import MediaInfo
from eks_harness.media.animate import Timebase
from eks_harness.media.audio.inference import (
    installed,
    require_stems,
    separate,
    stems_resolution,
    version,
)
from eks_harness.media.audio.io import decode, has_audio, write_wav24
from eks_harness.media.files import (
    MediaToolError,
    ffmpeg,
    ffmpeg_version,
    ffprobe,
    ingest,
    probe,
    scratch,
    time_text,
)

SOURCE_OP = "media.source"
MARKERS_OP = "media.markers"
TTS_OP = "media.tts"
STEMS_OP = "media.stems"
SOURCE_EXECUTOR = "eks.media.source"
MARKERS_EXECUTOR = "eks.media.markers"
TTS_EXECUTOR = "eks.media.tts"
STEMS_EXECUTOR = "eks.media.stems"
MEDIA_EXECUTOR_VERSION = "1"
ML_EXTRA = "eks-harness[ml]"
MAC_EXTRA = "eks-harness[mac]"
UNPROBED_KINDS = frozenset({"file", "bundle"})
RENDER_SUFFIX = ".png"
BEAT_ORDER = ("beatnet", "madmom", "librosa")
ONSET_ORDER = ("madmom", "librosa")
CUDA = "cuda"
CPU = "cpu"
MPS = "mps"


@dataclass(frozen=True)
class _Package:
    module: str
    distributions: tuple[str, ...]
    extra: str


PACKAGES: Mapping[str, _Package] = {
    "beatnet": _Package("BeatNet", ("BeatNet",), ML_EXTRA),
    "madmom": _Package("madmom", ("madmom",), ML_EXTRA),
    "librosa": _Package("librosa", ("librosa",), "eks-harness[video]"),
    "faster-whisper": _Package("faster_whisper", ("faster-whisper",), ML_EXTRA),
    "whisperx": _Package("whisperx", ("whisperx",), "whisperx (pip install whisperx)"),
    "mlx-whisper": _Package("mlx_whisper", ("mlx-whisper",), MAC_EXTRA),
    "scenedetect": _Package("scenedetect", ("scenedetect",), "scenedetect (pip install scenedetect[opencv])"),
    "opencv": _Package("cv2", ("opencv-python-headless", "opencv-python", "opencv-contrib-python"), ML_EXTRA),
}
STT_MODELS: Mapping[str, str] = {
    "faster-whisper": "large-v3-turbo",
    "whisperx": "large-v3",
    "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
}
AUDIO_KINDS = frozenset({"beat_tracker", "stt_markers", "silence_markers", "onset_markers", "energy_markers",
                         "momentum_markers"})
VIDEO_KINDS = frozenset({"scene_markers", "face_markers", "motion_markers"})


@dataclass(frozen=True)
class Choice:
    source: MarkerBase
    backend: str
    version: str
    extra: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    def record(self) -> dict[str, JsonValue]:
        return {"backend": self.backend, "version": self.version, **self.extra}


def _version_of(package: _Package) -> str:
    for distribution in package.distributions:
        found = version(distribution)
        if found != "unknown":
            return found
    return "unknown"


def _available(backend: str) -> bool:
    if backend == "mlx-whisper" and not (platform.system() == "Darwin" and platform.machine() == "arm64"):
        return False
    return installed(PACKAGES[backend].module)


def _unavailable(source: MarkerBase, backend: str, why: str = "") -> NodeFailure:
    package = PACKAGES[backend]
    reason = why or f"{backend} is not installed on this machine"
    return NodeFailure("resolution_unavailable",
                       f"marker source {source.name!r} ({source.kind}) needs {backend}: {reason}; install "
                       f"{package.extra}", detail={"source": source.name, "backend": backend, "extra": package.extra})


def _first(source: MarkerBase, order: Sequence[str]) -> str:
    for backend in order:
        if _available(backend):
            return backend
    names = ", ".join(order)
    raise NodeFailure("resolution_unavailable",
                      f"marker source {source.name!r} ({source.kind}) has no backend on this machine (tried {names}); "
                      f"install {PACKAGES[order[-1]].extra}", detail={"source": source.name, "tried": list(order)})


def _require(source: MarkerBase, backend: str) -> Choice:
    if not _available(backend):
        raise _unavailable(source, backend)
    return Choice(source, backend, _version_of(PACKAGES[backend]))


def _cuda_devices(backend: str) -> int:
    try:
        if backend == "faster-whisper":
            ctranslate2: Any = importlib.import_module("ctranslate2")
            return int(ctranslate2.get_cuda_device_count())
        torch: Any = importlib.import_module("torch")
        return int(torch.cuda.device_count()) if bool(torch.cuda.is_available()) else 0
    except ImportError:
        return 0


def _stt_choice(source: STTMarkers) -> Choice:
    backend = source.backend
    if not _available(backend):
        raise _unavailable(source, backend)
    model = source.model or STT_MODELS[backend]
    if backend == "mlx-whisper":
        device = source.device or MPS
        compute_type = source.compute_type or "float16"
        if device != MPS or compute_type != "float16":
            raise _unavailable(source, backend, f"the mlx-whisper integration runs on mps in float16, not on {device} "
                                                f"in {compute_type}")
    else:
        cuda = _cuda_devices(backend) > 0
        device = source.device or (CUDA if cuda else CPU)
        if device == CUDA and not cuda:
            raise _unavailable(source, backend, "the source pins device cuda and this machine has no CUDA device")
        compute_type = source.compute_type or ("float16" if device == CUDA else "int8")
    return Choice(source, backend, _version_of(PACKAGES[backend]),
                  {"model": model, "device": device, "compute_type": compute_type})


def choose(source: MarkerBase) -> Choice:
    if isinstance(source, BeatTracker):
        if source.backend != "auto":
            return _require(source, source.backend)
        return _require(source, _first(source, BEAT_ORDER))
    if isinstance(source, STTMarkers):
        return _stt_choice(source)
    if isinstance(source, OnsetMarkers):
        return _require(source, _first(source, ONSET_ORDER))
    if isinstance(source, EnergyMarkers | SilenceMarkers | MomentumMarkers):
        return _require(source, "librosa")
    if isinstance(source, SceneMarkers):
        return _require(source, "scenedetect")
    if isinstance(source, MotionMarkers | FaceMarkers):
        return _require(source, "opencv")
    raise NodeFailure("args_invalid", f"marker source {source.name!r} has kind {source.kind!r}, which media.markers "
                                      "has no analyser for")


def _sources(args: object) -> tuple[MarkerBase, ...]:
    found = cast(Sequence[object], getattr(args, "sources", ()))
    return tuple(source for source in found if isinstance(source, MarkerBase))


def marker_resolution(sources: Sequence[MarkerBase]) -> tuple[list[Choice], dict[str, JsonValue]]:
    choices = [choose(source) for source in sources]
    record: dict[str, JsonValue] = {choice.source.name: choice.record() for choice in choices}
    return choices, {"markers": record}


def _ticks(seconds: float) -> int:
    return max(0, round(Fraction(repr(float(seconds))) * TICKS_PER_SECOND))


def _times(values: Sequence[float]) -> list[JsonValue]:
    return [time_text(ticks) for ticks in sorted(_ticks(value) for value in values)]


def _seconds(call: ExecContext, value: Time, where: str) -> float:
    if value.is_concrete:
        return value.ticks / TICKS_PER_SECOND
    try:
        return Timebase(call.time()).local_seconds(value)
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"{where}: {error}") from error


def _backend_failure(choice: Choice, error: Exception) -> NodeFailure:
    source = choice.source
    return NodeFailure("executor_error",
                       f"marker source {source.name!r} ({source.kind}) failed with {choice.backend}: "
                       f"{type(error).__name__}: {error}", detail={"source": source.name, "backend": choice.backend})


class _Audio:
    def __init__(self, path: Path, directory: Path) -> None:
        self.path = path
        self.directory = directory
        self._wav: Path | None = None

    def wav(self, source: MarkerBase) -> Path:
        if self._wav is None:
            if not has_audio(self.path):
                raise NodeFailure("args_invalid", f"marker source {source.name!r} ({source.kind}) analyses audio, and "
                                                  f"{self.path.name} has no audio stream")
            out = self.directory / "markers-audio.wav"
            ffmpeg(["-i", str(self.path)], ["-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "pcm_f32le", str(out)])
            self._wav = out
        return self._wav


def _beats(choice: Choice, source: BeatTracker, wav: Path) -> dict[str, JsonValue]:
    from eks_harness.video.ir.markers import BeatTracker as LegacyBeatTracker
    from eks_harness.video.plugins.builtin.markers.beat_tracker import (
        BeatTrackerExtractor,
    )

    legacy = LegacyBeatTracker(name=source.name, source=str(wav), streams=list(source.streams), bpm=source.bpm,
                               backend=cast(Any, choice.backend))
    payload = BeatTrackerExtractor()._run_backend(cast(Any, choice.backend), wav, legacy)
    return {stream: _times(payload.get(stream, [])) for stream in source.streams}


def _stt(choice: Choice, source: STTMarkers, wav: Path) -> dict[str, JsonValue]:
    from eks_harness.video.plugins.builtin.captioners import (
        FasterWhisperCaptioner,
        MlxWhisperCaptioner,
        WhisperXCaptioner,
    )

    makers: dict[str, Callable[[], Any]] = {"faster-whisper": FasterWhisperCaptioner,
                                            "whisperx": WhisperXCaptioner, "mlx-whisper": MlxWhisperCaptioner}
    options: dict[str, Any] = {key: choice.extra[key] for key in ("model", "device", "compute_type")}
    if source.language is not None:
        options["language"] = source.language
    words = cast(list[Any], makers[choice.backend]().transcribe(wav, options))
    spoken: list[JsonValue] = [{"text": str(word.text), "start": time_text(_ticks(float(word.start))),
                                "end": time_text(_ticks(float(word.end)))} for word in words]
    return {source.name: _times([float(word.start) for word in words]), "words": spoken}


def _momentum(call: ExecContext, source: MomentumMarkers, wav: Path) -> dict[str, JsonValue]:
    from eks_harness.video.ir.markers import MomentumMarkers as LegacyMomentum
    from eks_harness.video.plugins.builtin.markers.momentum_markers import (
        MOMENTUM_EVENTS,
        _to_marker_set,
        analyze_momentum,
    )

    where = f"marker source {source.name!r}"
    snap = source.snap_window
    legacy = LegacyMomentum(
        name=source.name, source=str(wav),
        smoothing=_seconds(call, source.smoothing, f"{where} smoothing"),
        drop_threshold=source.drop_threshold, break_threshold=source.break_threshold,
        min_build=_seconds(call, source.min_build, f"{where} min_build"),
        min_section=_seconds(call, source.min_section, f"{where} min_section"),
        snap_window=_seconds(call, snap, f"{where} snap_window") if snap is not None else 0.0)
    found = _to_marker_set(legacy, analyze_momentum(wav, legacy))
    return {event: _times(found.streams.get(f"{source.name}.{event}", [])) for event in MOMENTUM_EVENTS}


def _analyse(call: ExecContext, choice: Choice, media: Path, audio: _Audio) -> dict[str, JsonValue]:
    from eks_harness.video.plugins.builtin.markers import (
        energy_markers,
        face_markers,
        motion_markers,
        onset_markers,
        scene_markers,
        silence_markers,
    )

    source = choice.source
    where = f"marker source {source.name!r}"
    target = audio.wav(source) if source.kind in AUDIO_KINDS else media
    if isinstance(source, BeatTracker):
        return _beats(choice, source, target)
    if isinstance(source, STTMarkers):
        return _stt(choice, source, target)
    if isinstance(source, MomentumMarkers):
        return _momentum(call, source, target)
    if isinstance(source, OnsetMarkers):
        times = onset_markers._run_backend(cast(Any, choice.backend), target, source.sensitivity)
    elif isinstance(source, EnergyMarkers):
        times = energy_markers._detect_energy_peaks(target, source.band, source.percentile)
    elif isinstance(source, SilenceMarkers):
        times = silence_markers._detect_silences(target, source.threshold_db,
                                                 _seconds(call, source.min_duration, f"{where} min_duration"))
    elif isinstance(source, SceneMarkers):
        times = scene_markers._detect_scenes(target, source.threshold)
    elif isinstance(source, MotionMarkers):
        times = motion_markers._detect_motion_frames(target, source.threshold)
    elif isinstance(source, FaceMarkers):
        times = face_markers._detect_face_runs(target, source.min_size)
    else:
        raise NodeFailure("args_invalid", f"{where} has kind {source.kind!r}, which media.markers has no analyser for")
    return {source.name: _times(times)}


def _video_check(choice: Choice, info: MediaInfo) -> None:
    source = choice.source
    if source.kind in VIDEO_KINDS and info.kind not in ("video", "image"):
        raise NodeFailure("args_invalid", f"marker source {source.name!r} ({source.kind}) analyses video frames, and "
                                          f"{info.uri} is {info.kind}")


def run_markers(call: ExecContext) -> ExecResult:
    choices, resolution = marker_resolution(_sources(call.args))
    found = call.media(call.data.get("media"))
    out: dict[str, JsonValue] = {}
    with scratch(call) as directory:
        audio = _Audio(found.path, directory)
        for index, choice in enumerate(choices):
            call.check_cancelled()
            _video_check(choice, found.info)
            call.progress(index / max(1, len(choices)), f"analysing {choice.source.name}")
            try:
                out[choice.source.name] = _analyse(call, choice, found.path, audio)
            except NodeFailure:
                raise
            except Exception as error:
                raise _backend_failure(choice, error) from error
    call.progress(1.0, "markers done")
    return ExecResult(value=out, resolution=resolution)


def resolve_markers(context: ResolveContext) -> Mapping[str, JsonValue]:
    return marker_resolution(_sources(context.args))[1]


def _artifact_id(uri: str) -> str | None:
    if not uri.startswith("artifact:"):
        return None
    return uri.removeprefix("artifact:").split("@", 1)[0]


def _load_renderer(call: ExecContext, kind: str) -> Callable[..., Any]:
    try:
        spec = call.registry.renderer(kind)
    except Exception as error:
        raise NodeFailure("args_invalid", f"media.source names renderer {kind!r}: {error}") from error
    module_name, _colon, qualname = spec.impl.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as error:
        raise NodeFailure("executor_error", f"renderer {kind!r} ({spec.impl}) cannot be loaded: {error}") from error
    if not callable(target):
        raise NodeFailure("executor_error", f"renderer {kind!r} ({spec.impl}) is not callable")
    return cast(Callable[..., Any], target)


def _render(call: ExecContext, path: Path, uri: str, kind: str) -> JsonValue:
    render = _load_renderer(call, kind)
    options: dict[str, Any] = {}
    if "size" in inspect.signature(render).parameters:
        info = probe(path, uri)
        if info.width is None or info.height is None:
            raise NodeFailure("args_invalid", f"renderer {kind!r} needs the size of {uri}, and ffprobe reports none; "
                                              "give the source a raster size or a renderer that takes no size")
        options["size"] = (info.width, info.height)
    with scratch(call) as directory:
        out = directory / f"{path.stem}{RENDER_SUFFIX}"
        try:
            produced = render(path, out, **options)
        except Exception as error:
            raise NodeFailure("executor_error", f"renderer {kind!r} failed on {uri}: {type(error).__name__}: "
                                                f"{error}") from error
        result = Path(produced) if isinstance(produced, str | Path) else out
        if not result.is_file():
            raise NodeFailure("executor_error", f"renderer {kind!r} wrote no file for {uri}")
        made = probe(result, result.name)
        return ingest(call, result, kind=made.kind, name=result.name, meta={"source": uri, "renderer": kind})


def run_source(call: ExecContext) -> ExecResult:
    uri = str(call.data.get("uri"))
    kind = call.data.get("kind")
    renderer = call.data.get("renderer")
    found = call.media({"uri": uri, "kind": kind if isinstance(kind, str) else "file"})
    artifact = _artifact_id(uri)
    if isinstance(renderer, str):
        return ExecResult(value=_render(call, found.path, uri, renderer))
    if isinstance(kind, str) and kind in UNPROBED_KINDS:
        info = MediaInfo.model_validate({"uri": uri, "kind": kind, "artifact": artifact})
    else:
        try:
            info = probe(found.path, uri, kind=kind if isinstance(kind, str) else None, artifact=artifact)
        except MediaToolError as error:
            if kind is not None or error.code != "content_unavailable":
                raise
            info = MediaInfo.model_validate({"uri": uri, "kind": "file", "artifact": artifact})
    return ExecResult(value=info.model_dump(mode="json", by_alias=True, exclude_none=True))


def _speech_unavailable(backend: str) -> NodeFailure:
    return NodeFailure("resolution_unavailable",
                       f"media.tts backend {backend!r} is not available: this eks-harness build has no speech "
                       "synthesis integration yet, so media.tts cannot run; use a recorded voice file as an audio track",
                       detail={"backend": backend})


def _speech_backend(data: Mapping[str, JsonValue]) -> str:
    backend = data.get("backend")
    return backend if isinstance(backend, str) else "kokoro"


def run_tts(call: ExecContext) -> ExecResult:
    raise _speech_unavailable(_speech_backend(call.data))


def resolve_tts(context: ResolveContext) -> Mapping[str, JsonValue]:
    raise _speech_unavailable(_speech_backend(context.data))


def _stem_args(data: Mapping[str, JsonValue]) -> tuple[list[str], str]:
    stems = data.get("stems")
    model = data.get("model")
    names = [str(stem) for stem in cast(list[JsonValue], stems)] if isinstance(stems, list) else []
    return names, model if isinstance(model, str) else "htdemucs"


def resolve_stems(context: ResolveContext) -> Mapping[str, JsonValue]:
    require_stems("media.stems")
    return stems_resolution(_stem_args(context.data)[1])


def run_stems(call: ExecContext) -> ExecResult:
    require_stems("media.stems")
    names, model = _stem_args(call.data)
    resolution = stems_resolution(model)
    found = call.media(call.data.get("media"))
    with scratch(call) as directory:
        separated = separate(found.path, names, model, directory)
        streams = cast(list[dict[str, Any]], ffprobe(separated).get("streams") or [])
        audio: dict[str, Any] = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        rate = int(audio.get("sample_rate") or 44100)
        channels = int(audio.get("channels") or 2)
        samples = decode(separated, rate=rate, channels=channels, directory=directory, what="the separated stems")
        out = directory / f"stems-{'-'.join(names)}.wav"
        write_wav24(out, np.asarray(samples, dtype=np.float64), rate)
        value = ingest(call, out, kind="audio", name=out.name, meta={"stems": cast(JsonValue, names), "model": model})
    return ExecResult(value=value, resolution=resolution)


def _runtime(*names: str) -> Callable[[], str]:
    def runtime() -> str:
        try:
            parts = [f"ffmpeg {ffmpeg_version()}"]
        except NodeFailure:
            parts = ["ffmpeg missing"]
        parts.extend(f"{name} {version(name)}" for name in names)
        return "; ".join(parts)

    return runtime


def media_executor_specs() -> list[ExecutorSpec]:
    provenance = builtin_provenance()
    return [
        ExecutorSpec(SOURCE_EXECUTOR, MEDIA_EXECUTOR_VERSION, ops=(SOURCE_OP,), run=run_source,
                     runtime_version=_runtime(), provenance=provenance,
                     description="Resolves a media file or artifact and probes it with ffprobe"),
        ExecutorSpec(MARKERS_EXECUTOR, MEDIA_EXECUTOR_VERSION, ops=(MARKERS_OP,), run=run_markers,
                     resolver=resolve_markers, runtime_version=_runtime("numpy", "scipy", "librosa"),
                     provenance=provenance,
                     description="Extracts beat, onset, energy, silence, momentum, scene, motion, face and speech "
                                 "markers with the video engine's analysers"),
        ExecutorSpec(TTS_EXECUTOR, MEDIA_EXECUTOR_VERSION, ops=(TTS_OP,), run=run_tts, resolver=resolve_tts,
                     provenance=provenance, description="Speech synthesis; needs a speech backend extra"),
        ExecutorSpec(STEMS_EXECUTOR, MEDIA_EXECUTOR_VERSION, ops=(STEMS_OP,), run=run_stems, resolver=resolve_stems,
                     runtime_version=_runtime("numpy"), provenance=provenance,
                     description="Separates stems with Demucs and mixes the chosen ones into one audio file"),
    ]


__all__ = [
    "MARKERS_EXECUTOR",
    "MEDIA_EXECUTOR_VERSION",
    "SOURCE_EXECUTOR",
    "STEMS_EXECUTOR",
    "TTS_EXECUTOR",
    "Choice",
    "choose",
    "marker_resolution",
    "media_executor_specs",
    "resolve_markers",
    "resolve_stems",
    "resolve_tts",
    "run_markers",
    "run_source",
    "run_stems",
    "run_tts",
]
