from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Iterator, Mapping, Sequence
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import JsonValue

from eks_harness.engine.cache import ContentResolver
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    ExecContext,
    ExecResult,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.time import TempoMap, TimeContext
from eks_harness.ir.types import Artifact
from eks_harness.media.audio.inference import installed
from eks_harness.media.audio.limiter import limit
from eks_harness.media.audio.loudness import integrated_loudness, true_peak_db
from eks_harness.ops.audio import AUDIO_EXECUTOR, AudioMixExecutor

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not on PATH")

RATE = 48000
NODE_HASH = "sha256:" + "a" * 64
PLAN_HASH = "sha256:" + "b" * 64
Floats = NDArray[np.float64]


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, tuple[Artifact, Path]] = {}

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        artifact_id = f"A{len(self.items) + 1:05d}"
        target = self.root / "store" / artifact_id / (name or path.name)
        target.parent.mkdir(parents=True)
        if move:
            shutil.move(path, target)
        else:
            shutil.copyfile(path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        artifact = Artifact(id=artifact_id, kind=kind or "file", name=target.name, size=target.stat().st_size,
                            sha256=f"sha256:{digest}")
        self.items[artifact_id] = (artifact, target)
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        found = self.items.get(artifact_id)
        return found[0] if found is not None else None

    def file(self, artifact_id: str) -> Path | None:
        found = self.items.get(artifact_id)
        return found[1] if found is not None else None

    def scratch(self) -> Path:
        base = self.root / "scratch"
        base.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(dir=base))


class Sink:
    def __init__(self) -> None:
        self.artifacts: list[Artifact] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        return None

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)


def lavfi(path: Path, source: str, seconds: float, *, channels: int = 1) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}:duration={seconds}",
                    "-ac", str(channels), "-ar", str(RATE), "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def sine(tmp_path: Path, name: str, frequency: float, seconds: float, *, volume_db: float = -12.0) -> Path:
    path = tmp_path / f"{name}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:sample_rate={RATE}:duration={seconds}", "-af",
                    f"volume={volume_db}dB", "-ac", "1", "-c:a", "pcm_s24le", str(path)], check=True)
    return path


def noise(tmp_path: Path, name: str, seconds: float, *, seed: int, amplitude: float = 0.3,
          color: str = "pink") -> Path:
    return lavfi(tmp_path / f"{name}.wav",
                 f"anoisesrc=color={color}:amplitude={amplitude}:seed={seed}:sample_rate={RATE}", seconds,
                 channels=2)


def media(path: Path) -> JsonValue:
    return {"uri": f"file:{path}", "kind": "audio"}


def mix_call(tmp_path: Path, mix: dict[str, JsonValue], *, context: TimeContext | None = None) -> ExecContext:
    registry = default_registry()
    spec = registry.op("audio.mix")
    data: dict[str, JsonValue] = {"mix": mix}
    resolved = validate_args(spec, data)
    node = Node(id="n0000000001", op="audio.mix", args=data, out=spec.out, hash=NODE_HASH)
    plan = Plan(kind="edit", name="mix", nodes=[node], hash=PLAN_HASH)
    info = RunInfo(id="01JMIXTESTRUN000000000000", project="acme/mix", session="mix", plan_hash=PLAN_HASH,
                   plan_kind="edit", plan_name="mix", created_at=time.time())
    store = Store(tmp_path)
    sink = Sink()
    clock = context or TimeContext.static(fps=Fraction(30), tempo=TempoMap(bpm=120.0))
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    content = ContentResolver(store, tree=tmp_path, base=".", plugin_roots={}, scratch=downloads)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=None, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=sink,
                       artifacts=ArtifactSink(store, run=info, node_id=node.id, instance="", op="audio.mix",
                                              sink=sink),
                       seed=7, clock=plan.clock, deadline=None, cancel=threading.Event(),
                       time_context=lambda: clock, content=content)


def render(tmp_path: Path, mix: dict[str, JsonValue], *,
           context: TimeContext | None = None) -> tuple[ExecResult, Path]:
    call = mix_call(tmp_path, mix, context=context)
    result = AudioMixExecutor().run(call)
    assert isinstance(result.value, dict)
    artifact = result.value["artifact"]
    assert isinstance(artifact, str)
    path = call.artifacts.file(artifact)
    assert path is not None
    return result, path


def read(path: Path) -> Floats:
    with wave.open(str(path), "rb") as found:
        assert found.getsampwidth() == 3
        channels = found.getnchannels()
        raw = np.frombuffer(found.readframes(found.getnframes()), dtype=np.uint8).reshape(-1, 3)
    padded = np.zeros((raw.shape[0], 4), dtype=np.uint8)
    padded[:, 1:] = raw
    values = padded.view("<i4").reshape(-1).astype(np.float64) / 2147483648.0
    return values.reshape(-1, channels)


