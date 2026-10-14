from __future__ import annotations

import json
import struct
import threading
import time
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import JsonValue

import eks
from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import projects, sessions
from eks_harness.engine.drivers import CallSite, DriverReply
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    BodyOutcome,
    DaemonArtifacts,
    ExecContext,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.model import (
    BackendHandle,
    BackendIdentity,
    CheckpointOutcome,
    FakeAnswer,
    FakeProfile,
    LeaseHandle,
    RunInfo,
    RunResources,
)
from eks_harness.engine.resources import (
    CassetteRequest,
    SeedRestoreRequest,
    cassette_key,
)
from eks_harness.ir.hashing import derive_id, machine_dependent
from eks_harness.ir.plan import CheckUse, Node, Plan, Resource, SessionSpec
from eks_harness.ir.registry import Registry, default_registry, using_registry
from eks_harness.ir.types import Artifact, MediaInfo
from eks_harness.ops.fakes import (
    FAKE_DRIVER,
    FAKE_EXECUTOR,
    FAKE_PLATFORMS,
    FAKE_URL,
    FAKE_VERSION,
    SHOT_SIZE,
    FakeCassetteHook,
    FakeDriverPort,
    FakeDriverProvider,
    FakeExecutors,
    FakeSeedHook,
    answer_for,
    covers,
    default_resolution,
    default_value,
    png_bytes,
)

PROJECT = "acme/fakes"
SESSION = "fake-tests"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
LEASE_ID = "rlease01"
BACKEND_ID = "rback001"
SOURCE_REF: dict[str, JsonValue] = {"$ref": "n0000000000", "path": []}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []
        self.artifacts: list[Artifact] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        self.events.append(("progress", {"fraction": fraction}))

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)


def session_spec() -> SessionSpec:
    return SessionSpec(app="web-app", platform="web", viewport="desktop",
                       defaults=(CheckUse(kind="console_errors"), CheckUse(kind="layout_spill")),
                       checks=(CheckUse(kind="perf", params={"lcp": 2500}),))


def make_node(op: str, args: dict[str, JsonValue] | None = None, *, out: str | None = None, node_id: str = "n0000000001",
              hash_: str = HASH_A, uses: list[str] | None = None,
              branches: dict[str, list[Node]] | None = None) -> Node:
    spec = default_registry().op(op)
    return Node(id=node_id, op=op, args=args or {}, out=out or spec.out, hash=hash_, uses=uses or [],
                branches=branches)


def make_plan(*nodes: Node) -> Plan:
    resources = [
        Resource(id=LEASE_ID, kind="lease.web", app="web-app", session="main"),
        Resource(id=BACKEND_ID, kind="backend", app="web-app", session="main", definition="testapi"),
    ]
    return Plan(kind="flow", name="fakes", session=session_spec(), resources=resources, nodes=list(nodes),
                hash=HASH_B)


def run_info(plan: Plan) -> RunInfo:
    return RunInfo(id="01JFAKETESTRUN00000000000", project=PROJECT, session=SESSION, plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, actor="tester", created_at=time.time())


def make_call(plan: Plan, node: Node, store: DaemonArtifacts, *, attempt: int = 1, sink: RecordingSink | None = None,
              resources: RunResources | None = None, cancel: threading.Event | None = None,
              deadline: float | None = None) -> ExecContext:
    registry = default_registry()
    spec = registry.op(node.op)
    model = spec.args.model_validate(node.args)
    data = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    info = run_info(plan)
    recording = sink or RecordingSink()
    artifacts = ArtifactSink(store, run=info, node_id=node.id, instance="", op=node.op, sink=recording)
    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=attempt,
                       args=model, data=data, session=plan.session, state=SessionState("main"),
                       resources=resources or RunResources(), driver=None, sink=recording, artifacts=artifacts,
                       seed=0, clock=plan.clock, deadline=deadline, cancel=cancel or threading.Event(),
                       simulated=True)


@pytest.fixture
def store(db: Database, config: Config) -> DaemonArtifacts:
    with db.transaction() as conn:
        projects.ensure(conn, PROJECT)
        sessions.ensure_in_project(conn, PROJECT, SESSION)
    return DaemonArtifacts(db, config)


