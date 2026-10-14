from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import JsonValue

import eks
from eks import fx, t
from eks_harness.engine import cache
from eks_harness.engine.cache import (
    CACHE_KEY_VERSION,
    Bypass,
    CacheKey,
    CacheKeyRequest,
    ContentResolver,
    cache_key,
    clock_material,
    content_digest,
    fonts_digest,
    gather_inputs,
    harness_digest,
    plugin_material,
    required_resolution,
    resolution_problems,
)
from eks_harness.engine.errors import ContentError
from eks_harness.engine.executors import ExecResult, ExecutorSpec, ResolveContext
from eks_harness.engine.model import RunClock
from eks_harness.ir.catalog.curves import CurveBase
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.catalog.markers import BeatTracker, STTMarkers
from eks_harness.ir.hashing import (
    cache_inputs,
    canonical_json,
    digest,
    hash_plan,
    machine_dependent,
    pure_closure,
)
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Provenance, Registry, contributing, default_registry
from eks_harness.ir.time import TempoMap
from eks_harness.ir.types import Artifact

PIN_SHA = "sha256:" + "a" * 64
PIN = "@" + PIN_SHA
FOOTAGE = "artifact:VID1" + PIN
REF = {"$ref": "n0000000001", "path": []}


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.rows: dict[str, Artifact] = {}
        self.paths: dict[str, Path] = {}
        self.count = 0

    def add(self, data: bytes, *, kind: str = "image", name: str = "shot.png", record_sha: bool = True) -> Artifact:
        self.count += 1
        artifact_id = f"ART{self.count}"
        path = self.root / f"{artifact_id}-{name}"
        path.write_bytes(data)
        sha = "sha256:" + hashlib.sha256(data).hexdigest() if record_sha else None
        row = Artifact(id=artifact_id, kind=kind, name=name, size=len(data), sha256=sha)
        self.rows[artifact_id] = row
        self.paths[artifact_id] = path
        return row

    def rewrite(self, artifact_id: str, data: bytes) -> Artifact:
        self.paths[artifact_id].write_bytes(data)
        row = self.rows[artifact_id].model_copy(
            update={"size": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()})
        self.rows[artifact_id] = row
        return row

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        data = path.read_bytes()
        if move:
            path.unlink()
        return self.add(data, kind=kind or "file", name=name or path.name)

    def get(self, artifact_id: str) -> Artifact | None:
        return self.rows.get(artifact_id)

    def file(self, artifact_id: str) -> Path | None:
        path = self.paths.get(artifact_id)
        return path if path is not None and path.exists() else None

    def scratch(self) -> Path:
        folder = self.root / f"scratch-{self.count}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


class Digests:
    def __init__(self, plugins: Mapping[str, str | None] | None = None, project: str | None = "sha256:" + "0" * 64):
        self.plugins = dict(plugins or {})
        self.project_digest = project

    def plugin(self, plugin_id: str) -> str | None:
        return self.plugins.get(plugin_id)

    def project(self) -> str | None:
        return self.project_digest


def resolver_of(record: Mapping[str, JsonValue]) -> Callable[[ResolveContext], Mapping[str, JsonValue]]:
    def resolve(_context: ResolveContext) -> Mapping[str, JsonValue]:
        return record

    return resolve


def executor(*, version: str = "1", resolver: Callable[[ResolveContext], Mapping[str, JsonValue]] | None = None,
             runtime: Callable[[], str] | None = None, fonts: bool = False,
             provenance: Provenance | None = None) -> ExecutorSpec:
    return ExecutorSpec(name="test.exec", version=version, ops=("*",), run=lambda _call: ExecResult(), resolver=resolver,
                        runtime_version=runtime, fonts=fonts,
                        provenance=provenance if provenance is not None else Provenance("eks", "0.test"))


def nodes_of(plan: Plan, op: str) -> list[Node]:
    return [node for node, _ancestors in plan.walk() if node.op == op]


def one(plan: Plan, op: str) -> Node:
    (node,) = nodes_of(plan, op)
    return node


def sources_of(plan: Plan, node: Node, registry: Registry) -> dict[str, str]:
    by_id = {candidate.id: candidate for candidate, _ancestors in plan.walk()}
    return {node_id: PIN_SHA for node_id in pure_closure(plan, node, registry)
            if node_id != node.id and by_id[node_id].op == "media.source"}


def key_of(plan: Plan, node: Node, registry: Registry, *, spec: ExecutorSpec | None = None,
           inputs: Mapping[str, str] | None = None, resolution: Mapping[str, JsonValue] | None = None,
           digests: Digests | None = None, clock: RunClock | None = None) -> CacheKey | Bypass:
    chosen = inputs if inputs is not None else sources_of(plan, node, registry)
    return cache_key(CacheKeyRequest(plan=plan, node=node, registry=registry, executor=spec or executor(),
                                     inputs=chosen, resolution=resolution, plugins=digests or Digests(),
                                     clock=clock))


def key(plan: Plan, node: Node, registry: Registry, **options: Any) -> CacheKey:
    found = key_of(plan, node, registry, **options)
    assert isinstance(found, CacheKey), found
    return found


def mk(op: str, args: dict[str, Any], out: str = "media") -> Node:
    return Node(id="n0000000002", op=op, args=args, out=out)


# BL-001: resolution requirements


def markers_node(*sources: dict[str, Any]) -> Node:
    return mk("media.markers", {"media": REF, "sources": list(sources)}, out="json")


def test_markers_require_backend_fields_per_unpinned_source(registry: Registry) -> None:
    node = markers_node({"kind": "beat_tracker", "name": "song"},
                        {"kind": "beat_tracker", "name": "fixed", "backend": "librosa"},
                        {"kind": "stt_markers", "name": "vo"},
                        {"kind": "onset_markers", "name": "hits"})
    assert required_resolution(node, registry) == (
        "markers.song.backend", "markers.song.version",
        "markers.vo.model", "markers.vo.device", "markers.vo.compute_type", "markers.vo.backend",
        "markers.vo.version",
    )
    pinned = markers_node({"kind": "stt_markers", "name": "vo", "model": "large-v3", "device": "cpu",
                           "compute_type": "int8"})
    assert required_resolution(pinned, registry) == ()


