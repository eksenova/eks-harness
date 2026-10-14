from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_engine_scheduler import Rig, register_probes, web_flow

import eks
from eks_harness.config import Config
from eks_harness.daemon.context import AppContext
from eks_harness.db import Database
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.engine.cache import (
    CacheService,
    required_resolution,
    resolution_problems,
)
from eks_harness.engine.model import ControlRequest, FakeAnswer, FakeProfile, RunOptions
from eks_harness.ir.hashing import machine_dependent
from eks_harness.ir.plan import Node
from eks_harness.ir.registry import Registry, default_registry, using_registry
from eks_harness.ir.types import Artifact
from eks_harness.ops.fakes import default_resolution


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        register_probes()
        yield chosen
    finally:
        chosen.restore(state)


@pytest.fixture
def make_rig(ctx: AppContext, db: Database, config: Config, tmp_path: Path) -> Callable[..., Rig]:
    def build(plan: Any, **kwargs: Any) -> Rig:
        return Rig(ctx=ctx, db=db, config=config, tmp_path=tmp_path, plan=plan, **kwargs)

    return build


def cache_events(rig: Rig, node_id: str) -> list[dict[str, Any]]:
    return [dict(event.detail) for event in rig.events("node.cache", node_id)]


def shot_flow(name: str = "shots", value: int = 7) -> Any:
    def body(s: eks.Session) -> Any:
        return s.record_op("probe.shot", {"value": value, "mode": "file"})

    return web_flow(body, name=name)


def test_pure_ops_store_then_hit(make_rig: Callable[..., Rig]) -> None:
    plan = shot_flow()
    (node,) = plan.nodes
    first = make_rig(plan)
    outcome = first.run()
    assert outcome.status == "passed"
    assert (outcome.cache.misses, outcome.cache.stored, outcome.cache.hits) == (1, 1, 0)
    record = first.node(node.id)
    assert record.cache == "stored" and record.cache_key is not None
    (stored,) = cache_events(first, node.id)
    assert stored == {"outcome": "stored", "key": record.cache_key}
    produced = Artifact.model_validate(record.output)
    assert record.artifacts == [produced.id]

    second = make_rig(plan)
    assert second.deps.values is not first.deps.values
    outcome = second.run()
    assert outcome.status == "passed"
    assert (outcome.cache.hits, outcome.cache.misses, outcome.cache.stored) == (1, 0, 0)
    hit = second.node(node.id)
    assert hit.cache == "hit" and hit.cache_key == record.cache_key
    assert hit.output == record.output and hit.artifacts == [produced.id]
    (served,) = cache_events(second, node.id)
    assert served["outcome"] == "hit" and served["key"] == record.cache_key and served["rematerialized"] is False
    assert second.probe.calls == []

    refresh = make_rig(plan, options=RunOptions(cache="refresh"))
    outcome = refresh.run()
    assert (outcome.cache.hits, outcome.cache.misses, outcome.cache.stored) == (0, 0, 1)
    assert len(refresh.probe.calls) == 1
    assert refresh.node(node.id).cache == "stored"

    readonly = make_rig(shot_flow("readonly", 9), options=RunOptions(cache="readonly"))
    outcome = readonly.run()
    assert (outcome.cache.hits, outcome.cache.misses, outcome.cache.stored) == (0, 1, 0)
    assert readonly.node(readonly.plan.nodes[0].id).cache == "miss"
    again = make_rig(shot_flow("readonly", 9), options=RunOptions(cache="readonly"))
    assert again.run().cache.misses == 1

    off = make_rig(plan, options=RunOptions(cache="off"))
    outcome = off.run()
    assert outcome.cache.model_dump() == {"hits": 0, "misses": 0, "stored": 0, "bypassed": 0}
    assert off.node(node.id).cache == "off" and cache_events(off, node.id) == []

    edited = make_rig(plan, options=RunOptions(breakpoints=[node.id]))
    thread = edited.start()
    edited.wait_paused()
    answer = edited.scheduler.control(ControlRequest(action="edit", args={"value": 8, "mode": "file"}))
    assert answer.ok, answer.message
    assert edited.scheduler.control(ControlRequest(action="continue")).ok
    thread.join(10)
    assert edited.outcome is not None and edited.outcome.status == "passed"
    assert edited.outcome.cache.bypassed == 1 and edited.outcome.cache.stored == 0
    record = edited.node(node.id)
    assert record.cache == "bypass" and record.edited
    (bypass,) = cache_events(edited, node.id)
    assert bypass["outcome"] == "bypass" and "edited" in bypass["reason"]