def reference(path: Path) -> tuple[float, float]:
    done = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true",
                           "-f", "null", "-"], capture_output=True, text=True, check=True)
    summary = done.stderr[done.stderr.rindex("Summary:"):]
    loudness = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    peak = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    assert loudness is not None and peak is not None
    return float(loudness.group(1)), float(peak.group(1))


def rms_db(samples: Floats) -> float:
    return 10.0 * float(np.log10(np.mean(samples * samples) + 1e-30))


def lowpass_rms_db(samples: Floats, start: float, stop: float, cutoff: float = 1000.0) -> float:
    window = samples[round(start * RATE):round(stop * RATE)].mean(axis=1)
    spectrum = np.fft.rfft(window)
    frequencies = np.fft.rfftfreq(window.shape[0], 1.0 / RATE)
    spectrum[frequencies > cutoff] = 0
    return rms_db(np.fft.irfft(spectrum, n=window.shape[0])[:, None])


def test_loudness_meter_matches_ffmpeg_ebur128(tmp_path: Path) -> None:
    path = noise(tmp_path, "pink", 6.0, seed=11, amplitude=0.2)
    samples = read_any(path)
    ours = integrated_loudness(samples, RATE)
    theirs, peak = reference(path)
    assert abs(ours - theirs) < 0.2
    assert abs(true_peak_db(samples) - peak) < 0.3


def test_loudness_meter_reads_the_ebu_reference_sine() -> None:
    seconds = np.arange(20 * RATE, dtype=np.float64) / RATE
    tone = 10.0 ** (-23.0 / 20.0) * np.sin(2.0 * np.pi * 1000.0 * seconds)
    assert abs(integrated_loudness(np.stack([tone, tone], axis=1), RATE) - -23.0) < 0.1
    assert integrated_loudness(np.zeros((RATE, 2)), RATE) == float("-inf")


def test_limiter_keeps_the_true_peak_under_the_ceiling() -> None:
    rng = np.random.default_rng(1)
    loud = rng.normal(0.0, 0.4, (5 * RATE, 2))
    limited = limit(loud, RATE, -1.0)
    assert true_peak_db(limited) <= -1.0
    assert true_peak_db(limited) > -1.2
    quiet = loud * 0.1
    assert np.array_equal(limit(quiet, RATE, -1.0), quiet)


def read_any(path: Path) -> Floats:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f64le", "-c:a", "pcm_f64le", "-"],
                         capture_output=True, check=True).stdout
    with wave.open(str(path), "rb") as found:
        channels = found.getnchannels()
    return np.frombuffer(raw, dtype="<f8").reshape(-1, channels).astype(np.float64)


def test_normalises_integrated_loudness_to_the_target(tmp_path: Path) -> None:
    song = noise(tmp_path, "song", 8.0, seed=3, amplitude=0.05)
    result, path = render(tmp_path, {"tracks": [{"name": "song", "media": media(song)}],
                                     "master": {"loudness": -16.0, "true_peak": -1.0}})
    measured, peak = reference(path)
    assert abs(measured - -16.0) < 0.5
    assert peak <= -0.9
    assert isinstance(result.detail["loudness"], float)
    assert abs(result.detail["loudness"] - measured) < 0.3
    assert result.resolution is None


def test_true_peak_limiter_holds_the_ceiling(tmp_path: Path) -> None:
    loud = noise(tmp_path, "loud", 6.0, seed=5, amplitude=0.9, color="white")
    result, path = render(tmp_path, {"tracks": [{"name": "loud", "media": media(loud)}],
                                     "master": {"loudness": -6.0, "true_peak": -2.0}})
    samples = read(path)
    assert true_peak_db(samples) <= -2.0 + 1e-3
    _measured, peak = reference(path)
    assert peak <= -1.9
    assert isinstance(result.detail["true_peak"], float)
    assert result.detail["true_peak"] <= -2.0


def test_writes_a_24_bit_wav_at_the_mix_rate_and_channels(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 1.0)
    result, path = render(tmp_path, {"tracks": [{"name": "tone", "media": media(tone)}], "sample_rate": 44100,
                                     "channels": 1, "duration": "1.5s"})
    with wave.open(str(path), "rb") as found:
        assert (found.getsampwidth(), found.getframerate(), found.getnchannels()) == (3, 44100, 1)
        assert found.getnframes() == 66150
    assert isinstance(result.value, dict)
    assert result.value["kind"] == "audio"
    assert result.detail["sample_rate"] == 44100