def test_media_checks_require_engine_only_when_machine_dependent(registry: Registry) -> None:
    auto = mk("check.media", {"media": REF, "kind": "text", "params": {"text": "x", "engine": "auto"}}, "json")
    pinned = mk("check.media", {"media": REF, "kind": "text", "params": {"text": "x", "engine": "tesseract"}}, "json")
    assert required_resolution(auto, registry) == ("engine", "version")
    assert required_resolution(pinned, registry) == ()


def edit_args(effects: list[dict[str, Any]], encoder: str) -> dict[str, Any]:
    return {"timeline": {"fps": 30, "size": [1080, 1920], "render": {"encoder": encoder},
                         "tracks": [{"name": "main", "clips": [{"id": "main/0", "media": REF, "effects": effects}]}]}}


def test_render_rows(registry: Registry) -> None:
    inference = ("inference.device", "inference.model", "inference.weights", "inference.backend_version")
    assert required_resolution(mk("edit.render", edit_args([], "nvenc")), registry) == ("encoder", "encoder_version")
    assert required_resolution(mk("edit.render", edit_args([{"kind": "zoom", "scale": 1.2}], "libx264")),
                               registry) == ()
    assert required_resolution(mk("edit.render", edit_args([{"kind": "birefnet_remove"}], "libx264")),
                               registry) == inference
    assert required_resolution(mk("edit.render", edit_args([{"kind": "auto_reframe"}], "qsv")),
                               registry) == ("encoder", "encoder_version", *inference)
    assert required_resolution(mk("gfx.render", {"scene": {}}), registry) == ("gpu.adapter", "gpu.driver")
    assert required_resolution(mk("gfx.render", {"cpu": True}), registry) == ()
    assert required_resolution(mk("scene.render", {}), registry) == ("browser",)
    assert required_resolution(mk("media.tts", {"text": "merhaba"}), registry) == (
        "device", "model", "weights", "backend_version")
    assert required_resolution(mk("media.stems", {"media": REF, "stems": ["vocals"]}), registry) == (
        "device", "model", "weights", "backend_version")
    assert required_resolution(mk("audio.mix", {"mix": {"tracks": [{"name": "vo", "stems": ["vocals"]}]}}),
                               registry) == inference
    assert required_resolution(mk("audio.mix", {"mix": {"tracks": [{"name": "vo", "denoise": {}}]}}),
                               registry) == inference
    assert required_resolution(mk("audio.mix", {"mix": {"tracks": [{"name": "vo"}]}}), registry) == ()


def blender(engine: str, device: str, platform: str | None = None) -> Node:
    render: dict[str, Any] = {"engine": engine, "device": device}
    if platform is not None:
        render["platform"] = platform
    return mk("blender.render", {"scene": {"render": render}})


def test_blender_rows(registry: Registry) -> None:
    assert required_resolution(blender("CYCLES", "CPU"), registry) == ("platform",)
    assert required_resolution(blender("CYCLES", "CPU", "linux-x86_64"), registry) == ()
    assert required_resolution(blender("CYCLES", "GPU"), registry) == ("gpu.adapter", "gpu.driver")
    assert required_resolution(blender("BLENDER_EEVEE_NEXT", "GPU"), registry) == ("gpu.adapter", "gpu.driver")
    assert required_resolution(blender("BLENDER_WORKBENCH", "GPU"), registry) == ("gpu.adapter", "gpu.driver")


def test_a_device_dependent_plugin_op_requires_device_fields(registry: Registry) -> None:
    with contributing("acme.ml", "1.0"):
        @eks.op("acme", name="upscale", out="media", pure=True, location="node", device_dependent=True)
        class Upscale(eks.Args):
            factor: int = 2

    assert required_resolution(mk("acme.upscale", {"factor": 4}), registry) == (
        "device", "model", "weights", "backend_version")
    assert Upscale(factor=3).factor == 3


def test_resolution_problems_lists_missing_and_empty_fields(registry: Registry) -> None:
    node = markers_node({"kind": "beat_tracker", "name": "song"})
    assert resolution_problems(node, registry, None) == ["markers.song.backend", "markers.song.version"]
    assert resolution_problems(node, registry, {"markers": {"song": {"backend": "madmom", "version": ""}}}) == [
        "markers.song.version"]
    assert resolution_problems(node, registry, {"markers": {"song": {"backend": "madmom", "version": "0.16"}}}) == []
    assert resolution_problems(node, registry, {"markers.song.backend": "madmom",
                                                "markers.song.version": "0.16"}) == []
    assert resolution_problems(node, registry, {"markers": {"song": {"backend": None, "version": []}}}) == [
        "markers.song.backend", "markers.song.version"]


def beat_plan(source: BeatTracker | STTMarkers) -> Plan:
    @eks.edit(name="beats", fps=30, size=(1080, 1920), duration=t("4s"))
    def edit(e: eks.Edit) -> None:
        song = e.media(FOOTAGE)
        eks.media.markers(song, source)
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media(FOOTAGE))

    return eks.plan(edit)


def song_record(version: str = "0.16") -> dict[str, JsonValue]:
    return {"markers": {"song": {"backend": "madmom", "version": version}}, "host": "a"}