def png_header(data: bytes) -> tuple[int, int, int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    crc = struct.unpack(">I", data[29:33])[0]
    assert crc == zlib.crc32(data[12:29])
    assert data.endswith(b"IEND\xaeB`\x82")
    return width, height, depth, colour


def has_path(record: Mapping[str, JsonValue], dotted: str) -> bool:
    value: JsonValue = dict(record)
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return isinstance(value, str) and value.startswith("fake")


ALL_FIELDS = (
    "engine", "version", "encoder", "encoder_version", "inference.device", "inference.model", "inference.weights",
    "inference.backend_version", "gpu.adapter", "gpu.driver", "platform", "browser", "device", "model", "weights",
    "backend_version",
)
MARKER_FIELDS = ("backend", "version", "model", "device", "compute_type")


def test_covers_and_covered() -> None:
    registry = default_registry()
    tap = registry.op("ui.tap")
    markers = registry.op("media.markers")
    assert covers(["ui"], tap) and covers(["ui.*"], tap) and covers(["*"], tap) and covers(["ui.tap"], tap)
    assert not covers(["media", "ui.fill"], tap)
    assert not covers([], tap)
    fakes = FakeExecutors(FakeProfile(families=["media"], force=["ui.tap"]), registry)
    assert fakes.covered(tap, real=True)
    assert fakes.covered(tap, real=False)
    assert fakes.covered(markers, real=False)
    assert not fakes.covered(markers, real=True)
    assert not fakes.covered(registry.op("ui.fill"), real=False)


def test_executor_spec_fields() -> None:
    registry = default_registry()
    fakes = FakeExecutors(FakeProfile(), registry)
    tap = fakes.executor(registry.op("ui.tap"))
    assert tap is not None
    assert (tap.name, tap.version, tap.ops) == (FAKE_EXECUTOR, FAKE_VERSION, ("ui.tap",))
    assert tap.run == fakes.run and tap.container is None
    assert tap.resolver is None and tap.fake and tap.locations is None
    record = fakes.executor(registry.op("capture.record"))
    assert record is not None and record.container is fakes and record.run is None
    for name in ("media.markers", "check.media", "edit.render", "gfx.render", "blender.render", "scene.render",
                 "audio.mix", "media.tts", "media.stems"):
        spec = fakes.executor(registry.op(name))
        assert spec is not None and spec.resolver == fakes.resolve, name
    for name in ("media.source", "overlay.annotate", "data.create", "api.get"):
        spec = fakes.executor(registry.op(name))
        assert spec is not None and spec.resolver is None, name
    assert fakes.executor(registry.op("ctl.when")) is None
    assert fakes.cassette_hook is not None
    assert FakeExecutors(FakeProfile(cassettes=False), registry).cassette_hook is None
    own = FakeCassetteHook()
    assert FakeExecutors(FakeProfile(cassettes=False), registry, cassette_hook=own).cassette_hook is own


def test_answer_for_precedence_and_times() -> None:
    node = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    by_id = FakeAnswer(value=1, times=2)
    by_op = FakeAnswer(value=2)
    profile = FakeProfile(answers={node.id: by_id, "ui.tap": by_op})
    assert answer_for(profile, node, 1) is by_id
    assert answer_for(profile, node, 2) is by_id
    assert answer_for(profile, node, 3) is None
    assert answer_for(FakeProfile(answers={"ui.tap": by_op}), node, 9) is by_op
    assert answer_for(FakeProfile(), node, 1) is None


def test_scripted_failures_values_delays_and_resolutions(store: DaemonArtifacts) -> None:
    registry = default_registry()
    get = make_node("ui.text", {"target": {"by": "text", "args": {"text": "x"}}}, uses=[LEASE_ID])
    plan = make_plan(get)
    failing = FakeExecutors(FakeProfile(answers={get.id: FakeAnswer(fail="boom", code="assertion", retryable=True,
                                                                     soft=True, times=1)}), registry)
    with pytest.raises(NodeFailure, match="boom") as failure:
        failing.run(make_call(plan, get, store))
    assert (failure.value.code, failure.value.retryable, failure.value.soft) == ("assertion", True, True)
    assert failing.run(make_call(plan, get, store, attempt=2)).value == f"fake ui.text {HASH_A[7:15]}"

    valued = FakeExecutors(FakeProfile(answers={"ui.text": FakeAnswer(value=None)}), registry)
    assert valued.run(make_call(plan, get, store)).value is None
    wrong = FakeExecutors(FakeProfile(answers={"ui.text": FakeAnswer(value=42)}), registry)
    assert wrong.run(make_call(plan, get, store)).value == 42

    slow = FakeExecutors(FakeProfile(answers={"ui.text": FakeAnswer(delay_ms=5000)}), registry)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    started = time.monotonic()
    timer.start()
    with pytest.raises(NodeFailure) as cancelled:
        slow.run(make_call(plan, get, store, cancel=cancel))
    assert cancelled.value.code == "cancelled"
    assert time.monotonic() - started < 2.0
    started = time.monotonic()
    with pytest.raises(NodeFailure) as timed_out:
        slow.run(make_call(plan, get, store, deadline=time.monotonic() + 0.1))
    assert timed_out.value.code == "timeout"
    assert time.monotonic() - started < 2.0
    short = FakeExecutors(FakeProfile(answers={"ui.text": FakeAnswer(delay_ms=50, value="late")}), registry)
    assert short.run(make_call(plan, get, store)).value == "late"

    markers = make_node("media.markers", {"media": SOURCE_REF, "sources": [{"kind": "beat_tracker", "name": "song"}]})
    custom: dict[str, JsonValue] = {"markers": {"song": {"backend": "madmom"}}}
    resolving = FakeExecutors(FakeProfile(answers={markers.id: FakeAnswer(resolution=custom)}), registry)
    marker_plan = make_plan(markers)
    call = make_call(marker_plan, markers, store)
    assert resolving.run(call).resolution == custom
    context = ResolveContext(run=call.run, plan=marker_plan, registry=registry, node=markers, spec=call.spec,
                             args=call.args, data=call.data, resources=RunResources())
    assert resolving.resolve(context) == custom
    plain = FakeExecutors(FakeProfile(), registry)
    assert plain.resolve(context) == default_resolution(call.spec, markers, registry)
    assert plain.run(call).resolution == plain.resolve(context)
    assert plain.run(make_call(plan, get, store)).resolution is None


def test_default_values_by_type() -> None:
    registry = default_registry()

    def value(op: str, args: dict[str, JsonValue] | None = None, *, out: str | None = None,
              hash_: str = HASH_A) -> JsonValue:
        node = make_node(op, args, out=out, hash_=hash_)
        spec = registry.op(op)
        model = spec.args.model_validate(node.args)
        return default_value(spec, node, model, model.model_dump(mode="json", by_alias=True, exclude_none=True))

    target: dict[str, JsonValue] = {"target": {"by": "text", "args": {"text": "x"}}}
    assert value("ui.tap", target) is None
    assert value("ui.exists", target) is True
    assert value("ui.count", target) == 1
    assert value("ui.attr", {**target, "name": "href"}, out="float") == 1.0
    assert value("ui.url") == FAKE_URL
    assert value("ui.wait_url", {"pattern": "/x"}) == FAKE_URL
    assert value("ui.text", target) == f"fake ui.text {HASH_A[7:15]}"
    assert value("ui.text", target, hash_=HASH_B) == f"fake ui.text {HASH_B[7:15]}"
    assert value("ui.route") == {"name": "fake", "params": {}}
    assert value("web.logs") == []
    assert value("mobile.route_names") == []
    assert value("expect.target", {**target, "assertion": {"kind": "visible"}}) == {"ok": True, "assertion": "visible"}
    assert value("check.explore", {"budget": "10s"}) == {"ok": True, "screens": 0, "findings": []}
    assert value("check.media", {"media": SOURCE_REF, "kind": "cut"}) == {"ok": True, "kind": "cut"}
    assert value("ui.layout", target) == {}
    assert value("ui.state", {"path": "a.b"}, out="list[json]") == []
    created = value("data.create", {"kind": "tenant", "key": "harman", "path": "tenant:harman"})
    assert created == {"id": derive_id("f", "tenant:harman"), "path": "tenant:harman", "kind": "tenant",
                       "key": "harman"}
    based = value("data.base", {"base": "standard", "kind": "tenant", "path": "tenant:harman"})
    assert based == {"id": derive_id("f", "tenant:harman"), "path": "tenant:harman", "kind": "tenant", "key": None}
    resource = value("data.create", {"kind": "tenant", "path": "tenant#0"}, out="resource:tenant")
    assert resource == {"id": derive_id("f", "tenant#0"), "path": "tenant#0", "kind": "tenant", "key": None}
    assert value("media.source", {"uri": "file:assets/clip.mp4"}) == {"uri": "file:assets/clip.mp4", "kind": "video"}
    assert value("media.source", {"uri": "file:a.blend", "kind": "file"}) == {"uri": "file:a.blend", "kind": "file"}
    assert value("media.source", {"uri": "https://x.test/song.mp3"}) == {"uri": "https://x.test/song.mp3",
                                                                          "kind": "audio"}
    assert value("media.source", {"uri": "artifact:ABC"}) == {"uri": "artifact:ABC", "kind": "file"}

    checkpoint = value("check.checkpoint", {"label": "liste"})
    outcome = CheckpointOutcome.model_validate(checkpoint)
    assert outcome.label == "liste" and outcome.ok and outcome.shots == [] and outcome.checks == []
    own = value("check.checkpoint", {"label": "x", "checks": [{"kind": "perf", "params": {}}]})
    assert [check.kind for check in CheckpointOutcome.model_validate(own).checks] == ["perf"]

    markers = value("media.markers", {"media": SOURCE_REF, "sources": [
        {"kind": "beat_tracker", "name": "song", "streams": ["beat", "downbeat"]},
        {"kind": "scene_markers", "name": "cuts"},
        {"kind": "momentum_markers", "name": "energy"},
    ]})
    assert isinstance(markers, dict)
    grid = [f"{index * 0.5:g}s" if index else "0s" for index in range(16)]
    assert markers["song"] == {"beat": grid, "downbeat": grid}
    assert markers["cuts"] == {"cuts": grid}
    energy = markers["energy"]
    assert isinstance(energy, dict)
    assert set(energy) == {"drop", "build", "break", "section", "peak"}

    file_cases: list[tuple[str, dict[str, JsonValue] | None]] = [("capture.shot", {"name": "x"}), ("edit.render", None)]
    for op, args in file_cases:
        spec = registry.op(op)
        node = make_node(op, args)
        with pytest.raises(ValueError, match="comes from the file"):
            default_value(spec, node, spec.args.model_construct(), {})


def test_grid_times_are_valid() -> None:
    registry = default_registry()
    spec = registry.op("media.markers")
    node = make_node("media.markers", {"media": SOURCE_REF, "sources": [{"kind": "onset_markers", "name": "hits"}]})
    model = spec.args.model_validate(node.args)
    found = default_value(spec, node, model, {})
    assert isinstance(found, dict)
    hits = found["hits"]
    assert isinstance(hits, dict)
    points = hits["hits"]
    assert isinstance(points, list) and len(points) == 16
    assert points[0] == "0s" and points[1] == "0.5s" and points[15] == "7.5s"


def test_png_bytes_is_a_deterministic_png() -> None:
    first = png_bytes("capture.shot:" + HASH_A)
    assert first == png_bytes("capture.shot:" + HASH_A)
    assert first != png_bytes("capture.shot:" + HASH_B)
    assert png_header(first) == (SHOT_SIZE[0], SHOT_SIZE[1], 8, 2)
    assert png_header(png_bytes("x", (3, 2)))[:2] == (3, 2)
    raw = zlib.decompress(first[first.index(b"IDAT") + 4:first.index(b"IEND") - 8])
    assert len(raw) == SHOT_SIZE[1] * (1 + 3 * SHOT_SIZE[0])
    with pytest.raises(ValueError, match="positive size"):
        png_bytes("x", (0, 4))


def test_default_resolution_covers_every_requirement() -> None:
    registry = default_registry()
    cases = [
        make_node("media.markers", {"media": SOURCE_REF, "sources": [
            {"kind": "beat_tracker", "name": "song"},
            {"kind": "stt_markers", "name": "speech"},
        ]}),
        make_node("check.media", {"media": SOURCE_REF, "kind": "not-registered-check"}),
        make_node("edit.render", {"timeline": {"fps": 30, "size": [1080, 1920], "render": {"encoder": "nvenc"}}}),
        make_node("gfx.render", {"scene": {"size": [100, 100]}}),
        make_node("blender.render", {"scene": {"render": {"engine": "BLENDER_EEVEE", "size": [10, 10]}}}),
        make_node("scene.render", {"scene": {"size": [100, 100]}}),
        make_node("media.tts", {"text": "merhaba"}),
        make_node("audio.mix", {"mix": {"tracks": [{"media": SOURCE_REF, "stems": ["vocals"]}]}}),
    ]
    records = []
    for node in cases:
        assert machine_dependent(node, registry), node.op
        record = default_resolution(registry.op(node.op), node, registry)
        assert record is not None, node.op
        missing = [field for field in ALL_FIELDS if not has_path(record, field)]
        assert missing == [], (node.op, missing)
        records.append(record)
    marker_record = records[0]
    for source in ("song", "speech"):
        assert all(has_path(marker_record, f"markers.{source}.{field}") for field in MARKER_FIELDS)
    assert {key for record in records for key in record} == set(records[1])
    for node in (
        make_node("media.markers", {"media": SOURCE_REF, "sources": [
            {"kind": "beat_tracker", "name": "song", "backend": "madmom"}]}),
        make_node("edit.render", {"timeline": {"fps": 30, "size": [10, 10], "render": {"encoder": "libx264"}}}),
        make_node("gfx.render", {"scene": {"size": [10, 10]}, "cpu": True}),
        make_node("media.source", {"uri": "file:a.mp4"}),
        make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}}),
    ):
        assert default_resolution(registry.op(node.op), node, registry) is None, node.op


