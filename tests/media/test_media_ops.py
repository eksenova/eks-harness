from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import JsonValue

from eks_harness.engine.cache import (
    ContentResolver,
    required_resolution,
    resolution_problems,
)
from eks_harness.engine.clock import marker_streams
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    ExecContext,
    ExecutorRegistry,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.hashing import machine_dependent
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.time import TICKS_PER_SECOND, TempoMap, TimeContext
from eks_harness.ir.types import Artifact, MediaInfo
from eks_harness.media.audio.inference import installed, version
from eks_harness.ops.media import (
    MARKERS_EXECUTOR,
    SOURCE_EXECUTOR,
    STEMS_EXECUTOR,
    TTS_EXECUTOR,
    media_executor_specs,
    resolve_markers,
    resolve_stems,
    resolve_tts,
    run_markers,
    run_source,
    run_stems,
    run_tts,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not on PATH")

RATE = 48000
NODE_HASH = "sha256:" + "c" * 64
PLAN_HASH = "sha256:" + "d" * 64
CLICK_PERIOD = 0.5
CLICK_SECONDS = 12.0
CLICKS = [index * CLICK_PERIOD for index in range(int(CLICK_SECONDS / CLICK_PERIOD))]


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
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=base))


class Sink:
    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        return None

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        return None


def make_call(store: Store, op: str, data: dict[str, JsonValue]) -> ExecContext:
    registry = default_registry()
    spec = registry.op(op)
    resolved = validate_args(spec, data)
    ref: JsonValue = {"$ref": "n0000000000"}
    args = {key: ref if key == "media" else value for key, value in data.items()}
    node = Node(id="n0000000001", op=op, args=args, out=spec.out, hash=NODE_HASH)
    plan = Plan(kind="edit", name="media", nodes=[node], hash=PLAN_HASH)
    info = RunInfo(id="01JMEDIATESTRUN0000000000", project="acme/media", session="media", plan_hash=PLAN_HASH,
                   plan_kind="edit", plan_name="media", created_at=time.time())
    sink = Sink()
    clock = TimeContext.static(fps=Fraction(30), tempo=TempoMap(bpm=120.0))
    downloads = store.root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    content = ContentResolver(store, tree=store.root, base=".", plugin_roots={}, scratch=downloads)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=None, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=sink,
                       artifacts=ArtifactSink(store, run=info, node_id=node.id, instance="", op=op, sink=sink),
                       seed=3, clock=plan.clock, deadline=None, cancel=threading.Event(),
                       time_context=lambda: clock, content=content)


def resolve_context(call: ExecContext) -> ResolveContext:
    return ResolveContext(run=call.run, plan=call.plan, registry=call.registry, node=call.node, spec=call.spec,
                          args=call.args, data=call.data, resources=call.resources)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def clicks(tmp_path: Path) -> Path:
    path = tmp_path / "clicks.wav"
    ffmpeg("-f", "lavfi", "-i",
           f"aevalsrc='if(lt(mod(t,{CLICK_PERIOD}),0.02),0.8*sin(2*PI*1000*t)*exp(-200*mod(t,{CLICK_PERIOD})),0)'"
           f":s={RATE}:d={CLICK_SECONDS}", "-c:a", "pcm_s16le", str(path))
    return path


def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2", "-f", "lavfi", "-i",
           f"sine=frequency=440:sample_rate={RATE}:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path))
    return path


def image(tmp_path: Path) -> Path:
    path = tmp_path / "still.png"
    ffmpeg("-f", "lavfi", "-i", "color=red:size=64x48:duration=1", "-frames:v", "1", str(path))
    return path


def source(store: Store, uri: str, kind: str | None = None) -> MediaInfo:
    data: dict[str, JsonValue] = {"uri": uri}
    if kind is not None:
        data["kind"] = kind
    result = run_source(make_call(store, "media.source", data))
    return MediaInfo.model_validate(result.value)