def media_plan(uri: str, name: str) -> Any:
    def body(s: eks.Session) -> Any:
        source = s.record_op("media.source", {"uri": uri, "kind": "video"})
        return s.record_op("probe.pure", {"value": source})

    return web_flow(body, name=name)


def test_media_content_keys_and_pins(make_rig: Callable[..., Rig], tmp_path: Path, db: Database) -> None:
    clip = tmp_path / "clip.bin"
    clip.write_bytes(b"first take")
    plan = media_plan("file:clip.bin", "media")
    source, reader = plan.nodes
    profile = FakeProfile(families=["*"], force=["media.source"])
    first = make_rig(plan, fakes=profile)
    assert first.run().status == "passed"
    content = first.node(source.id).resolution
    digest = "sha256:" + hashlib.sha256(b"first take").hexdigest()
    assert content is not None and content["content"] == {"uri": "file:clip.bin", "sha256": digest,
                                                           "size": len(b"first take"), "source": "file"}
    keys = {node.id: first.node(node.id).cache_key for node in plan.nodes}

    assert first.node(source.id).simulated and not first.node(reader.id).simulated
    second = make_rig(plan, fakes=profile)
    assert second.run().cache.hits == 2
    assert second.node(source.id).cache == "hit" and second.node(source.id).simulated
    assert second.node(reader.id).cache == "hit" and not second.node(reader.id).simulated

    clip.write_bytes(b"second, longer take")
    third = make_rig(plan, fakes=profile)
    outcome = third.run()
    assert outcome.status == "passed"
    assert (outcome.cache.hits, outcome.cache.misses) == (0, 2)
    for node in plan.nodes:
        record = third.node(node.id)
        assert record.cache == "stored" and record.cache_key != keys[node.id]
    changed = third.node(source.id).resolution
    assert changed is not None and isinstance(changed["content"], dict)
    assert changed["content"]["sha256"] == "sha256:" + hashlib.sha256(b"second, longer take").hexdigest()

    artifact = first.artifacts.ingest(clip, project="acme/scheduler", session="scheduler-tests", kind="video",
                                      name="clip.bin", caption="", tags=[], meta={}, actor=None, move=False)
    pinned = media_plan(f"artifact:{artifact.id}@sha256:{'0' * 64}", "pinned")
    rig = make_rig(pinned, fakes=profile)
    outcome = rig.run()
    assert outcome.status == "failed"
    record = rig.node(pinned.nodes[0].id)
    assert record.error is not None and record.error.code == "content_mismatch"
    assert f"artifact {artifact.id} now holds" in record.error.message


def machine_plan(name: str = "machine") -> Any:
    def body(s: eks.Session) -> Any:
        return s.record_op("probe.machine", {"value": 5})

    return web_flow(body, name=name)