def test_fake_run_ingests_artifacts_and_media(store: DaemonArtifacts, db: Database) -> None:
    registry = default_registry()
    fakes = FakeExecutors(FakeProfile(families=["*"], force=["capture.*", "check.*"]), registry)
    shot = make_node("capture.shot", {"name": "liste"}, uses=[LEASE_ID], node_id="n0000000001")
    render = make_node("edit.render", {"timeline": {"fps": 30, "size": [1080, 1920], "duration": "2s",
                                                    "render": {"encoder": "libx264"}}}, node_id="n0000000002")
    speech = make_node("media.tts", {"text": "merhaba"}, node_id="n0000000003")
    source = make_node("media.source", {"uri": "file:does/not/exist.mp4"}, node_id="n0000000004")
    checkpoint = make_node("check.checkpoint", {"label": "liste"}, uses=[LEASE_ID], node_id="n0000000005")
    dump = make_node("capture.dump", {"kind": "dom"}, uses=[LEASE_ID], node_id="n0000000006")
    har = make_node("capture.har", {}, uses=[LEASE_ID], node_id="n0000000007")
    plan = make_plan(shot, render, speech, source, checkpoint, dump, har)

    sink = RecordingSink()
    result = fakes.run(make_call(plan, shot, store, sink=sink))
    artifact = Artifact.model_validate(result.value)
    assert artifact.name == "liste.png" and artifact.kind == "screenshot"
    assert sink.artifacts == [artifact]
    stored = store.file(artifact.id)
    assert stored is not None and stored.read_bytes() == png_bytes(f"capture.shot:{HASH_A}")
    assert result.detail == {"fake": True}

    media = MediaInfo.model_validate(fakes.run(make_call(plan, render, store)).value)
    assert media.kind == "video" and media.artifact is not None and media.uri == f"artifact:{media.artifact}"
    assert (media.width, media.height) == (1080, 1920)
    assert media.fps == 30 and media.duration is not None
    rendered = store.file(media.artifact)
    assert rendered is not None
    assert json.loads(rendered.read_bytes())["op"] == "edit.render"
    again = MediaInfo.model_validate(fakes.run(make_call(plan, render, store)).value)
    second = store.file(again.artifact or "")
    assert second is not None and second.read_bytes() == rendered.read_bytes()
    assert again.artifact != media.artifact

    audio = MediaInfo.model_validate(fakes.run(make_call(plan, speech, store)).value)
    assert audio.kind == "audio" and audio.width is None

    assert fakes.run(make_call(plan, source, store)).value == {"uri": "file:does/not/exist.mp4", "kind": "video"}

    outcome = CheckpointOutcome.model_validate(fakes.run(make_call(plan, checkpoint, store)).value)
    assert outcome.ok and outcome.label == "liste" and outcome.viewport == "desktop"
    assert [check.kind for check in outcome.checks] == ["console_errors", "layout_spill", "perf"]
    assert all(check.ok for check in outcome.checks)
    assert len(outcome.shots) == 1
    shot_file = store.file(outcome.shots[0])
    assert shot_file is not None and png_header(shot_file.read_bytes())[:2] == SHOT_SIZE

    dumped = Artifact.model_validate(fakes.run(make_call(plan, dump, store)).value)
    assert dumped.kind == "dom"
    har_artifact = Artifact.model_validate(fakes.run(make_call(plan, har, store)).value)
    har_file = store.file(har_artifact.id)
    assert har_file is not None and "log" in json.loads(har_file.read_bytes())