def test_placement_source_span_and_duration_padding(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 4.0)
    _result, path = render(tmp_path, {"tracks": [{"name": "tone", "media": media(tone), "start": "1s",
                                                  "src": {"start": "1s", "end": "2s"}}],
                                      "duration": "3s", "master": {"limiter": False}})
    samples = read(path)
    assert samples.shape[0] == 3 * RATE
    assert np.max(np.abs(samples[: RATE - 10])) == 0.0
    assert rms_db(samples[RATE + 100: 2 * RATE - 100]) > -40.0
    assert np.max(np.abs(samples[2 * RATE + 10:])) == 0.0


def test_beat_anchored_start_resolves_through_the_clock(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 1.0)
    _result, path = render(tmp_path, {"tracks": [{"name": "tone", "media": media(tone), "start": "beat:2"}],
                                      "master": {"limiter": False}})
    samples = read(path)
    assert samples.shape[0] == 2 * RATE
    assert np.max(np.abs(samples[: RATE - 10])) == 0.0


def test_ducking_lowers_the_music_while_the_voice_plays(tmp_path: Path) -> None:
    music = sine(tmp_path, "music", 220.0, 5.0)
    voice = sine(tmp_path, "voice", 3000.0, 1.5)
    _result, path = render(tmp_path, {"tracks": [
        {"name": "music", "media": media(music), "ducks": [{"under": "voice", "amount_db": -12.0,
                                                             "threshold_db": -40.0}]},
        {"name": "voice", "media": media(voice), "start": "2s"},
    ], "master": {"limiter": False}})
    samples = read(path)
    before = lowpass_rms_db(samples, 0.5, 1.5)
    during = lowpass_rms_db(samples, 2.5, 3.3)
    after = lowpass_rms_db(samples, 4.3, 5.0)
    assert abs((before - during) - 12.0) < 1.0
    assert abs(before - after) < 0.5