def test_source_probes_video_audio_and_image(tmp_path: Path) -> None:
    store = Store(tmp_path)
    clip = source(store, f"file:{video(tmp_path)}")
    assert (clip.kind, clip.width, clip.height, clip.fps) == ("video", 320, 240, Fraction(30))
    assert clip.duration is not None
    assert abs(clip.duration.ticks / TICKS_PER_SECOND - 2.0) < 0.1
    sound = source(store, f"file:{clicks(tmp_path)}")
    assert (sound.kind, sound.width, sound.fps) == ("audio", None, None)
    assert sound.duration is not None and sound.duration.ticks == round(CLICK_SECONDS * TICKS_PER_SECOND)
    still = source(store, f"file:{image(tmp_path)}")
    assert (still.kind, still.width, still.height, still.duration) == ("image", 64, 48, None)


def test_source_of_an_artifact_names_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    stored = store.ingest(image(tmp_path), project="acme/media", session="media", kind="image", name="still.png",
                          caption="", tags=(), meta={}, actor=None, move=False)
    found = source(store, f"artifact:{stored.id}")
    assert (found.artifact, found.kind, found.width) == (stored.id, "image", 64)
    pinned = source(store, f"artifact:{stored.id}@{stored.sha256}")
    assert pinned.artifact == stored.id


def test_source_kind_file_skips_probing_and_unknown_files_are_files(tmp_path: Path) -> None:
    store = Store(tmp_path)
    notes = tmp_path / "scene.blend"
    notes.write_bytes(b"BLENDER-v300 not really a blend file")
    assert source(store, f"file:{notes}", "file").model_dump(exclude_none=True) == {"uri": f"file:{notes}",
                                                                                    "kind": "file"}
    assert source(store, f"file:{notes}").kind == "file"
    with pytest.raises(NodeFailure) as caught:
        source(store, f"file:{tmp_path / 'missing.wav'}")
    assert caught.value.code == "content_unavailable"


def markers_call(tmp_path: Path, sources: list[JsonValue]) -> ExecContext:
    store = Store(tmp_path)
    found = source(store, f"file:{clicks(tmp_path)}")
    media: JsonValue = found.model_dump(mode="json", exclude_none=True)
    return make_call(store, "media.markers", {"media": media, "sources": sources})


def test_beat_tracker_finds_the_clicks_with_librosa(tmp_path: Path) -> None:
    call = markers_call(tmp_path, [{"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"],
                                    "backend": "librosa"}])
    result = run_markers(call)
    assert isinstance(result.value, dict)
    assert set(result.value) == {"song"}
    assert set(cast_dict(result.value["song"])) == {"beat", "downbeat"}
    streams, words = marker_streams(result.value, "test")
    assert words == {}
    beats = [ticks / TICKS_PER_SECOND for ticks in streams["song.beat"]]
    assert len(beats) >= len(CLICKS) // 2
    near = [beat for beat in beats if min(abs(beat - click) for click in CLICKS) < 0.06]
    assert len(near) >= 0.8 * len(beats)
    assert streams["song.downbeat"]
    record = result.resolution
    assert record is not None
    assert record["markers"] == {"song": {"backend": "librosa", "version": version("librosa")}}


def cast_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_auto_backend_resolves_and_matches_the_prediction(tmp_path: Path) -> None:
    call = markers_call(tmp_path, [{"kind": "beat_tracker", "name": "song"}])
    assert machine_dependent(call.node, call.registry)
    predicted = dict(resolve_markers(resolve_context(call)))
    assert required_resolution(call.node, call.registry) == ("markers.song.backend", "markers.song.version")
    assert resolution_problems(call.node, call.registry, predicted) == []
    expected = next(name for name, module in (("beatnet", "BeatNet"), ("madmom", "madmom"), ("librosa", "librosa"))
                    if installed(module))
    assert cast_dict(cast_dict(predicted["markers"])["song"])["backend"] == expected
    if expected == "librosa":
        result = run_markers(call)
        assert result.resolution == predicted