def test_fake_containers_and_cassette_hook(store: DaemonArtifacts) -> None:
    registry = default_registry()
    hook = FakeCassetteHook()
    fakes = FakeExecutors(FakeProfile(force=["capture.record", "net.cassette", "score.take"]), registry,
                          cassette_hook=hook)
    record = make_node("capture.record", {"name": "anlatim"}, uses=[LEASE_ID], node_id="n0000000001",
                       branches={"body": []})
    cassette = make_node("net.cassette", {"name": "banka", "mode": "record"}, uses=[LEASE_ID],
                         node_id="n0000000002", branches={"body": []})
    take = make_node("score.take", {"session": {"key": "take0", "app": "mobile-app", "platform": "ios"},
                                    "size": [720, 1280]}, uses=[LEASE_ID, BACKEND_ID], node_id="n0000000003",
                     branches={"body": []})
    plan = make_plan(record, cassette, take)
    identity = BackendIdentity(definition="testapi", backend_id="testapi@x", lease_id="bl_1", source="request",
                               fingerprint="fp1")
    held = RunResources(backends={BACKEND_ID: BackendHandle(resource_id=BACKEND_ID, url="http://127.0.0.1:9",
                                                            identity=identity)})

    call = make_call(plan, record, store)
    entry = fakes.enter(call)
    assert entry.branches == ("body",)
    recorded = Artifact.model_validate(fakes.exit(call, BodyOutcome("passed", ("body",))).value)
    assert recorded.kind == "video"

    cassette_call = make_call(plan, cassette, store, resources=held)
    opened = fakes.enter(cassette_call)
    key = cassette_key(plan, cassette, identity)
    assert opened.detail["cassette"] == {"mode": "record", "key": key}
    closed = fakes.exit(cassette_call, BodyOutcome("passed", ("body",)))
    assert closed.value is None and closed.artifacts == []
    assert hook.recorded[key]["status"] == "passed"
    replay = CassetteRequest(run=cassette_call.run, node_id=cassette.id, name="banka", mode="replay", key=key,
                             backend=identity)
    assert hook.available(replay)

    take_media = MediaInfo.model_validate(fakes.exit(make_call(plan, take, store), BodyOutcome("passed", ())).value)
    assert (take_media.kind, take_media.width, take_media.height) == ("video", 720, 1280)

    failing = FakeExecutors(FakeProfile(force=["capture.record"],
                                        answers={"capture.record": FakeAnswer(fail="no video")}), registry)
    assert failing.enter(call).branches == ("body",)
    with pytest.raises(NodeFailure, match="no video"):
        failing.exit(call, BodyOutcome("passed", ("body",)))


