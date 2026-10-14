from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import numpy as np
import scipy
from pydantic import JsonValue, ValidationError

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    ResolveContext,
    builtin_provenance,
)
from eks_harness.ir.ops.audio import AudioTrack, Mix
from eks_harness.ir.refs import REF_KEY
from eks_harness.ir.time import TICKS_PER_SECOND
from eks_harness.media.audio.inference import (
    denoise,
    denoise_resolution,
    require_denoise,
    require_stems,
    separate,
    stems_resolution,
)
from eks_harness.media.audio.io import write_wav24
from eks_harness.media.audio.mix import Prepare, render_mix
from eks_harness.media.files import ffmpeg_version, ingest, scratch, time_text

AUDIO_EXECUTOR = "eks.audio"
AUDIO_EXECUTOR_VERSION = "1"
MIX_OP = "audio.mix"
STEM_MODEL = "htdemucs"
OUTPUT_NAME = "mix.wav"


def typed_mix(data: Mapping[str, JsonValue]) -> tuple[Mix, list[JsonValue]]:
    raw = data.get("mix")
    if not isinstance(raw, Mapping):
        raise NodeFailure("args_invalid", "audio.mix needs a mix object")
    tracks = raw.get("tracks")
    listed = cast(list[JsonValue], tracks) if isinstance(tracks, list) else []
    media: list[JsonValue] = []
    placeholders: list[JsonValue] = []
    for index, track in enumerate(listed):
        if not isinstance(track, Mapping):
            raise NodeFailure("args_invalid", f"audio.mix track {index} is not an object")
        media.append(track.get("media"))
        placeholders.append({**track, "media": {REF_KEY: f"track{index}"}})
    try:
        mix = Mix.model_validate({**raw, "tracks": placeholders})
    except ValidationError as error:
        raise NodeFailure("args_invalid", f"invalid audio mix: {error.errors()[0]['msg']}") from error
    return mix, media


def _needs(mix: Mix) -> tuple[bool, bool]:
    return any(track.stems for track in mix.tracks), any(track.denoise is not None for track in mix.tracks)


def mix_resolution(mix: Mix) -> dict[str, JsonValue] | None:
    stems, cleaned = _needs(mix)
    if stems:
        require_stems("audio.mix track stems")
    if cleaned:
        require_denoise("audio.mix track denoise")
    parts = ([stems_resolution(STEM_MODEL)] if stems else []) + ([denoise_resolution()] if cleaned else [])
    if not parts:
        return None
    return {"inference": {
        "device": "+".join(dict.fromkeys(str(part["device"]) for part in parts)),
        "model": "+".join(str(part["model"]) for part in parts),
        "weights": "+".join(str(part["weights"]) for part in parts),
        "backend_version": "; ".join(str(part["backend_version"]) for part in parts),
    }}


def _prepare(directory: Path) -> Prepare:
    def prepare(track: AudioTrack, path: Path) -> Path:
        found = path
        if track.stems:
            found = separate(found, track.stems, STEM_MODEL, directory)
        if track.denoise is not None:
            found = denoise(found, track.denoise.attenuation_limit_db, directory)
        return found

    return prepare


def _finite(value: float) -> JsonValue:
    return round(value, 4) if math.isfinite(value) else None


def runtime_version() -> str:
    return f"ffmpeg {ffmpeg_version()}; numpy {np.__version__}; scipy {scipy.__version__}"


class AudioMixExecutor:
    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(AUDIO_EXECUTOR, AUDIO_EXECUTOR_VERSION, ops=(MIX_OP,), run=self.run, resolver=self.resolve,
                            locations=frozenset({"node"}), runtime_version=runtime_version,
                            provenance=builtin_provenance(),
                            description="Renders the audio mix graph to a 24-bit WAV: EQ, compression, ducking, "
                                        "reverb, loudness normalisation and a true-peak limiter")

    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        return mix_resolution(typed_mix(context.data)[0]) or {}

    def run(self, call: ExecContext) -> ExecResult:
        mix, media = typed_mix(call.data)
        resolution = mix_resolution(mix)
        files = [call.media(value).path for value in media]
        context = call.time()
        with scratch(call) as directory:
            result = render_mix(mix, files, context=context, seed=call.seed, directory=directory,
                                prepare=_prepare(directory), check=call.check_cancelled)
            out = directory / OUTPUT_NAME
            write_wav24(out, result.samples, result.rate)
            value = ingest(call, out, kind="audio", name=OUTPUT_NAME)
        frames = result.samples.shape[0]
        detail: dict[str, JsonValue] = {
            "loudness": _finite(result.loudness),
            "true_peak": _finite(result.true_peak),
            "sample_rate": result.rate,
            "channels": int(result.samples.shape[1]),
            "duration": time_text(round(frames * TICKS_PER_SECOND / result.rate)),
        }
        return ExecResult(value=value, resolution=resolution, detail=detail)


__all__ = ["AUDIO_EXECUTOR", "AUDIO_EXECUTOR_VERSION", "AudioMixExecutor", "mix_resolution", "runtime_version",
           "typed_mix"]