def test_machine_dependent_nodes_key_by_their_resolution(make_rig: Callable[..., Rig]) -> None:
    plan = machine_plan()
    (node,) = plan.nodes
    profile = FakeProfile(families=["*"], force=["probe.machine"])
    first = make_rig(plan, fakes=profile)
    assert first.run().status == "passed"
    (resolved,) = first.events("node.resolution", node.id)
    record = first.node(node.id)
    assert record.simulated is True and record.executor == "eks.fake@1"
    assert record.resolution is not None and record.resolution["machine"] == resolved.detail["resolution"]
    entry = first.cache.entry(str(record.cache_key))
    assert entry is not None and entry.resolution == resolved.detail["resolution"]

    other = {"device": "cuda:1", "model": "m", "weights": "w", "backend_version": "2"}
    changed = FakeProfile(families=["*"], force=["probe.machine"],
                          answers={node.id: FakeAnswer(value=5, resolution=other)})
    second = make_rig(plan, fakes=changed)
    assert second.run().status == "passed"
    assert second.node(node.id).cache == "stored"
    assert second.node(node.id).cache_key != record.cache_key

    incomplete = FakeProfile(families=["*"], force=["probe.machine"],
                             answers={node.id: FakeAnswer(value=5, resolution={"device": "cpu"})})
    rig = make_rig(plan, fakes=incomplete)
    assert rig.run().status == "failed"
    failed = rig.node(node.id)
    assert failed.error is not None and failed.error.code == "resolution_incomplete"
    assert "weights" in failed.error.message

    rig = make_rig(plan, resolver=True)
    rig.probe.predicted = {"device": "cpu", "model": "m", "weights": "w", "backend_version": "1"}
    assert rig.run().status == "failed"
    failed = rig.node(node.id)
    assert failed.error is not None and failed.error.code == "resolution_mismatch"

    def body(s: eks.Session) -> Any:
        return s.record_op("probe.machine", {"value": 5, "mode": "resolution"})

    reporting = web_flow(body, name="reporting")
    rig = make_rig(reporting, resolver=True)
    rig.probe.predicted = {"device": "cpu", "model": "m", "weights": "w", "backend_version": "1"}
    rig.probe.resolution = {"device": "cuda", "model": "m", "weights": "w", "backend_version": "1"}
    assert rig.run().status == "failed"
    failed = rig.node(reporting.nodes[0].id)
    assert failed.error is not None and failed.error.code == "resolution_mismatch"
    assert "device" in failed.error.message and failed.has_output is False

    rig = make_rig(reporting, resolver=True)
    rig.probe.predicted = {"device": "cuda", "model": "m", "weights": "w", "backend_version": "1"}
    rig.probe.resolution = dict(rig.probe.predicted)
    assert rig.run().status == "passed"
    assert rig.node(reporting.nodes[0].id).cache == "stored"

    rig = make_rig(plan)
    outcome = rig.run()
    assert outcome.status == "passed" and outcome.cache.bypassed == 1
    bypass = cache_events(rig, node.id)
    assert bypass == [{"outcome": "bypass", "key": None,
                       "reason": "probe.machine is machine-dependent and its executor has no resolver"}]
    assert rig.events("node.resolution") == []


def test_deleted_artifacts_are_rematerialized(make_rig: Callable[..., Rig], db: Database) -> None:
    plan = shot_flow()
    (node,) = plan.nodes
    first = make_rig(plan)
    assert first.run().status == "passed"
    original = Artifact.model_validate(first.node(node.id).output)
    with db.transaction() as conn:
        assert artifacts_repo.delete(conn, original.id)
    second = make_rig(plan)
    outcome = second.run()
    assert outcome.status == "passed" and outcome.cache.hits == 1
    record = second.node(node.id)
    fresh = Artifact.model_validate(record.output)
    assert fresh.id != original.id and fresh.sha256 == original.sha256
    assert record.artifacts == [fresh.id]
    (served,) = cache_events(second, node.id)
    assert served["rematerialized"] is True and served["artifacts"] == [fresh.id]
    path = second.artifacts.file(fresh.id)
    assert path is not None and path.read_bytes() == b"shot 7"
    assert second.probe.calls == []


def test_refresh_with_another_value_diverges(make_rig: Callable[..., Rig]) -> None:
    def body(s: eks.Session) -> Any:
        return s.record_op("probe.pure", {"mode": "count", "key": "shared"})

    plan = web_flow(body, name="counting")
    (node,) = plan.nodes
    first = make_rig(plan)
    assert first.run().outputs == 1
    second = make_rig(plan, options=RunOptions(cache="refresh"))
    second.probe.counters["shared"] = 1
    assert second.run().outputs == 2
    (stored,) = cache_events(second, node.id)
    assert stored == {"outcome": "stored", "key": first.node(node.id).cache_key, "diverged": True}
    third = make_rig(plan, options=RunOptions(cache="refresh"))
    third.probe.counters["shared"] = 1
    assert third.run().outputs == 2
    assert cache_events(third, node.id) == [{"outcome": "stored", "key": first.node(node.id).cache_key}]

    shots = shot_flow("same-bytes")
    make_rig(shots).run()
    again = make_rig(shots, options=RunOptions(cache="refresh"))
    assert again.run().status == "passed"
    (event,) = cache_events(again, shots.nodes[0].id)
    assert "diverged" not in event