def lease() -> LeaseHandle:
    return LeaseHandle(resource_id=LEASE_ID, kind="lease.web", platform="web", session_key="main", sid="sid001",
                       project=PROJECT, session=SESSION)


def port_for(tmp_path: Path, profile: FakeProfile | None = None) -> tuple[FakeDriverPort, Plan]:
    plan = make_plan()
    provider = FakeDriverProvider(profile or FakeProfile(), default_registry(), tmp_path / "scratch")
    port = provider.open(lease(), plan.session, run_info(plan))
    assert isinstance(port, FakeDriverPort)
    return port, plan


def data_of(node: Node) -> dict[str, Any]:
    spec = default_registry().op(node.op)
    return spec.args.model_validate(node.args).model_dump(mode="json", by_alias=True, exclude_none=True)


def test_fake_driver_port_answers_worker_ops(tmp_path: Path) -> None:
    port, _plan = port_for(tmp_path)
    assert port.simulated and port.platform == "web" and port.version() == "fake-1"
    tap = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    reply = port.call("ui.tap", data_of(tap), site=CallSite(tap))
    assert reply.value is None and reply.files == ()

    shot = make_node("capture.shot", {"name": "liste"}, node_id="n0000000002")
    shot_reply = port.call("capture.shot", data_of(shot), site=CallSite(shot))
    assert shot_reply.value is None
    (file,) = shot_reply.files
    assert file.role == "value" and file.kind == "screenshot" and file.name == "liste.png"
    assert file.path.read_bytes() == png_bytes(f"capture.shot:{HASH_A}")
    assert file.path.parent == tmp_path / "scratch" / f"01JFAKETESTRUN00000000000-{LEASE_ID}"
    assert HASH_A[7:23] in file.path.name
    other_shot = make_node("capture.shot", {"name": "liste"}, node_id="n0000000002", hash_=HASH_B)
    (other_file,) = port.call("capture.shot", data_of(other_shot), site=CallSite(other_shot)).files
    assert other_file.path.read_bytes() != file.path.read_bytes()
    (same_file,) = port.call("capture.shot", data_of(shot), site=CallSite(shot, attempt=2)).files
    assert same_file.path != file.path and same_file.path.read_bytes() == file.path.read_bytes()

    checkpoint = make_node("check.checkpoint", {"label": "liste"}, node_id="n0000000003", hash_=HASH_B)
    check_reply = port.call("check.checkpoint", data_of(checkpoint), site=CallSite(checkpoint))
    outcome = CheckpointOutcome.model_validate(check_reply.value)
    assert outcome.ok and outcome.shots == [] and len(outcome.checks) == 3
    (shot_file,) = check_reply.files
    assert shot_file.role == "shot" and shot_file.path.read_bytes() == png_bytes(f"check.checkpoint:{HASH_B}")

    persona = make_node("session.as", {"persona": "admin"}, node_id="n0000000004")
    assert port.call("session.as", {"persona": "admin", "headers": {}}, site=CallSite(persona)).value is None

    record = make_node("capture.record", {"name": "video"}, node_id="n0000000005", branches={"body": []})
    entered = port.call("capture.record", data_of(record), site=CallSite(record), phase="enter")
    assert entered.files == () and entered.value is None
    exited = port.call("capture.record", data_of(record), site=CallSite(record), phase="exit")
    assert exited.files[0].role == "value" and exited.files[0].kind == "video"
    with pytest.raises(DriverError, match="takes the phase run"):
        port.call("ui.tap", data_of(tap), site=CallSite(tap), phase="enter")
    with pytest.raises(DriverError, match="takes the phase enter or exit"):
        port.call("capture.record", data_of(record), site=CallSite(record))
    with pytest.raises(DriverError, match="knows no op"):
        port.call("ui.nope", {}, site=CallSite(tap))
    assert port.calls[:8] == [("ui.tap", "run"), ("capture.shot", "run"), ("capture.shot", "run"),
                              ("capture.shot", "run"), ("check.checkpoint", "run"), ("session.as", "run"),
                              ("capture.record", "enter"), ("capture.record", "exit")]