def test_the_key_holds_the_whole_resolution_record(registry: Registry) -> None:
    plan = beat_plan(BeatTracker(name="song"))
    markers = one(plan, "media.markers")
    assert machine_dependent(markers, registry)
    spec = executor(resolver=resolver_of(song_record()))
    found = key(plan, markers, registry, spec=spec, resolution={"z": 1, **song_record()})
    assert found.material["resolution"] == {"host": "a", "markers": {"song": {"backend": "madmom", "version": "0.16"}},
                                            "z": 1}
    assert found.key == digest(canonical_json(dict(found.material)))
    assert found.material["inputs"] == {one(plan, "media.source").id: PIN_SHA}
    again = key(plan, markers, registry, spec=spec, resolution={"z": 1, **song_record()})
    assert again.key == found.key
    changed = key(plan, markers, registry, spec=spec, resolution={"z": 1, **song_record("0.17")})
    assert changed.key != found.key
    assert key(plan, markers, registry, spec=spec, resolution={"z": 2, **song_record()}).key != found.key


def test_an_incomplete_or_missing_record_is_refused(registry: Registry) -> None:
    plan = beat_plan(BeatTracker(name="song"))
    markers = one(plan, "media.markers")
    spec = executor(resolver=resolver_of(song_record()))
    with pytest.raises(ValueError, match="markers.song.version"):
        key_of(plan, markers, registry, spec=spec, resolution={"markers": {"song": {"backend": "madmom"}}})
    with pytest.raises(ValueError, match="resolve it"):
        key_of(plan, markers, registry, spec=spec, resolution=None)


def test_a_machine_dependent_node_without_a_resolver_bypasses(registry: Registry) -> None:
    plan = beat_plan(BeatTracker(name="song"))
    markers = one(plan, "media.markers")
    found = key_of(plan, markers, registry, spec=executor(), resolution=song_record())
    assert found == Bypass("media.markers is machine-dependent and its executor has no resolver")


def captions_plan(source: STTMarkers) -> Plan:
    @eks.edit(name="captioned", fps=30, size=(1080, 1920), seed=7, duration=t("10s"))
    def edit(e: eks.Edit) -> None:
        voice = e.media("artifact:VO1" + PIN)
        eks.media.markers(voice, source)
        e.track("main").clip(e.media(FOOTAGE), start=t("0s")).effect(fx.captions("vo"))
        e.render_settings(encoder="libx264")

    return eks.plan(edit)


def test_the_readers_of_an_unpinned_analysis_are_keyed_by_its_output(registry: Registry, tmp_path: Path) -> None:
    plan = captions_plan(STTMarkers(name="vo", language="tr"))
    markers = one(plan, "media.markers")
    render = one(plan, "edit.render")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    assert cache_inputs(plan, render, registry) == (markers.id,)
    sources = sources_of(plan, render, registry)
    assert len(sources) == 2
    words: dict[str, JsonValue] = {"vo": {"words": [{"text": "merhaba", "start": 0.5}]}}
    inputs = gather_inputs(plan, render, registry, value_of=lambda _node: words, content=content, artifacts=store,
                           override=sources.get)
    assert inputs == {**sources, markers.id: digest(canonical_json(words))}
    first = key(plan, render, registry, inputs=inputs)
    assert first.material["resolution"] is None
    other: dict[str, JsonValue] = {"vo": {"words": [{"text": "selam", "start": 0.5}]}}
    changed = gather_inputs(plan, render, registry, value_of=lambda _node: other, content=content, artifacts=store,
                            override=sources.get)
    assert key(plan, render, registry, inputs=changed).key != first.key