def test_equal_power_pan_and_animated_pan(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 2.0)
    _left, left = render(tmp_path / "left", {"tracks": [{"name": "tone", "media": media(tone), "pan": -1.0}],
                                             "master": {"limiter": False}})
    hard = read(left)
    assert rms_db(hard[:, 1:]) < -200.0
    assert abs(rms_db(hard[:, :1]) - rms_db(read_any(tone))) < 0.05
    _center, center = render(tmp_path / "center", {"tracks": [{"name": "tone", "media": media(tone)}],
                                                   "master": {"limiter": False}})
    middle = read(center)
    assert abs(rms_db(middle[:, :1]) - rms_db(middle[:, 1:])) < 1e-6
    sweep: JsonValue = {"keys": [{"t": "0s", "v": -1.0}, {"t": "2s", "v": 1.0}]}
    _moving, moving = render(tmp_path / "moving", {"tracks": [{"name": "tone", "media": media(tone), "pan": sweep}],
                                                   "master": {"limiter": False}})
    swept = read(moving)
    early, late = swept[: RATE // 4], swept[-RATE // 4:]
    assert rms_db(early[:, :1]) > rms_db(early[:, 1:]) + 10.0
    assert rms_db(late[:, 1:]) > rms_db(late[:, :1]) + 10.0


def test_fades_ramp_in_and_out(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 3.0)
    _result, path = render(tmp_path, {"tracks": [{"name": "tone", "media": media(tone),
                                                  "fades": {"fade_in": "1s", "fade_out": "1s"}}],
                                      "master": {"limiter": False}})
    samples = read(path)[:, 0]
    assert abs(samples[0]) < 1e-6
    assert abs(samples[-1]) < 1e-3
    head = rms_db(samples[: RATE // 10, None])
    body = rms_db(samples[int(1.4 * RATE): int(1.6 * RATE), None])
    tail = rms_db(samples[-RATE // 10:, None])
    assert head < body - 15.0
    assert tail < body - 15.0


def test_eq_compressor_and_gain(tmp_path: Path) -> None:
    high = sine(tmp_path, "high", 5000.0, 1.0, volume_db=-6.0)
    _filtered, filtered = render(tmp_path / "eq", {"tracks": [{"name": "high", "media": media(high), "eq": {
        "bands": [{"frequency_hz": 500.0, "type": "low_pass", "q": 0.707}]}}], "master": {"limiter": False}})
    assert rms_db(read(filtered)) < rms_db(read_any(high)) - 30.0
    loud = sine(tmp_path, "loud", 440.0, 2.0, volume_db=12.0)
    _squashed, squashed = render(tmp_path / "comp", {"tracks": [{"name": "loud", "media": media(loud), "pan": 0.0,
                                                                 "compressor": {"threshold_db": -20.0,
                                                                                "ratio": 4.0}}],
                                                     "channels": 1, "master": {"limiter": False}})
    level = rms_db(read(squashed)[RATE:])
    assert rms_db(read_any(loud)) > -12.0
    expected = -20.0 + (rms_db(read_any(loud)) + 20.0) / 4.0
    assert abs(level - expected) < 0.5
    _quiet, quiet = render(tmp_path / "gain", {"tracks": [{"name": "loud", "media": media(loud), "gain": -6.0}],
                                               "channels": 1, "master": {"limiter": False}})
    assert abs(rms_db(read(quiet)) - (rms_db(read_any(loud)) - 6.0)) < 0.01


def test_reverb_adds_a_tail(tmp_path: Path) -> None:
    click = sine(tmp_path, "click", 1000.0, 0.2)
    _result, path = render(tmp_path, {"tracks": [{"name": "click", "media": media(click),
                                                  "reverb": {"wet": 0.5, "room_size": 0.7}}],
                                      "duration": "2s", "master": {"limiter": False}})
    samples = read(path)
    assert rms_db(samples[int(0.4 * RATE): int(0.8 * RATE)]) > -80.0


def full_mix(tmp_path: Path) -> dict[str, JsonValue]:
    song = noise(tmp_path, "song", 5.0, seed=21, amplitude=0.2)
    voice = sine(tmp_path, "voice", 600.0, 2.0)
    return {
        "tracks": [
            {"name": "song", "media": media(song), "gain": {"keys": [{"t": "0s", "v": -12.0}, {"t": "2s", "v": 0.0}]},
             "eq": {"bands": [{"frequency_hz": 120.0, "type": "low_shelf", "gain_db": 3.0},
                              {"frequency_hz": 4000.0, "gain_db": {"keys": [{"t": "0s", "v": 0.0},
                                                                            {"t": "3s", "v": -6.0}]}}]},
             "compressor": {"threshold_db": -24.0, "ratio": 3.0}, "reverb": {"wet": 0.2},
             "ducks": [{"under": "voice"}], "fades": {"fade_in": "0.5s", "fade_out": "1s"}},
            {"name": "voice", "media": media(voice), "start": "1s", "pan": -0.3},
        ],
        "master": {"loudness": -14.0, "true_peak": -1.0},
    }


def test_rendering_twice_gives_identical_bytes(tmp_path: Path) -> None:
    mix = full_mix(tmp_path)
    _first, one = render(tmp_path / "one", mix)
    _second, two = render(tmp_path / "two", mix)
    assert hashlib.sha256(one.read_bytes()).digest() == hashlib.sha256(two.read_bytes()).digest()
    measured, peak = reference(one)
    assert abs(measured - -14.0) < 0.5
    assert peak <= -0.9


def test_stems_and_denoise_need_their_extras(tmp_path: Path) -> None:
    if installed("demucs"):
        pytest.skip("demucs is installed")
    tone = sine(tmp_path, "tone", 440.0, 1.0)
    mix: dict[str, JsonValue] = {"tracks": [{"name": "tone", "media": media(tone), "stems": ["vocals"]}]}
    call = mix_call(tmp_path, mix)
    executor = AudioMixExecutor()
    with pytest.raises(NodeFailure) as caught:
        executor.run(call)
    assert caught.value.code == "resolution_unavailable"
    assert "eks-harness[stems]" in caught.value.message
    context = ResolveContext(run=call.run, plan=call.plan, registry=call.registry, node=call.node, spec=call.spec,
                             args=call.args, data=call.data, resources=call.resources)
    with pytest.raises(NodeFailure) as predicted:
        executor.resolve(context)
    assert predicted.value.code == "resolution_unavailable"


def test_unsupported_lra_and_empty_mix_are_refused(tmp_path: Path) -> None:
    tone = sine(tmp_path, "tone", 440.0, 1.0)
    with pytest.raises(NodeFailure) as caught:
        render(tmp_path, {"tracks": [{"name": "tone", "media": media(tone)}], "master": {"lra": 7.0}})
    assert caught.value.code == "args_invalid"
    with pytest.raises(NodeFailure) as empty:
        render(tmp_path, {"tracks": []})
    assert empty.value.code == "args_invalid"


def test_spec_reports_node_location_and_runtime_versions() -> None:
    spec = AudioMixExecutor().spec()
    assert spec.name == AUDIO_EXECUTOR
    assert spec.locations == frozenset({"node"})
    assert spec.ops == ("audio.mix",)
    runtime = spec.runtime_version() if spec.runtime_version is not None else ""
    assert "scipy" in runtime and "numpy" in runtime and "ffmpeg" in runtime