def example_nodes(registry: Registry) -> list[Node]:
    nodes: list[Node] = []
    for spec in registry.ops():
        for example in spec.examples:
            nodes.append(Node(id="n0000000001", op=spec.name, args=dict(example), out=spec.out,
                              hash="sha256:" + "a" * 64))
    blender = registry.op("blender.render").examples[0]
    cpu = {**blender, "scene": {**blender["scene"], "render": {**blender["scene"]["render"], "device": "CPU"}}}
    edit = registry.op("edit.render").examples[0]
    clip = edit["timeline"]["tracks"][0]["clips"][0]
    effected = {**clip, "effects": [{"kind": "punch_in"}]}
    timeline = {**edit["timeline"], "tracks": [{"name": "main", "clips": [effected]}]}
    mix = registry.op("audio.mix").examples[0]
    tracks = [{**mix["mix"]["tracks"][0], "denoise": True}, *mix["mix"]["tracks"][1:]]
    stt = {"media": {"$ref": "n0000000002", "path": []},
           "sources": [{"kind": "stt_markers", "name": "voice"}]}
    for op, args in (("blender.render", cpu), ("edit.render", {"timeline": timeline}),
                     ("audio.mix", {"mix": {**mix["mix"], "tracks": tracks}}), ("media.markers", stt),
                     ("gfx.render", {}), ("check.media", {"media": {"$ref": "n0000000002", "path": []},
                                                          "kind": "text", "params": {"engine": "auto"}})):
        nodes.append(Node(id="n0000000003", op=op, args=args, out=registry.op(op).out, hash="sha256:" + "b" * 64))
    return nodes


def test_fake_default_resolution_satisfies_every_requirement() -> None:
    registry = default_registry()
    covered: dict[str, set[str]] = {}
    for node in example_nodes(registry):
        with using_registry(registry):
            if not machine_dependent(node, registry):
                continue
        spec = registry.op(node.op)
        required = required_resolution(node, registry)
        assert resolution_problems(node, registry, default_resolution(spec, node, registry)) == [], node.op
        covered.setdefault(node.op, set()).update(required)
    assert {"blender.render", "check.media", "edit.render", "gfx.render", "media.markers", "media.stems",
            "media.tts", "scene.render", "audio.mix"} <= set(covered)
    assert "platform" in covered["blender.render"] and "gpu.adapter" in covered["blender.render"]
    assert "inference.device" in covered["edit.render"] and "encoder" in covered["edit.render"]
    assert "inference.weights" in covered["audio.mix"]
    assert {"markers.song.backend", "markers.voice.model", "markers.voice.compute_type"} <= covered["media.markers"]
    assert "browser" in covered["scene.render"]
    assert {"engine", "version"} <= covered["check.media"]


class LockedLookups(CacheService):
    def lookup(self, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")


class LockedStores(CacheService):
    def store(self, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")


def test_cache_database_errors_fall_back_to_a_miss(make_rig: Callable[..., Rig]) -> None:
    plan = shot_flow("locked")
    (node,) = plan.nodes
    assert make_rig(plan).run().status == "passed"

    rig = make_rig(plan)
    rig.deps.cache = LockedLookups(rig.config.paths.cache_dir, rig.db, max_bytes=1 << 30, max_age_days=30)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert (outcome.cache.hits, outcome.cache.misses, outcome.cache.stored) == (0, 1, 1)
    assert len(rig.probe.calls) == 1
    warnings = [str(event.detail["message"]) for event in rig.events("node.log", node.id)]
    assert any("database is locked" in message for message in warnings)

    rig = make_rig(shot_flow("unstored", 11))
    rig.deps.cache = LockedStores(rig.config.paths.cache_dir, rig.db, max_bytes=1 << 30, max_age_days=30)
    outcome = rig.run()
    assert outcome.status == "passed"
    assert (outcome.cache.misses, outcome.cache.stored) == (1, 0)
    record = rig.node(rig.plan.nodes[0].id)
    assert record.cache == "miss" and record.status == "passed"
    (missed,) = cache_events(rig, record.node_id)
    assert missed["outcome"] == "miss" and "database is locked" in missed["reason"]