def test_fake_driver_port_answers_by_site(tmp_path: Path) -> None:
    text = make_node("ui.text", {"target": {"by": "text", "args": {"text": "x"}}}, node_id="n0000000009")
    other = make_node("ui.text", {"target": {"by": "text", "args": {"text": "y"}}}, node_id="n0000000008",
                      hash_=HASH_B)
    profile = FakeProfile(answers={
        text.id: FakeAnswer(value="by id", times=1),
        "ui.text": FakeAnswer(value="by op"),
        "ui.fill": FakeAnswer(fail="cannot type", code="driver_error", retryable=True, soft=True),
    })
    port, _plan = port_for(tmp_path, profile)
    assert port.call("ui.text", data_of(text), site=CallSite(text, attempt=1)).value == "by id"
    assert port.call("ui.text", data_of(text), site=CallSite(text, attempt=2)).value == f"fake ui.text {HASH_A[7:15]}"
    assert port.call("ui.text", data_of(other), site=CallSite(other)).value == "by op"
    fill = make_node("ui.fill", {"target": {"by": "text", "args": {"text": "x"}}, "value": "abc"})
    with pytest.raises(DriverError, match="cannot type") as failure:
        port.call("ui.fill", data_of(fill), site=CallSite(fill))
    assert (failure.value.code, failure.value.retryable, failure.value.soft) == ("driver_error", True, True)