def test_a_pure_node_without_inputs_keys_with_empty_inputs(registry: Registry) -> None:
    @eks.scene(name="card", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
    def card(sc: eks.GfxScene) -> None:
        sc.rect(width=200.0, height=200.0, fill="#f5c542")

    plan = eks.plan(card)
    render = one(plan, "gfx.render")
    assert cache_inputs(plan, render, registry) == ()
    first = key(plan, render, registry)
    assert first.material["inputs"] == {}
    assert first.material["resolution"] is None
    assert first.material["v"] == CACHE_KEY_VERSION
    assert first.material["node"] == render.hash
    assert key(plan, render, registry).key == first.key


def test_an_impure_node_bypasses(registry: Registry) -> None:
    @eks.op("acme", name="probe", out="json")
    class Probe(eks.Args):
        pass

    @eks.flow(app="web-app", platform="web")
    def probe(s: eks.Session) -> None:
        s.record_op("acme.probe", {})

    plan = eks.plan(probe)
    assert key_of(plan, one(plan, "acme.probe"), registry) == Bypass("acme.probe is not pure")
    assert Probe().model_dump() == {}


def test_inputs_must_cover_cache_inputs_exactly(registry: Registry) -> None:
    plan = still("artifact:SHOT1")
    render = one(plan, "edit.render")
    source = one(plan, "media.source")
    with pytest.raises(ValueError, match="lacks"):
        key_of(plan, render, registry, inputs={})
    with pytest.raises(ValueError, match="extra"):
        key_of(plan, render, registry, inputs={source.id: "sha256:" + "1" * 64, "n0000000099": "x"})


# BL-002: content


def still(uri: str) -> Plan:
    @eks.edit(name=f"still_{abs(hash(uri))}", fps=30, size=(1080, 1920), duration=t("3s"))
    def edit(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media(uri), start=t("0s"))

    return eks.plan(edit)


def render_key(plan: Plan, registry: Registry, content: ContentResolver, store: FileStore) -> CacheKey:
    render = one(plan, "edit.render")
    inputs = gather_inputs(plan, render, registry, value_of=lambda _node: None, content=content, artifacts=store)
    return key(plan, render, registry, inputs=inputs)


def test_a_file_source_keys_by_its_bytes(registry: Registry, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"first take")
    plan = still(f"file:{clip}")
    source = one(plan, "media.source")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    first = render_key(plan, registry, content, store)
    assert first.material["inputs"] == {source.id: "sha256:" + hashlib.sha256(b"first take").hexdigest()}
    clip.write_bytes(b"second take, longer")
    second = render_key(plan, registry, content, store)
    assert second.key != first.key
    own = gather_inputs(plan, source, registry, value_of=lambda _node: None, content=content, artifacts=store)
    assert own == {source.id: "sha256:" + hashlib.sha256(b"second take, longer").hexdigest()}


def test_an_artifact_source_keys_by_the_stores_sha256(registry: Registry, tmp_path: Path) -> None:
    store = FileStore(tmp_path / "store")
    shot = store.add(b"v1")
    plan = still(f"artifact:{shot.id}")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    first = render_key(plan, registry, content, store)
    store.rewrite(shot.id, b"v2")
    fresh = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    second = render_key(plan, registry, fresh, store)
    assert second.key != first.key


def test_an_artifact_output_input_keys_by_the_stores_sha256(registry: Registry, tmp_path: Path) -> None:
    @eks.op("acme", name="snap", out="artifact")
    class Snap(eks.Args):
        pass

    @eks.op("acme", name="frame", out="artifact", pure=True)
    class Frame(eks.Args):
        image: Any = None

    @eks.flow(app="web-app", platform="web")
    def snapping(s: eks.Session) -> None:
        shot = s.record_op("acme.snap", {})
        s.record_op("acme.frame", {"image": shot})

    plan = eks.plan(snapping)
    snap, frame = one(plan, "acme.snap"), one(plan, "acme.frame")
    store = FileStore(tmp_path / "store")
    shot = store.add(b"pixels")
    value: JsonValue = shot.model_dump(mode="json")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    inputs = gather_inputs(plan, frame, registry, value_of=lambda _node: value, content=content, artifacts=store)
    assert inputs == {snap.id: shot.sha256}
    first = key(plan, frame, registry, inputs=inputs)
    store.rewrite(shot.id, b"other pixels")
    changed = gather_inputs(plan, frame, registry, value_of=lambda _node: value, content=content, artifacts=store)
    assert changed != inputs
    assert key(plan, frame, registry, inputs=changed).key != first.key
    del store.rows[shot.id]
    with pytest.raises(ContentError) as gone:
        gather_inputs(plan, frame, registry, value_of=lambda _node: value, content=content, artifacts=store)
    assert gone.value.code == "content_unavailable"
    assert Snap().model_dump() == {} and Frame().image is None


def test_an_override_digest_wins(registry: Registry, tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"bytes")
    plan = still(f"file:{clip}")
    source = one(plan, "media.source")
    render = one(plan, "edit.render")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    pinned = "sha256:" + "f" * 64
    inputs = gather_inputs(plan, render, registry, value_of=lambda _node: None, content=content, artifacts=store,
                           override=lambda node_id: pinned if node_id == source.id else None)
    assert inputs == {source.id: pinned}
    clip.unlink()
    again = gather_inputs(plan, render, registry, value_of=lambda _node: None, content=content, artifacts=store,
                          override=lambda node_id: pinned if node_id == source.id else None)
    assert again == inputs


def test_a_missing_file_is_unavailable(registry: Registry, tmp_path: Path) -> None:
    plan = still(f"file:{tmp_path / 'gone.mp4'}")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    with pytest.raises(ContentError) as missing:
        render_key(plan, registry, content, store)
    assert missing.value.code == "content_unavailable"
    assert "gone.mp4" in missing.value.message


def test_an_empty_store_digest_is_hashed_from_the_file(registry: Registry, tmp_path: Path) -> None:
    store = FileStore(tmp_path / "store")
    first = store.add(b"legacy one")
    second = store.add(b"legacy two")
    for row in (first, second):
        store.rows[row.id] = row.model_copy(update={"sha256": "sha256:"})
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    one_sha = "sha256:" + hashlib.sha256(b"legacy one").hexdigest()
    two_sha = "sha256:" + hashlib.sha256(b"legacy two").hexdigest()
    assert content.media(f"artifact:{first.id}").sha256 == one_sha
    assert content.media(f"artifact:{second.id}").sha256 == two_sha
    assert content.media(f"artifact:{first.id}@{one_sha}").sha256 == one_sha
    assert content_digest({"id": first.id, "kind": "image"}, "artifact", store) == one_sha
    assert content_digest({"id": second.id, "kind": "image"}, "artifact", store) == two_sha
    assert content_digest({"uri": f"artifact:{second.id}", "kind": "image", "artifact": second.id}, "media",
                          store) == two_sha


def test_a_pinned_artifact_that_moved_on_is_a_mismatch(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "store")
    shot = store.add(b"v1")
    pinned = shot.sha256
    assert pinned is not None
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    uri = f"artifact:{shot.id}@{pinned}"
    assert content.media(uri).sha256 == pinned
    current = store.rewrite(shot.id, b"v2").sha256
    with pytest.raises(ContentError) as mismatch:
        ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "s2").media(uri)
    assert mismatch.value.code == "content_mismatch"
    assert mismatch.value.message == (
        f"artifact {shot.id} now holds {current}; the plan pins {pinned}. Restore that version or plan again with "
        "the new digest")


def each_plan(*, read_value: bool = False) -> Plan:
    @eks.op("acme", name="items", out="list[str]")
    class Items(eks.Args):
        pass

    @eks.op("acme", name="shape", out="json", pure=True)
    class Shape(eks.Args):
        value: Any = None
        total: Any = None

    @eks.flow(app="web-app", platform="web")
    def items(s: eks.Session) -> None:
        with eks.each(s.record_op("acme.items", {})) as item:
            s.record_op("acme.shape", {"value": item})

    plan = eks.plan(items)
    assert Items().model_dump() == {} and Shape().value is None
    if not read_value:
        return plan
    each, shape = one(plan, "ctl.each"), one(plan, "acme.shape")
    reading = shape.model_copy(update={"args": {**shape.args, "total": {"$ref": each.id, "path": []}}})
    return plan.model_copy(update={"nodes": [
        node.model_copy(update={"branches": {name: [reading if child.id == shape.id else child for child in children]
                                             for name, children in (node.branches or {}).items()}})
        if node.id == each.id else node for node in plan.nodes]})