def test_audio_marker_sources_produce_their_streams(tmp_path: Path) -> None:
    call = markers_call(tmp_path, [
        {"kind": "onset_markers", "name": "hits", "sensitivity": 0.5},
        {"kind": "silence_markers", "name": "gaps", "threshold_db": -40.0, "min_duration": "0.2s"},
        {"kind": "energy_markers", "name": "loud", "band": "mid", "percentile": 0.9},
        {"kind": "momentum_markers", "name": "flow"},
    ])
    result = run_markers(call)
    value = cast_dict(result.value)
    assert set(value) == {"hits", "gaps", "loud", "flow"}
    assert set(cast_dict(value["flow"])) == {"drop", "build", "break", "section", "peak"}
    streams, _words = marker_streams(value, "test")
    onsets = [ticks / TICKS_PER_SECOND for ticks in streams["hits"]]
    assert len(onsets) >= len(CLICKS) - 2
    assert all(min(abs(onset - click) for click in CLICKS) < 0.06 for onset in onsets)
    gaps = [ticks / TICKS_PER_SECOND for ticks in streams["gaps"]]
    assert len(gaps) >= len(CLICKS) - 2
    assert streams["loud"]
    assert set(cast_dict(cast_dict(result.resolution)["markers"])) == {"hits", "gaps", "loud", "flow"}


def test_pinned_backends_that_are_missing_fail_without_fallback(tmp_path: Path) -> None:
    if installed("BeatNet"):
        pytest.skip("BeatNet is installed")
    call = markers_call(tmp_path, [{"kind": "beat_tracker", "name": "song", "backend": "beatnet"}])
    with pytest.raises(NodeFailure) as caught:
        run_markers(call)
    assert caught.value.code == "resolution_unavailable"
    assert "eks-harness[ml]" in caught.value.message


def test_speech_to_text_without_a_backend_is_unavailable(tmp_path: Path) -> None:
    if installed("faster_whisper"):
        pytest.skip("faster-whisper is installed")
    call = markers_call(tmp_path, [{"kind": "stt_markers", "name": "speech"}])
    with pytest.raises(NodeFailure) as caught:
        resolve_markers(resolve_context(call))
    assert caught.value.code == "resolution_unavailable"
    assert "faster-whisper" in caught.value.message


def test_scene_markers_need_scenedetect(tmp_path: Path) -> None:
    if installed("scenedetect"):
        pytest.skip("scenedetect is installed")
    store = Store(tmp_path)
    clip = source(store, f"file:{video(tmp_path)}").model_dump(mode="json", exclude_none=True)
    call = make_call(store, "media.markers", {"media": clip, "sources": [{"kind": "scene_markers", "name": "cuts"}]})
    with pytest.raises(NodeFailure) as caught:
        run_markers(call)
    assert caught.value.code == "resolution_unavailable"


def test_tts_is_unavailable_and_stems_name_the_extra_to_install(tmp_path: Path) -> None:
    store = Store(tmp_path)
    speech = make_call(store, "media.tts", {"text": "merhaba", "voice": "tr_female_1"})
    for action in (lambda: run_tts(speech), lambda: resolve_tts(resolve_context(speech))):
        with pytest.raises(NodeFailure) as caught:
            action()
        assert caught.value.code == "resolution_unavailable"
        assert "no speech synthesis integration" in caught.value.message
    if installed("demucs"):
        return
    song = source(store, f"file:{clicks(tmp_path)}").model_dump(mode="json", exclude_none=True)
    stems = make_call(store, "media.stems", {"media": song, "stems": ["drums"]})
    for action in (lambda: run_stems(stems), lambda: resolve_stems(resolve_context(stems))):
        with pytest.raises(NodeFailure) as caught:
            action()
        assert caught.value.code == "resolution_unavailable"
        assert "eks-harness[stems]" in caught.value.message


def test_specs_register_for_every_media_op() -> None:
    registry = ExecutorRegistry()
    for spec in media_executor_specs():
        registry.register(spec)
    ops = default_registry()
    chosen = {name: registry.real(ops.op(name)) for name in ("media.source", "media.markers", "media.tts",
                                                             "media.stems")}
    assert {name: spec.name if spec is not None else None for name, spec in chosen.items()} == {
        "media.source": SOURCE_EXECUTOR, "media.markers": MARKERS_EXECUTOR, "media.tts": TTS_EXECUTOR,
        "media.stems": STEMS_EXECUTOR}
    markers = chosen["media.markers"]
    assert markers is not None and markers.resolver is not None
    assert markers.runtime_version is not None and "librosa" in markers.runtime_version()