def test_fake_driver_port_delay_is_bounded_and_cancellable(tmp_path: Path) -> None:
    slow = make_node("ui.text", {"target": {"by": "text", "args": {"text": "x"}}})
    port, _plan = port_for(tmp_path, FakeProfile(answers={"ui.text": FakeAnswer(delay_ms=5000, value="v")}))
    started = time.monotonic()
    with pytest.raises(DriverError) as timed_out:
        port.call("ui.text", data_of(slow), site=CallSite(slow), timeout=0.1)
    assert timed_out.value.code == "timeout"
    assert time.monotonic() - started < 2.0
    timer = threading.Timer(0.1, port.close)
    timer.start()
    started = time.monotonic()
    with pytest.raises(DriverError, match="port closed"):
        port.call("ui.text", data_of(slow), site=CallSite(slow))
    assert time.monotonic() - started < 2.0
    quick, _plan = port_for(tmp_path, FakeProfile(answers={"ui.text": FakeAnswer(delay_ms=30, value="v")}))
    assert quick.call("ui.text", data_of(slow), site=CallSite(slow), timeout=5).value == "v"


def test_fake_driver_port_snapshots_and_close(tmp_path: Path) -> None:
    port, _plan = port_for(tmp_path)
    files = port.snapshot(["screen", "dom", "a11y"], tmp_path / "snaps")
    assert [file.kind for file in files] == ["screenshot", "dom", "a11y"]
    assert all(file.role == "evidence" and file.path.is_file() for file in files)
    assert png_header(files[0].path.read_bytes())[:2] == SHOT_SIZE
    assert files[1].path.suffix == ".html" and "<html>" in files[1].path.read_text(encoding="utf-8")
    port.close()
    tap = make_node("ui.tap", {"target": {"by": "text", "args": {"text": "x"}}})
    with pytest.raises(DriverError, match="port closed") as closed:
        port.call("ui.tap", data_of(tap), site=CallSite(tap))
    assert closed.value.code == "driver_error"
    with pytest.raises(DriverError, match="port closed"):
        port.snapshot(["screen"], tmp_path / "later")


def test_fake_driver_provider() -> None:
    provider = FakeDriverProvider(FakeProfile(), default_registry(), Path("/tmp/unused"))
    assert provider.name == FAKE_DRIVER
    assert provider.platforms == FAKE_PLATFORMS == frozenset({"web", "ios", "android"})


def test_fake_driver_port_validates_args_against_its_own_registry(tmp_path: Path) -> None:
    from eks_harness.ir.ops.overlay import OverlayBase

    isolated = Registry()
    isolated.restore(default_registry().snapshot())
    with using_registry(isolated):
        @eks.overlay_kind(live="acme/badge.js", description="Acme badge")
        class AcmeBadge(OverlayBase, frozen=True):
            kind: Literal["acme_badge"] = "acme_badge"

    assert AcmeBadge.model_fields["kind"].default == "acme_badge"
    assert not any(spec.kind == "acme_badge" for spec in default_registry().catalogs("overlay"))
    plan = make_plan()
    port = FakeDriverProvider(FakeProfile(), isolated, tmp_path / "scratch").open(lease(), plan.session,
                                                                                 run_info(plan))
    show = Node(id="n0000000001", op="overlay.show", args={"overlay": {"kind": "acme_badge"}}, out="none",
                hash=HASH_A)
    outcome: list[object] = []

    def call_from_worker() -> None:
        try:
            outcome.append(port.call("overlay.show", {"overlay": {"kind": "acme_badge"}}, site=CallSite(show)))
        except DriverError as problem:
            outcome.append(problem)

    worker = threading.Thread(target=call_from_worker)
    worker.start()
    worker.join(5)
    (reply,) = outcome
    assert isinstance(reply, DriverReply) and reply.value is None
    with pytest.raises(DriverError, match="do not validate") as invalid:
        port.call("overlay.show", {"overlay": {"kind": "acme_missing"}}, site=CallSite(show))
    assert invalid.value.code == "args_invalid"