def test_an_each_item_input_uses_the_items_value_digest(registry: Registry, tmp_path: Path) -> None:
    plan = each_plan()
    each, shape = one(plan, "ctl.each"), one(plan, "acme.shape")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    items: dict[str, JsonValue] = {each.id: "second"}
    inputs = gather_inputs(plan, shape, registry, value_of=lambda node_id: pytest.fail(f"value of {node_id}"),
                           content=content, artifacts=store, item_of=items.__getitem__)
    assert inputs == {each.id: digest(canonical_json("second"))}
    items[each.id] = "third"
    assert key(plan, shape, registry, inputs=inputs).key != key(
        plan, shape, registry,
        inputs=gather_inputs(plan, shape, registry, value_of=lambda _node: None, content=content, artifacts=store,
                             item_of=items.__getitem__)).key
    with pytest.raises(ValueError, match="pass item_of"):
        gather_inputs(plan, shape, registry, value_of=lambda _node: None, content=content, artifacts=store)


def test_each_iterations_over_a_value_store_key_apart(registry: Registry, tmp_path: Path) -> None:
    from eks_harness.engine.model import ROOT_INSTANCE
    from eks_harness.engine.values import ValueStore, child_instance, each_segment

    plan = each_plan()
    each, shape = one(plan, "ctl.each"), one(plan, "acme.shape")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    values = ValueStore()
    values.set_items(each.id, ROOT_INSTANCE, ["red", "blue"])
    keys: list[str] = []
    for index in range(2):
        instance = child_instance(ROOT_INSTANCE, each_segment(each.id, index))
        inputs = gather_inputs(plan, shape, registry, value_of=lambda node_id, at=instance: values.get(node_id, at),
                               content=content, artifacts=store,
                               item_of=lambda each_id, at=instance: values.item(each_id, at))
        keys.append(key(plan, shape, registry, inputs=inputs).key)
    assert keys[0] != keys[1]


def test_an_each_read_by_value_and_item_keys_both(registry: Registry, tmp_path: Path) -> None:
    plan = each_plan(read_value=True)
    each, shape = one(plan, "ctl.each"), one(plan, "acme.shape")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    values: dict[str, JsonValue] = {each.id: ["red", "blue"]}
    items: dict[str, JsonValue] = {each.id: "red"}

    def inputs() -> dict[str, str]:
        return gather_inputs(plan, shape, registry, value_of=values.__getitem__, content=content, artifacts=store,
                             item_of=items.__getitem__)

    first = inputs()
    assert first == {each.id: digest(canonical_json({"item": digest(canonical_json("red")),
                                                     "value": digest(canonical_json(["red", "blue"]))}))}
    items[each.id] = "blue"
    assert inputs() != first
    items[each.id] = "red"
    values[each.id] = ["red", "green"]
    assert inputs() != first


def test_a_bundle_source_keys_by_its_directory(registry: Registry, tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "img").mkdir(parents=True)
    (site / "index.html").write_text("<html></html>")
    (site / "style.css").write_text("body { color: red }")
    (site / "img" / "logo.svg").write_text("<svg/>")
    plan = still(f"file:{site / 'index.html'}")
    source = one(plan, "media.source")
    bundle = source.model_copy(update={"args": {**source.args, "kind": "bundle"}})
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")

    def own() -> str:
        return gather_inputs(plan, bundle, registry, value_of=lambda _node: None, content=content,
                             artifacts=store)[bundle.id]

    first = own()
    assert first == cache.content_hash(site)
    assert first != content.media(f"file:{site / 'index.html'}").sha256
    assert own() == first
    (site / "style.css").write_text("body { color: blue; margin: 0 }")
    second = own()
    assert second != first
    (site / "img" / "logo.svg").write_text("<svg width='2'/>")
    assert own() != second
    (site / "index.html").unlink()
    with pytest.raises(ContentError) as missing:
        own()
    assert missing.value.code == "content_unavailable"


def test_a_bundle_digest_is_memoized_by_its_files(registry: Registry, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>")
    (site / "app.js").write_text("run()")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    calls: list[Path] = []
    real = cache.content_hash
    monkeypatch.setattr(cache, "content_hash", lambda root: calls.append(root) or real(root))
    uri = f"file:{site / 'index.html'}"
    first = content._bundle_digest(uri)
    assert content._bundle_digest(uri) == first
    assert calls == [site]
    (site / "app.js").write_text("run(); stop()")
    assert content._bundle_digest(uri) != first
    assert len(calls) == 2


def test_content_digest_by_output_type(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "store")
    image = store.add(b"img")
    bare = store.add(b"no sha recorded", record_sha=False)
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    dump: JsonValue = image.model_dump(mode="json")
    assert content_digest(dump, "artifact", store) == image.sha256
    assert content_digest({"id": bare.id, "kind": "image"}, "artifact", store) == (
        "sha256:" + hashlib.sha256(b"no sha recorded").hexdigest())
    media: JsonValue = {"uri": f"artifact:{image.id}", "kind": "image", "artifact": image.id}
    assert content_digest(media, "media", store) == image.sha256
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"wave")
    by_uri: JsonValue = {"uri": f"file:{clip}", "kind": "audio"}
    assert content_digest(by_uri, "media", store, content=content) == "sha256:" + hashlib.sha256(b"wave").hexdigest()
    with pytest.raises(ContentError):
        content_digest(by_uri, "media", store)
    plain: JsonValue = {"uri": "memory:x", "kind": "audio"}
    assert content_digest(plain, "media", store) == digest(canonical_json(plain))
    listed = content_digest([dump, dump], "list[artifact]", store)
    assert listed == digest(canonical_json([image.sha256, image.sha256]))
    assert content_digest(["a", "b"], "list[str]", store) == digest(canonical_json(["a", "b"]))
    assert content_digest({"x": 1}, "json", store) == digest(canonical_json({"x": 1}))


# BL-003: executor versions, runtime versions, harness and fonts digests


def gfx_render(registry: Registry) -> tuple[Plan, Node]:
    @eks.scene(name="card", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
    def card(sc: eks.GfxScene) -> None:
        sc.text("EksenGOLD", id="title")

    plan = eks.plan(card)
    return plan, one(plan, "gfx.render")


def test_the_key_changes_with_executor_version_and_runtime(registry: Registry) -> None:
    plan, render = gfx_render(registry)
    base = key(plan, render, registry, spec=executor(version="1"))
    assert key(plan, render, registry, spec=executor(version="2")).key != base.key
    with_runtime = key(plan, render, registry, spec=executor(runtime=lambda: "blender 4.2"))
    assert with_runtime.key != base.key
    assert key(plan, render, registry, spec=executor(runtime=lambda: "blender 4.3")).key != with_runtime.key
    material: Any = with_runtime.material["executor"]
    assert material["runtime"] == "blender 4.2"
    assert material["name"] == "test.exec"


def test_the_harness_digest_keys_built_in_executors_only(registry: Registry,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    plan, render = gfx_render(registry)
    plugin = executor(provenance=Provenance("acme.render", "1.0"))
    built_in = key(plan, render, registry)
    from_plugin = key(plan, render, registry, spec=plugin, digests=Digests({"acme.render": "sha256:" + "2" * 64}))
    material: Any = built_in.material["executor"]
    assert material["harness"] == harness_digest()
    assert material["harness"].startswith("sha256:")
    assert material["fonts"] is None
    monkeypatch.setitem(cache._DIGESTS, "harness", "sha256:" + "9" * 64)
    assert key(plan, render, registry).key != built_in.key
    assert key(plan, render, registry, spec=plugin,
               digests=Digests({"acme.render": "sha256:" + "2" * 64})).key == from_plugin.key


def test_the_fonts_digest_keys_text_executors_only(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, render = gfx_render(registry)
    texts = key(plan, render, registry, spec=executor(fonts=True))
    plain = key(plan, render, registry)
    material: Any = texts.material["executor"]
    assert material["fonts"] == fonts_digest()
    monkeypatch.setitem(cache._DIGESTS, "fonts", "sha256:" + "8" * 64)
    assert key(plan, render, registry, spec=executor(fonts=True)).key != texts.key
    assert key(plan, render, registry).key == plain.key


def test_digests_are_computed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    first = harness_digest()
    fonts = fonts_digest()
    calls: list[str] = []
    monkeypatch.setattr(cache, "content_hash", lambda root: calls.append(str(root)) or "sha256:" + "0" * 64)
    assert harness_digest() == first
    assert fonts_digest() == fonts
    assert calls == []
    monkeypatch.delitem(cache._DIGESTS, "fonts")
    assert fonts_digest() == "sha256:" + "0" * 64
    assert calls[0].endswith("fonts")


def framed_plan() -> Plan:
    @eks.scene(name="badge", kind="gfx", size=(1080, 1920), duration=t("2s"), fps=30, cpu=True)
    def badge(sc: eks.GfxScene) -> None:
        sc.rect(width=400.0, height=400.0, fill="#f5c542")

    @eks.edit(name="framed", fps=30, size=(1080, 1920), duration=t("2s"))
    def edit(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.gfx(badge), start=t("0s"))

    return eks.plan(edit)


def test_a_pure_upstream_render_keys_its_reader_by_what_it_produced(registry: Registry, tmp_path: Path) -> None:
    plan = framed_plan()
    gfx, render = one(plan, "gfx.render"), one(plan, "edit.render")
    assert cache_inputs(plan, gfx, registry) == () and cache_inputs(plan, render, registry) == ()
    assert not machine_dependent(gfx, registry) and not machine_dependent(render, registry)
    assert pure_closure(plan, render, registry) == (gfx.id, render.id)
    upgraded = key(plan, gfx, registry, spec=executor(version="2"))
    assert upgraded.key != key(plan, gfx, registry, spec=executor(version="1")).key
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    before = store.add(b"frames of executor 1", kind="video", name="badge.mp4")
    after = store.add(b"frames of executor 2", kind="video", name="badge.mp4")
    served = store.add(b"frames of executor 1", kind="video", name="badge.mp4")

    def reader(produced: Artifact) -> CacheKey:
        value: JsonValue = {"uri": f"artifact:{produced.id}", "kind": "video", "artifact": produced.id}
        inputs = gather_inputs(plan, render, registry, value_of=lambda _node: value, content=content, artifacts=store)
        return key(plan, render, registry, inputs=inputs)

    first = reader(before)
    assert first.material["inputs"] == {gfx.id: before.sha256}
    assert reader(after).key != first.key
    assert reader(served).key == first.key
    with pytest.raises(ValueError, match="lacks"):
        key_of(plan, render, registry, inputs={})


def test_a_plugin_executor_upstream_changes_its_readers_through_their_inputs(registry: Registry,
                                                                            tmp_path: Path) -> None:
    plan = framed_plan()
    gfx, render = one(plan, "gfx.render"), one(plan, "edit.render")
    runner = executor(provenance=Provenance("acme.render", "1.0"))
    first_code = Digests({"acme.render": "sha256:" + "1" * 64})
    second_code = Digests({"acme.render": "sha256:" + "2" * 64})
    assert key(plan, gfx, registry, spec=runner, digests=first_code).key != key(
        plan, gfx, registry, spec=runner, digests=second_code).key
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=None, base=".", plugin_roots={}, scratch=tmp_path / "scratch")
    keys: list[str] = []
    for data in (b"plugin build one", b"plugin build two"):
        produced = store.add(data, kind="video", name="badge.mp4")
        value: JsonValue = {"uri": f"artifact:{produced.id}", "kind": "video", "artifact": produced.id}
        inputs = gather_inputs(plan, render, registry, value_of=lambda _node, v=value: v, content=content,
                               artifacts=store)
        keys.append(key(plan, render, registry, inputs=inputs).key)
    assert keys[0] != keys[1]


# BL-004: plugin and project code digests


def register_sparkle() -> None:
    with contributing("acme.fx", "1.0"):
        @eks.effect(description="Sparkles")
        class Sparkle(EffectBase, frozen=True):
            kind: Literal["sparkle"] = "sparkle"
            density: float = 1.0

        @eks.curve(description="A plugin wave")
        class AcmeWave(CurveBase, frozen=True):
            kind: Literal["acme_wave"] = "acme_wave"

        del Sparkle, AcmeWave


def sparkle_plan(effect: EffectBase) -> Plan:
    @eks.edit(name="sparkly", fps=30, size=(1080, 1920), duration=t("3s"))
    def edit(e: eks.Edit) -> None:
        e.render_settings(encoder="libx264")
        e.track("main").clip(e.media(FOOTAGE)).effect(effect)

    return eks.plan(edit)


def test_a_plugin_effect_keys_by_its_plugin_digest(registry: Registry) -> None:
    register_sparkle()
    plan = sparkle_plan(fx.custom("sparkle", density=2.0))
    render = one(plan, "edit.render")
    one_digest = Digests({"acme.fx": "sha256:" + "1" * 64})
    other = Digests({"acme.fx": "sha256:" + "2" * 64})
    first = key(plan, render, registry, digests=one_digest)
    assert first.material["plugins"] == {"acme.fx": "sha256:" + "1" * 64}
    assert key(plan, render, registry, digests=other).key != first.key
    assert key_of(plan, render, registry, digests=Digests({})) == Bypass("plugin acme.fx is not loaded on this hub")


def test_a_plugin_curve_inside_a_pure_render_keys_by_its_digest(registry: Registry) -> None:
    register_sparkle()
    plan = sparkle_plan(fx.flash(t("1s"), t("200ms"), curve=eks.curves.custom("acme_wave")))
    render = one(plan, "edit.render")
    first = key(plan, render, registry, digests=Digests({"acme.fx": "sha256:" + "1" * 64}))
    assert first.material["plugins"] == {"acme.fx": "sha256:" + "1" * 64}
    assert key(plan, render, registry, digests=Digests({"acme.fx": "sha256:" + "3" * 64})).key != first.key


def test_a_pure_input_contributed_by_a_plugin_keys_its_readers(registry: Registry) -> None:
    with contributing("acme.fx", "1.0"):
        @eks.op("acmefx", name="palette", out="json", pure=True)
        class Palette(eks.Args):
            hue: int = 0

    @eks.op("acme", name="paint", out="json", pure=True)
    class Paint(eks.Args):
        palette: Any = None

    @eks.flow(app="web-app", platform="web")
    def painting(s: eks.Session) -> None:
        colors = s.record_op("acmefx.palette", {"hue": 30})
        s.record_op("acme.paint", {"palette": colors})

    plan = eks.plan(painting)
    paint = one(plan, "acme.paint")
    colors: dict[str, str] = {one(plan, "acmefx.palette").id: digest(canonical_json({"hue": 30}))}
    found = plugin_material(plan, paint, registry, Digests({"acme.fx": "sha256:" + "1" * 64}))
    assert found == {"acme.fx": "sha256:" + "1" * 64, "project": "sha256:" + "0" * 64}
    first = key(plan, paint, registry, inputs=colors, digests=Digests({"acme.fx": "sha256:" + "1" * 64}))
    assert key(plan, paint, registry, inputs=colors,
               digests=Digests({"acme.fx": "sha256:" + "4" * 64})).key != first.key
    assert key(plan, paint, registry, inputs=colors, digests=Digests({"acme.fx": "sha256:" + "1" * 64},
                                                                     project="sha256:" + "5" * 64)).key != first.key
    assert key_of(plan, paint, registry, inputs=colors,
                  digests=Digests({"acme.fx": "sha256:" + "1" * 64}, project=None)) == (
        Bypass("project code without a tree"))
    assert Palette().hue == 0 and Paint().palette is None


def test_a_plugin_executor_keys_by_its_own_plugin_digest(registry: Registry) -> None:
    plan, render = gfx_render(registry)
    runner = executor(provenance=Provenance("acme.render", "1.0"))
    first = key(plan, render, registry, spec=runner, digests=Digests({"acme.render": "sha256:" + "1" * 64}))
    assert first.material["plugins"] == {"acme.render": "sha256:" + "1" * 64}
    material: Any = first.material["executor"]
    assert material["harness"] is None
    assert key(plan, render, registry, spec=runner,
               digests=Digests({"acme.render": "sha256:" + "2" * 64})).key != first.key
    assert key_of(plan, render, registry, spec=runner, digests=Digests({})) == (
        Bypass("plugin acme.render is not loaded on this hub"))
    local = executor(provenance=Provenance("project", ""))
    from_tree = key(plan, render, registry, spec=local, digests=Digests(project="sha256:" + "3" * 64))
    assert from_tree.material["plugins"] == {"project": "sha256:" + "3" * 64}
    assert key(plan, render, registry, spec=local, digests=Digests(project="sha256:" + "4" * 64)).key != (
        from_tree.key)
    assert key_of(plan, render, registry, spec=local, digests=Digests(project=None)) == (
        Bypass("project code without a tree"))


def test_built_in_only_nodes_have_no_plugin_material(registry: Registry) -> None:
    plan, render = gfx_render(registry)
    assert plugin_material(plan, render, registry, Digests()) == {}
    assert key(plan, render, registry, digests=Digests(project=None)).material["plugins"] == {}


# BL-008: renders with ML effects


def test_an_ml_render_requires_and_keys_the_inference_record(registry: Registry) -> None:
    plan = sparkle_plan(fx.punch_in())
    render = one(plan, "edit.render")
    assert machine_dependent(render, registry)
    required = required_resolution(render, registry)
    assert required == ("inference.device", "inference.model", "inference.weights", "inference.backend_version")
    record: dict[str, JsonValue] = {"inference": {"device": "mps", "model": "blazeface", "weights": "sha256:1",
                                                  "backend_version": "mediapipe 0.10"}}
    spec = executor(resolver=resolver_of(record))
    found = key(plan, render, registry, spec=spec, resolution=record)
    assert found.material["resolution"] == record
    cuda: dict[str, JsonValue] = {"inference": {"device": "cuda", "model": "blazeface", "weights": "sha256:1",
                                                "backend_version": "mediapipe 0.10"}}
    assert key(plan, render, registry, spec=spec, resolution=cuda).key != found.key
    assert key_of(plan, render, registry, resolution=record) == Bypass(
        "edit.render is machine-dependent and its executor has no resolver")


# BL-009: Blender


def test_a_render_with_an_effect_missing_from_the_registry_is_never_keyed(registry: Registry) -> None:
    state = registry.snapshot()
    with contributing("acme.fx", "1.0"):
        @eks.effect(description="Neural glow", device_dependent=True)
        class NeuralGlow(EffectBase, frozen=True):
            kind: Literal["neural_glow"] = "neural_glow"

    plan = sparkle_plan(fx.custom("neural_glow"))
    render = one(plan, "edit.render")
    record: dict[str, JsonValue] = {"inference": {"device": "mps", "model": "glow", "weights": "sha256:" + "6" * 64,
                                                  "backend_version": "torch 2.5"}}
    assert isinstance(key_of(plan, render, registry, spec=executor(resolver=resolver_of(record)), resolution=record,
                             digests=Digests({"acme.fx": "sha256:" + "1" * 64})), CacheKey)
    registry.restore(state)
    assert not machine_dependent(render, registry)
    for spec in (executor(), executor(resolver=resolver_of(record))):
        found = key_of(plan, render, registry, spec=spec, resolution=record,
                       digests=Digests({"acme.fx": "sha256:" + "1" * 64}))
        assert isinstance(found, Bypass) and "do not validate" in found.reason, found
    assert NeuralGlow().kind == "neural_glow"


def blender_plan(device: Literal["CPU", "GPU"], platform: str | None = None) -> Plan:
    @eks.scene(name=f"cube_{device}_{platform or 'any'}".replace("-", "_"), kind="blender", engine="cycles",
               samples=8, fps=30, duration=t("2s"), device=device, platform=platform)
    def cube(sc: eks.BlenderScene) -> None:
        sc.camera(lens=50)

    return eks.plan(cube)


def test_blender_runtime_versions_enter_the_key(registry: Registry) -> None:
    pinned = blender_plan("CPU", "linux-x86_64")
    render = one(pinned, "blender.render")
    assert required_resolution(render, registry) == ()
    first = key(pinned, render, registry, spec=executor(runtime=lambda: "blender 4.2.1 cycles 4.2 oidn 2.3"))
    second = key(pinned, render, registry, spec=executor(runtime=lambda: "blender 4.2.2 cycles 4.2 oidn 2.3"))
    assert first.key != second.key
    unpinned = blender_plan("CPU")
    loose = one(unpinned, "blender.render")
    assert required_resolution(loose, registry) == ("platform",)
    record: dict[str, JsonValue] = {"platform": "darwin-arm64"}
    spec = executor(resolver=resolver_of(record), runtime=lambda: "blender 4.2.1")
    mac = key(unpinned, loose, registry, spec=spec, resolution=record)
    linux = key(unpinned, loose, registry, spec=spec, resolution={"platform": "linux-x86_64"})
    assert mac.key != linux.key
    gpu = blender_plan("GPU")
    assert required_resolution(one(gpu, "blender.render"), registry) == ("gpu.adapter", "gpu.driver")


# BL-010: external tempo


def tempo_plan(bpm: float | None) -> Plan:
    @eks.scene(name=f"pulse_{bpm or 'external'}".replace(".", "_"), kind="gfx", size=(1080, 1920),
               duration=t("4s"), fps=30, cpu=True, bpm=bpm)
    def pulse(sc: eks.GfxScene) -> None:
        sc.rect(width=100.0, height=100.0, fill="#ffffff")

    return eks.plan(pulse)


def run_clock(bpm: float) -> RunClock:
    return RunClock(tempo=TempoMap(bpm=bpm), cues={"drop": t("2s")}, markers={"hits": [t("1s")]})


def test_clock_material_follows_external_tempo(registry: Registry) -> None:
    own = tempo_plan(120.0)
    external = tempo_plan(None)
    assert own.clock.tempo is not None and not own.clock.tempo.external
    assert external.clock.tempo is not None and external.clock.tempo.external
    assert clock_material(own, run_clock(120.0)) is None
    assert clock_material(external, None) is None
    material: Any = clock_material(external, run_clock(128.0))
    assert material["tempo"]["bpm"] == 128.0
    assert set(material) == {"tempo", "cues", "markers"}
    assert list(material["cues"]) == ["drop"]
    assert list(material["markers"]) == ["hits"]
    render = one(external, "gfx.render")
    first = key(external, render, registry, clock=run_clock(128.0))
    assert first.material["clock"] == material
    assert key(external, render, registry, clock=run_clock(100.0)).key != first.key
    assert key(external, render, registry, clock=run_clock(128.0)).key == first.key


def test_hash_plan_is_untouched_by_cache_keys(registry: Registry) -> None:
    plan, render = gfx_render(registry)
    copy = hash_plan(Plan.model_validate(plan.model_dump(mode="json", by_alias=True)))
    key(plan, render, registry)
    assert copy.hash == plan.hash


def test_a_resolver_reads_a_relative_tree_file(registry: Registry, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "edits" / "assets").mkdir(parents=True)
    (tree / "edits" / "assets" / "clip.mp4").write_bytes(b"tree bytes")
    store = FileStore(tmp_path / "store")
    content = ContentResolver(store, tree=tree, base="edits", plugin_roots={}, scratch=tmp_path / "scratch")
    found = content.media("file:assets/clip.mp4")
    assert found.source == "file"
    assert found.path == tree / "edits" / "assets" / "clip.mp4"
    shutil.rmtree(tree)
    with pytest.raises(ContentError):
        content.media("file:assets/clip.mp4")