def test_fake_driver_port_calls_the_cassette_hook(tmp_path: Path) -> None:
    hook = FakeCassetteHook()
    cassette = make_node("net.cassette", {"name": "banka", "mode": "record"}, uses=[LEASE_ID],
                         branches={"body": []})
    plan = make_plan(cassette)
    identity = BackendIdentity(definition="testapi", backend_id="testapi@x", lease_id="bl_1", source="request",
                               fingerprint="fp1")
    with pytest.raises(ValueError, match="needs the run's plan"):
        FakeDriverProvider(FakeProfile(), default_registry(), tmp_path / "scratch", cassette_hook=hook)
    provider = FakeDriverProvider(FakeProfile(), default_registry(), tmp_path / "scratch", cassette_hook=hook,
                                  plan=plan, backends={BACKEND_ID: identity})
    port = provider.open(lease(), plan.session, run_info(plan))
    assert isinstance(port, FakeDriverPort) and port.cassette_hook is hook and port.plan is plan
    key = cassette_key(plan, cassette, identity)
    entered = port.call("net.cassette", data_of(cassette), site=CallSite(cassette), phase="enter")
    assert entered.detail["cassette"] == {"mode": "record", "key": key}
    assert entered.detail["branches"] == ["body"]
    assert hook.recorded == {}
    exited = port.call("net.cassette", data_of(cassette), site=CallSite(cassette, instance="i1", attempt=2),
                       phase="exit")
    assert exited.value is None and exited.files == () and exited.detail["cassette"] is None
    assert hook.recorded[key] == {"node": cassette.id, "instance": "i1", "attempt": 2}
    replay = CassetteRequest(run=run_info(plan), node_id=cassette.id, name="banka", mode="replay", key=key,
                             backend=identity)
    assert hook.available(replay)

    unbacked = FakeDriverProvider(FakeProfile(), default_registry(), tmp_path / "other", cassette_hook=hook,
                                  plan=plan).open(lease(), plan.session, run_info(plan))
    opened = unbacked.call("net.cassette", data_of(cassette), site=CallSite(cassette), phase="enter")
    assert opened.detail["cassette"] == {"mode": "record", "key": cassette_key(plan, cassette, None)}

    plain, _plan = port_for(tmp_path)
    assert "cassette" not in plain.call("net.cassette", data_of(cassette), site=CallSite(cassette),
                                        phase="enter").detail
    failing = FakeDriverProvider(FakeProfile(answers={"net.cassette": FakeAnswer(fail="proxy down")}),
                                 default_registry(), tmp_path / "failing", cassette_hook=FakeCassetteHook(),
                                 plan=plan, backends={BACKEND_ID: identity}).open(lease(), plan.session,
                                                                                  run_info(plan))
    with pytest.raises(DriverError, match="proxy down"):
        failing.call("net.cassette", data_of(cassette), site=CallSite(cassette), phase="exit")


def test_fake_seed_hook() -> None:
    plan = make_plan()
    identity = BackendIdentity(definition="testapi", backend_id="testapi@x", lease_id="bl_1", source="plan")
    key = "sha256:" + "0123456789abcdef" * 4
    request = SeedRestoreRequest(run=run_info(plan), resource_id=BACKEND_ID, session_key="main", seed="standard",
                                 seed_hash=HASH_A, backend=identity, url="http://127.0.0.1:9", now=None, key=key)
    result = FakeSeedHook().restore(request, {})
    assert result.restored and result.snapshot == "fake:" + key[7:23]
    assert (result.resource_id, result.seed, result.key) == (BACKEND_ID, "standard", key)


def test_fake_cassette_hook() -> None:
    plan = make_plan()
    hook = FakeCassetteHook()

    def request(mode: Any, key: str = "k1") -> CassetteRequest:
        return CassetteRequest(run=run_info(plan), node_id="n0000000001", name="c", mode=mode, key=key, backend=None)

    assert hook.available(request("record")) and hook.available(request("auto"))
    assert not hook.available(request("replay"))
    assert hook.opened(request("replay")) == {"mode": "replay", "key": "k1"}
    assert hook.closed(request("record"), {"calls": 3}) is None
    assert hook.recorded == {"k1": {"calls": 3}}
    assert hook.available(request("replay"))
    assert not hook.available(request("replay", key="k2"))
