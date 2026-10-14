from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_core import to_jsonable_python

from eks_harness.ir.catalog.markers import MarkerBase, STTMarkers
from eks_harness.ir.contributions import (
    CHECK_MEDIA_OP,
    NO_SESSION,
    PERSONA_CATEGORY,
    SESSION_PRESETS,
    Contribution,
    JsonDecoder,
    SessionScope,
    contributions,
    node_args,
    node_contributions,
    resource_contributions,
    session_scope,
    walk_scoped,
)
from eks_harness.ir.ops.blender import BlenderScene, blender_set_values
from eks_harness.ir.ops.edit import SOFTWARE_ENCODERS
from eks_harness.ir.ops.gfx import GfxScene, gfx_set_values
from eks_harness.ir.plan import Binding, Clock, Node, Plan, PlanError, Resource
from eks_harness.ir.refs import (
    ITEM_KEY,
    REF_KEY,
    ItemExpr,
    PathStep,
    RefError,
    RefExpr,
    map_exprs,
    walk_exprs,
)
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    Pinnable,
    Registry,
    RegistryError,
    default_registry,
    using_registry,
)
from eks_harness.ir.time import (
    Span,
    TempoMap,
    Time,
    TimeError,
    TimeNeeds,
    TimeSet,
    field_needs,
    format_fps,
    format_time,
    parse_time,
)
from eks_harness.ir.types import PINNED_ARTIFACT_PATTERN

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
NODE_ID_LENGTH = 10
RESOURCE_ID_LENGTH = 7
PLAN_HASH_EXCLUDED = frozenset({"name", "source", "tags", "hash"})
NODE_HASH_EXCLUDED = frozenset({"id", "src", "label", "hash"})


EACH_OP = "ctl.each"
MARKERS_OP = "media.markers"
SOURCE_OP = "media.source"
PINNED_ARTIFACT_URI = re.compile(PINNED_ARTIFACT_PATTERN)
EDIT_RENDER_OP = "edit.render"
GFX_RENDER_OP = "gfx.render"
SCENE_RENDER_OP = "scene.render"
BLENDER_RENDER_OP = "blender.render"
AUDIO_MIX_OP = "audio.mix"
CPU_DEVICE = "CPU"
CPU_ENGINE = "CYCLES"
TIME_BINDING = "time"
WINDOW_NEEDS = TimeNeeds(window=True)
_DUMP: dict[str, Any] = {"mode": "json", "by_alias": True, "exclude_none": True}


def canonical_json(value: Any) -> bytes:
    data = value.model_dump(**_DUMP) if isinstance(value, BaseModel) else value
    _check_json(data, "$")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _check_json(value: Any, where: str) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: {value!r} is not a finite number and has no canonical JSON form")
        return
    if isinstance(value, list):
        for index, item in enumerate(cast("list[Any]", value)):
            _check_json(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in cast("dict[Any, Any]", value).items():
            if not isinstance(key, str):
                raise TypeError(f"{where}: object key {key!r} is a {type(key).__name__}; canonical JSON keys are strings")
            _check_json(item, f"{where}.{key}")
        return
    raise TypeError(
        f"{where}: {type(value).__name__} {value!r} is not JSON; dump models with mode='json' and use lists, not tuples"
    )


def canonical_dump(model: BaseModel) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", _sorted_sets(model, model.model_dump(**_DUMP)))


def canonical_items(values: Collection[Any]) -> list[Any]:
    if not isinstance(values, set | frozenset):
        return list(values)
    return sorted(values, key=_canonical_key)


def _canonical_key(item: Any) -> bytes:
    return canonical_json(_sorted_sets(item, to_jsonable_python(item, by_alias=True, exclude_none=True)))


def _sorted_sets(value: Any, data: Any) -> Any:
    if isinstance(value, BaseModel) and isinstance(data, dict):
        result = dict(cast("dict[str, Any]", data))
        for name, info in type(value).model_fields.items():
            key = info.serialization_alias or info.alias or name
            if key in result:
                result[key] = _sorted_sets(getattr(value, name), result[key])
        return result
    if isinstance(value, set | frozenset | list | tuple) and isinstance(data, list):
        items = cast("Collection[Any]", value)
        dumped = cast("list[Any]", data)
        if len(items) != len(dumped):
            return data
        paired = [_sorted_sets(item, entry) for item, entry in zip(items, dumped, strict=True)]
        return sorted(paired, key=canonical_json) if isinstance(value, set | frozenset) else paired
    if isinstance(value, Mapping) and isinstance(data, dict):
        source = cast("Mapping[Any, Any]", value)
        return {key: _sorted_sets(source[key], item) if key in source else item
                for key, item in cast("dict[str, Any]", data).items()}
    return data


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def b32(data: bytes, length: int) -> str:
    bits = 5 * length
    if length < 0 or bits > 8 * len(data):
        raise ValueError(f"cannot take {length} base32 characters from {len(data)} bytes")
    number = int.from_bytes(data, "big") >> (8 * len(data) - bits)
    return "".join(CROCKFORD[(number >> (5 * (length - 1 - index))) & 31] for index in range(length))


def derive_id(prefix: str, key: str, length: int = NODE_ID_LENGTH) -> str:
    return prefix + b32(hashlib.sha256(key.encode("utf-8")).digest(), length)


def resource_id(kind: str, identity: Mapping[str, Any]) -> str:
    if "kind" in identity:
        raise ValueError(f"resource identity of {kind!r} cannot contain the key 'kind': {dict(identity)!r}")
    data = {"kind": kind, **{key: value for key, value in identity.items() if value is not None}}
    return derive_id("r", canonical_json(data).decode("utf-8"), RESOURCE_ID_LENGTH)


def resource_hash(resource: Resource, registry: Registry | None = None) -> str:
    chosen = registry if registry is not None else default_registry()
    payload: dict[str, Any] = resource.model_dump(**_DUMP, exclude={"id"})
    kinds = _kinds_entries(resource_contributions(resource, chosen))
    if kinds is not None:
        payload["contributions"] = kinds
    return digest(canonical_json(payload))


def node_hash(
    node: Node,
    *,
    refs: Mapping[str, str],
    resources: Mapping[str, str],
    items: Sequence[str] = (),
    executor_version: str = "",
    clock: JsonValue = None,
) -> str:
    def replace(expr: RefExpr | ItemExpr) -> JsonValue:
        path = [_dump_step(step) for step in expr.path]
        if isinstance(expr, RefExpr):
            return {REF_KEY: _lookup(refs, expr.ref, node, "references node"), "path": path}
        if expr.item not in items:
            raise PlanError(f"node {node.id} ({node.op}) uses $item {expr.item!r} outside that eks.each container")
        depth = len(items) - 1 - list(items).index(expr.item)
        return {ITEM_KEY: depth, "path": path}

    branches: dict[str, list[str]] | None = None
    if node.branches is not None:
        branches = {
            name: [child.hash or _lookup(refs, child.id, node, "contains unhashed child") for child in children]
            for name, children in node.branches.items()
        }
    payload = {
        "op": node.op,
        "v": node.v,
        "x": executor_version,
        "args": map_exprs(dict(node.args), replace),
        "uses": [_lookup(resources, resource, node, "uses resource") for resource in node.uses],
        "after": sorted(_lookup(refs, after, node, "runs after node") for after in node.after),
        "out": node.out,
        "pin": format_time(node.pin) if node.pin is not None else None,
        "branches": branches,
        "clock": clock,
    }
    return digest(canonical_json(payload))


def _dump_step(step: PathStep) -> JsonValue:
    if isinstance(step, str | int):
        return step
    return step.model_dump(mode="json", by_alias=True)


def _lookup(hashes: Mapping[str, str], key: str, node: Node, relation: str) -> str:
    value = hashes.get(key)
    if not value:
        raise PlanError(f"node {node.id} ({node.op}) {relation} {key!r}, which has no hash yet; hash in program order")
    return value


def hash_plan(plan: Plan, *, executor_versions: Mapping[str, str] | None = None) -> Plan:
    hasher = _Hasher(plan, _ExecutorVersions(executor_versions or {}))
    for node, items in hasher.order:
        hasher.hash(node, items)
    plan.hash = plan_hash(plan, hasher.versions.registry)
    return plan


class _ExecutorVersions:
    def __init__(self, explicit: Mapping[str, str]) -> None:
        self._explicit = explicit
        self._registry: Registry | None = None

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = default_registry()
        return self._registry

    def __call__(self, op: str) -> str:
        if op in self._explicit:
            return self._explicit[op]
        return self.registry.op(op).executor_version if self.registry.has_op(op) else ""


_SESSION_CATEGORIES = frozenset({*(category for _field, category in SESSION_PRESETS), PERSONA_CATEGORY})
ALL_NEEDS = TimeNeeds(fps=True, tempo=True, window=True, default_words=True)


def time_needs(value: Any, decoder: JsonDecoder | None = None) -> TimeNeeds:
    if isinstance(value, Time | Span | TimeSet):
        return value.needs()
    needs = TimeNeeds()
    if isinstance(value, Binding):
        needs |= binding_needs(value)
    if isinstance(value, BaseModel):
        for name, info in type(value).model_fields.items():
            item = getattr(value, name)
            needs |= field_needs(info.metadata, item)
            needs |= time_needs(item, decoder)
        checked = decoder.check_params(value) if decoder is not None else None
        if checked is not None:
            needs |= time_needs(checked, decoder)
        for _position, typed in scene_set_values(value):
            needs |= time_needs(typed, decoder)
    elif isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        if decoder is not None:
            for model in decoder.models(mapping):
                needs |= time_needs(model, decoder)
        for item in mapping.values():
            needs |= time_needs(item, decoder)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in cast("Collection[Any]", value):
            needs |= time_needs(item, decoder)
    return needs


def scene_set_values(value: BaseModel) -> list[tuple[int, Any]]:
    if isinstance(value, GfxScene):
        return gfx_set_values(value)
    if isinstance(value, BlenderScene):
        return blender_set_values(value)
    return []


def binding_needs(binding: Binding) -> TimeNeeds:
    if binding.kind != TIME_BINDING or not isinstance(binding.value, str):
        return TimeNeeds()
    try:
        return parse_time(binding.value).needs()
    except (TimeError, ValueError):
        return TimeNeeds()


class _Validated:
    def __init__(self, node: Node, registry: Registry, decoder: JsonDecoder | None = None) -> None:
        self.node = node
        self.registry = registry
        self._decoder = decoder
        self.model = node_args(node, registry)

    @property
    def decoder(self) -> JsonDecoder:
        if self._decoder is None:
            self._decoder = JsonDecoder(self.registry)
        return self._decoder

    def needs(self) -> TimeNeeds | None:
        needs = self.node.pin.needs() if self.node.pin is not None else TimeNeeds()
        if self.registry.has_op(self.node.op) and self.registry.op(self.node.op).bounded:
            needs |= WINDOW_NEEDS
        if not self.node.args:
            return needs
        if self.model is None:
            return None
        return needs | time_needs(self.model, self.decoder)

    def contributions(self, scope: SessionScope) -> list[Contribution] | None:
        if self.model is None:
            return None
        return node_contributions(self.node, self.registry, scope=scope, model=self.model)


def _node_needs(node: Node, registry: Registry, decoder: JsonDecoder | None = None) -> TimeNeeds | None:
    return _Validated(node, registry, decoder).needs()


def node_needs(node: Node, registry: Registry | None = None) -> TimeNeeds | None:
    chosen = registry if registry is not None else default_registry()
    with using_registry(chosen):
        return _node_needs(node, chosen)


def _shown_version(contribution: Contribution) -> str:
    return "" if contribution.plugin == BUILTIN_PLUGIN else contribution.plugin_version


def _kinds_entries(found: Sequence[Contribution] | None) -> JsonValue:
    if found is None:
        return {"kinds": None}
    kinds: list[JsonValue] = [
        [item.category, item.kind, item.version, item.plugin, _shown_version(item)]
        for item in found if not item.preset
    ]
    presets: list[JsonValue] = [
        [item.category, item.kind, item.value, item.plugin, _shown_version(item)]
        for item in found if item.preset
    ]
    if not kinds and not presets:
        return None
    return {"kinds": sorted(kinds, key=canonical_json), "presets": sorted(presets, key=canonical_json)}


def kinds_payload(node: Node, registry: Registry | None = None, *, scope: SessionScope = NO_SESSION) -> JsonValue:
    chosen = registry if registry is not None else default_registry()
    return _kinds_entries(_Validated(node, chosen).contributions(scope))


def _with_kinds(clock: JsonValue, kinds: JsonValue) -> JsonValue:
    if kinds is None:
        return clock
    return {**cast("dict[str, JsonValue]", clock), **cast("dict[str, JsonValue]", kinds)}


class _Providers:
    def __init__(self, nodes: Sequence[Node], registry: Registry) -> None:
        self.streams: dict[str, list[str]] = {}
        self.words: dict[str, list[str]] = {}
        self.all_words: list[str] = []
        self.all_markers: list[str] = []
        for node in nodes:
            if node.op != MARKERS_OP:
                continue
            self.all_markers.append(node.id)
            try:
                model = registry.op(MARKERS_OP).args.model_validate(node.args)
            except (ValidationError, RegistryError):
                continue
            for source in getattr(model, "sources", ()):
                if not isinstance(source, MarkerBase):
                    continue
                names = {*source.stream_names(), *(name.rsplit(".", 1)[-1] for name in source.stream_names())}
                for name in names:
                    self.streams.setdefault(name, []).append(node.id)
                if isinstance(source, STTMarkers):
                    self.words.setdefault(source.name, []).append(node.id)
                    self.all_words.append(node.id)

    def ids(self, clock: Clock, needs: TimeNeeds, *, everything: bool = False, own: str | None = None) -> list[str]:
        found: list[str] = []
        tempo = clock.tempo
        if needs.tempo and tempo is not None and tempo.source is not None:
            found.append(tempo.source)
        for stream in sorted(set(self.streams) if everything else needs.markers):
            found.extend(self.streams.get(stream, ()))
        for source in sorted(set(self.words) if everything else needs.words):
            found.extend(self.words.get(source, ()))
        if needs.default_words or everything:
            found.extend(self.all_words)
        return [node_id for node_id in dict.fromkeys(found) if node_id != own]


class _Hasher:
    def __init__(self, plan: Plan, versions: _ExecutorVersions) -> None:
        self.plan = plan
        self.versions = versions
        self.resources = {resource.id: resource_hash(resource, versions.registry) for resource in plan.resources}
        self.order: list[tuple[Node, tuple[str, ...]]] = []
        self.index: dict[str, tuple[Node, tuple[str, ...]]] = {}
        self._collect(plan.nodes, ())
        self.scopes: dict[int, SessionScope] = {
            id(node): scope for node, scope in walk_scoped(plan.nodes, session_scope(plan.session))
        }
        self.done: dict[int, str] = {}
        self.hashes: dict[str, str] = {}
        self.active: set[int] = set()
        self.resolving: set[str] = set()
        self._providers: _Providers | None = None
        self._decoder: JsonDecoder | None = None

    @property
    def decoder(self) -> JsonDecoder:
        if self._decoder is None:
            self._decoder = JsonDecoder(self.versions.registry)
        return self._decoder

    def _collect(self, nodes: Sequence[Node], items: tuple[str, ...]) -> None:
        for node in nodes:
            self.order.append((node, items))
            self.index.setdefault(node.id, (node, items))
            if node.branches is not None:
                inner = (*items, node.id) if node.op == EACH_OP else items
                for children in node.branches.values():
                    self._collect(children, inner)

    @property
    def providers(self) -> _Providers:
        if self._providers is None:
            self._providers = _Providers([node for node, _items in self.order], self.versions.registry)
        return self._providers

    def by_id(self, node_id: str) -> str | None:
        found = self.index.get(node_id)
        if found is None:
            return None
        node, items = found
        if id(node) not in self.done and node_id not in self.resolving:
            self.resolving.add(node_id)
            for dependency in _inputs_of(node):
                if dependency not in self.hashes:
                    self.by_id(dependency)
            self.resolving.discard(node_id)
        return self.hash(node, items)

    def hash(self, node: Node, items: tuple[str, ...]) -> str:
        key = id(node)
        known = self.done.get(key)
        if known is not None:
            return known
        if key in self.active:
            raise PlanError(f"node {node.id} ({node.op}) depends on its own hash through the clock (a tempo or marker "
                            "source that needs this node); break the cycle")
        self.active.add(key)
        if node.branches is not None:
            inner = (*items, node.id) if node.op == EACH_OP else items
            for children in node.branches.values():
                for child in children:
                    self.hash(child, inner)
        clock = self.clock_payload(node)
        value = node_hash(node, refs=self.hashes, resources=self.resources, items=items,
                          executor_version=self.versions(node.op), clock=clock)
        node.hash = value
        self.done[key] = value
        self.hashes[node.id] = value
        self.active.discard(key)
        return value

    def clock_payload(self, node: Node) -> JsonValue:
        validated = _Validated(node, self.versions.registry, self.decoder)
        needs = validated.needs()
        clock = clock_payload(self.plan.clock, ALL_NEEDS if needs is None else needs, self.source_hash,
                              self.providers, everything=needs is None, own=node.id)
        return _with_kinds(clock, _kinds_entries(validated.contributions(self.scopes.get(id(node), NO_SESSION))))

    def source_hash(self, node_id: str) -> str:
        found = self.by_id(node_id)
        if found is None:
            raise PlanError(f"the clock refers to node {node_id!r}, which is not in the plan")
        return found


def clock_payload(
    clock: Clock,
    needs: TimeNeeds,
    source_hash: Callable[[str], str],
    providers: _Providers,
    *,
    everything: bool = False,
    own: str | None = None,
) -> JsonValue:
    payload: dict[str, Any] = {"seed": clock.seed}
    if clock.now is not None:
        payload["now"] = clock.now
    needs = clock_needs(clock, needs, everything=everything)
    cue_names = set(needs.cues)
    if cue_names:
        payload["cues"] = {name: format_time(clock.cues[name]) if name in clock.cues else None
                           for name in sorted(cue_names)}
    if needs.fps:
        payload["fps"] = format_fps(clock.fps) if clock.fps is not None else None
    if needs.window:
        payload["window"] = clock.window.model_dump(mode="json") if clock.window is not None else None
        payload["duration"] = format_time(clock.duration) if clock.duration is not None else None
    if needs.tempo:
        payload["tempo"] = tempo_payload(clock.tempo, source_hash)
    streams = set(providers.streams) if everything else set(needs.markers)
    if streams:
        payload["markers"] = {stream: sorted(source_hash(provider) for provider in providers.streams.get(stream, ())
                                             if provider != own)
                              for stream in sorted(streams)}
    sources = set(providers.words) if everything else set(needs.words)
    if sources:
        payload["words"] = {source: sorted(source_hash(provider) for provider in providers.words.get(source, ())
                                           if provider != own)
                            for source in sorted(sources)}
    if needs.default_words and providers.all_words:
        payload["all_words"] = sorted(source_hash(provider) for provider in providers.all_words if provider != own)
    return payload


def clock_needs(clock: Clock, needs: TimeNeeds, *, everything: bool = False) -> TimeNeeds:
    if everything:
        needs |= TimeNeeds(cues=frozenset(clock.cues))
    bounded = False
    while True:
        before = needs
        if (needs.window or everything) and not bounded:
            bounded = True
            needs |= bounds_needs(clock)
        for name in sorted(needs.cues):
            value = clock.cues.get(name)
            if value is not None:
                needs |= value.needs()
        if needs == before:
            return needs


def bounds_needs(clock: Clock) -> TimeNeeds:
    needs = TimeNeeds()
    if clock.window is not None:
        needs |= clock.window.needs()
    if clock.duration is not None:
        needs |= clock.duration.needs()
    return needs


def stored_clock_payload(plan: Plan, node: Node, registry: Registry | None = None) -> JsonValue:
    chosen = registry if registry is not None else default_registry()
    stored: dict[str, str] = {}
    for candidate, _ancestors in plan.walk():
        if candidate.hash:
            stored.setdefault(candidate.id, candidate.hash)

    def source_hash(node_id: str) -> str:
        if node_id not in stored:
            raise PlanError(f"the clock refers to node {node_id!r}, which has no stored hash")
        return stored[node_id]

    validated = _Validated(node, chosen)
    needs = validated.needs()
    providers = _Providers([candidate for candidate, _ancestors in plan.walk()], chosen)
    clock = clock_payload(plan.clock, ALL_NEEDS if needs is None else needs, source_hash, providers,
                          everything=needs is None, own=node.id)
    root = session_scope(plan.session)
    scope = next((found for candidate, found in walk_scoped(plan.nodes, root) if candidate is node), root)
    return _with_kinds(clock, _kinds_entries(validated.contributions(scope)))


def tempo_payload(tempo: TempoMap | None, source_hash: Callable[[str], str]) -> JsonValue:
    if tempo is None:
        return None
    data: dict[str, JsonValue] = tempo.model_dump(**_DUMP)
    if tempo.source is not None:
        data["source"] = source_hash(tempo.source)
    return data


def session_presets(plan: Plan, registry: Registry | None = None) -> list[JsonValue]:
    if plan.session is None:
        return []
    chosen = registry if registry is not None else default_registry()
    found = [item for item in contributions(plan.session, chosen) if item.preset and item.category in _SESSION_CATEGORIES]
    return sorted(([item.category, item.kind, item.value, item.plugin, _shown_version(item)] for item in found),
                  key=canonical_json)


def session_kinds(plan: Plan, registry: Registry | None = None) -> list[JsonValue]:
    if plan.session is None:
        return []
    chosen = registry if registry is not None else default_registry()
    found = [item for item in contributions(plan.session, chosen) if not item.preset]
    return sorted(([item.category, item.kind, item.version, item.plugin, _shown_version(item)] for item in found),
                  key=canonical_json)


def plan_hash(plan: Plan, registry: Registry | None = None) -> str:
    refs: dict[str, str] = {}
    for node, _ancestors in plan.walk():
        if not node.hash:
            raise PlanError(f"node {node.id} ({node.op}) has no hash; call hash_plan(plan) instead of plan_hash")
        refs.setdefault(node.id, node.hash)

    def replace(expr: RefExpr | ItemExpr) -> JsonValue:
        if isinstance(expr, ItemExpr):
            raise PlanError(f"plan outputs use $item {expr.item!r}; outputs may only reference nodes")
        if expr.ref not in refs:
            raise PlanError(f"plan outputs reference unknown node {expr.ref!r}")
        return {REF_KEY: refs[expr.ref], "path": [_dump_step(step) for step in expr.path]}

    def source_hash(node_id: str) -> str:
        if node_id not in refs:
            raise PlanError(f"the clock tempo source {node_id!r} is not a node of the plan")
        return refs[node_id]

    clock: dict[str, JsonValue] = plan.clock.model_dump(**_DUMP)
    if plan.clock.tempo is not None:
        clock["tempo"] = tempo_payload(plan.clock.tempo, source_hash)
    payload = {
        "schema": plan.schema_,
        "kind": plan.kind,
        "params": plan.params,
        "clock": clock,
        "session": plan.session.model_dump(**_DUMP) if plan.session is not None else None,
        "resources": sorted(resource_hash(resource, registry) for resource in plan.resources),
        "nodes": [node.hash for node in plan.nodes],
        "outputs": map_exprs(plan.outputs, replace),
        "personas": plan.personas,
        "requires": [requirement.model_dump(**_DUMP) for requirement in plan.requires],
        "base": plan.base,
        "presets": session_presets(plan, registry),
        "session_kinds": session_kinds(plan, registry),
    }
    return digest(canonical_json(payload))


def content_dependent(node: Node) -> bool:
    if node.op != SOURCE_OP:
        return False
    uri = node.args.get("uri")
    return not (isinstance(uri, str) and PINNED_ARTIFACT_URI.fullmatch(uri) is not None)


def machine_dependent(node: Node, registry: Registry | None = None) -> bool:
    chosen = registry if registry is not None else default_registry()
    if chosen.has_op(node.op) and chosen.op(node.op).device_dependent:
        return True
    if node.op == SCENE_RENDER_OP:
        return True
    if node.op == AUDIO_MIX_OP:
        return _inferred_tracks(node.args)
    if node.op == EDIT_RENDER_OP:
        if _raw(node.args, "timeline", "render", "encoder") not in SOFTWARE_ENCODERS:
            return True
        return _device_effects(node, chosen)
    if node.op == GFX_RENDER_OP:
        return _raw(node.args, "cpu") is not True
    if node.op == BLENDER_RENDER_OP:
        return not _pinned_cpu_render(node.args)
    if node.op == CHECK_MEDIA_OP:
        return _machine_check(node.args, chosen)
    if node.op != MARKERS_OP:
        return False
    model = node_args(node, chosen)
    sources = getattr(model, "sources", None)
    if not isinstance(sources, tuple):
        return True
    return any(not source.pinned() for source in cast("tuple[Any, ...]", sources) if isinstance(source, MarkerBase))


def _device_effects(node: Node, registry: Registry) -> bool:
    if not registry.has_op(EDIT_RENDER_OP):
        return True
    with using_registry(registry):
        try:
            registry.op(EDIT_RENDER_OP).args.model_validate(node.args)
        except ValidationError as exc:
            if not all(_unknown_effect(error["loc"], node.args, registry) for error in exc.errors()):
                return True
    return any(_device_effect(effect, registry) for effect in _raw_effects(node.args))


def _raw_effects(args: Mapping[str, Any]) -> list[object]:
    found: list[object] = []
    tracks = _raw(args, "timeline", "tracks")
    for track in cast("list[object]", tracks) if isinstance(tracks, list) else []:
        clips = _raw(cast("dict[str, Any]", track), "clips") if isinstance(track, dict) else None
        for clip in cast("list[object]", clips) if isinstance(clips, list) else []:
            effects = _raw(cast("dict[str, Any]", clip), "effects") if isinstance(clip, dict) else None
            found.extend(cast("list[object]", effects) if isinstance(effects, list) else [])
    return found


def _effect_kind(effect: object) -> str | None:
    if isinstance(effect, BaseModel):
        kind = getattr(effect, "kind", None)
    elif isinstance(effect, dict):
        kind = cast("dict[str, Any]", effect).get("kind")
    else:
        return None
    return kind if isinstance(kind, str) else None


def _device_effect(effect: object, registry: Registry) -> bool:
    kind = _effect_kind(effect)
    if kind is None:
        return True
    try:
        return registry.catalog("effect", kind).device_dependent
    except RegistryError:
        return False


def _unknown_effect(loc: tuple[int | str, ...], args: Mapping[str, Any], registry: Registry) -> bool:
    if len(loc) != 7 or loc[0] != "timeline" or loc[1] != "tracks" or loc[3] != "clips" or loc[5] != "effects":
        return False
    value: object = args
    for step in loc:
        if isinstance(step, str) and isinstance(value, dict):
            value = cast("dict[str, Any]", value).get(step)
        elif isinstance(step, int) and isinstance(value, list) and 0 <= step < len(cast("list[object]", value)):
            value = cast("list[object]", value)[step]
        else:
            return False
    kind = _effect_kind(value)
    if kind is None:
        return False
    try:
        registry.catalog("effect", kind)
    except RegistryError:
        return True
    return False


def _machine_check(args: Mapping[str, Any], registry: Registry) -> bool:
    kind = args.get("kind")
    params = args.get("params", {})
    if not isinstance(kind, str) or kind not in {spec.kind for spec in registry.checks("media")}:
        return True
    spec = registry.check(kind)
    if spec.device_dependent:
        return True
    if not isinstance(params, dict):
        return True
    try:
        model = spec.args.model_validate(params)
    except ValidationError:
        return True
    return isinstance(model, Pinnable) and not model.pinned()


def _pinned_cpu_render(args: Mapping[str, Any]) -> bool:
    engine = _raw(args, "scene", "render", "engine")
    device = _raw(args, "scene", "render", "device")
    platform = _raw(args, "scene", "render", "platform")
    return engine == CPU_ENGINE and device == CPU_DEVICE and isinstance(platform, str) and bool(platform)


def _inferred_tracks(args: Mapping[str, Any]) -> bool:
    tracks = _raw(args, "mix", "tracks")
    if not isinstance(tracks, list):
        return tracks is not None
    for track in cast("list[object]", tracks):
        if not isinstance(track, dict):
            return True
        entry = cast("dict[str, Any]", track)
        if entry.get("stems") is not None or entry.get("denoise") is not None:
            return True
    return False


def _raw(args: Mapping[str, Any], *names: str) -> object:
    value: object = args
    for name in names:
        if not isinstance(value, dict):
            return None
        value = cast("dict[str, Any]", value).get(name)
    return value


def _pure(node: Node, registry: Registry) -> bool:
    return registry.has_op(node.op) and registry.op(node.op).pure


def _inputs_of(node: Node) -> list[str]:
    try:
        exprs = list(walk_exprs(node.args))
    except RefError:
        exprs = []
    found = [expr.ref for expr in exprs if isinstance(expr, RefExpr)]
    return list(dict.fromkeys([*found, *node.after]))


def clock_providers(clock: Clock, needs: TimeNeeds | None, providers: _Providers, *, own: str | None = None) -> list[str]:
    everything = needs is None
    full = clock_needs(clock, ALL_NEEDS if needs is None else needs, everything=everything)
    return providers.ids(clock, full, everything=everything, own=own)


def clock_dependencies(plan: Plan, registry: Registry | None = None) -> dict[str, tuple[str, ...]]:
    chosen = registry if registry is not None else default_registry()
    decoder = JsonDecoder(chosen)
    nodes = [node for node, _ancestors in plan.walk()]
    providers = _Providers(nodes, chosen)
    found: dict[str, tuple[str, ...]] = {}
    for node in nodes:
        ids = clock_providers(plan.clock, _node_needs(node, chosen, decoder), providers, own=node.id)
        if ids:
            found[node.id] = tuple(ids)
    return found


def link_clock(plan: Plan, registry: Registry | None = None) -> Plan:
    chosen = registry if registry is not None else default_registry()
    for _round in range(len(list(plan.walk())) + 1):
        dependencies = clock_dependencies(plan, chosen)
        if not _hoist_providers(plan, dependencies, chosen):
            break
    dependencies = clock_dependencies(plan, chosen)
    for node, _ancestors in plan.walk():
        for provider in dependencies.get(node.id, ()):
            if provider not in node.after:
                node.after.append(provider)
    return plan


def _hoist_providers(plan: Plan, dependencies: Mapping[str, Sequence[str]], registry: Registry) -> bool:
    order = {node.id: index for index, (node, _ancestors) in enumerate(plan.walk())}
    root_of: dict[str, int] = {}
    for position, root in enumerate(plan.nodes):
        root_of[root.id] = position
        for inner in _nested(root.branches):
            root_of[inner.id] = position
    roots = {node.id: node for node in plan.nodes}
    for node, _ancestors in plan.walk():
        for provider in dependencies.get(node.id, ()):
            if provider not in order or order[provider] < order[node.id]:
                continue
            target = root_of[node.id]
            moved = _hoist_closure(provider, target, roots, root_of, dependencies, registry, node)
            kept = [root for root in plan.nodes if root.id not in moved]
            position = next(index for index, root in enumerate(kept) if root.id == plan.nodes[target].id)
            plan.nodes = [*kept[:position], *(root for root in plan.nodes if root.id in moved), *kept[position:]]
            return True
    return False


def _hoist_closure(provider: str, target: int, roots: Mapping[str, Node], root_of: Mapping[str, int],
                   dependencies: Mapping[str, Sequence[str]], registry: Registry, dependent: Node) -> set[str]:
    moved: set[str] = set()
    pending = [provider]
    while pending:
        current = pending.pop()
        if current in moved or root_of.get(current, -1) < target:
            continue
        node = roots.get(current)
        if node is None:
            raise PlanError(
                f"node {dependent.id} ({dependent.op}) resolves its times against clock provider {provider!r}, "
                f"which comes later inside a container and cannot be moved before it; declare the analysed media "
                "and its markers before the node that needs them")
        if root_of[current] == target:
            raise PlanError(
                f"node {dependent.id} ({dependent.op}) resolves its times against clock provider {provider!r}, "
                "which depends on that node; break the cycle")
        spec = registry.op(node.op) if registry.has_op(node.op) else None
        if spec is None or spec.ordering != "graph" or spec.container:
            raise PlanError(
                f"node {dependent.id} ({dependent.op}) resolves its times against clock provider {provider!r}, "
                f"whose input {node.id} ({node.op}) runs in program order after it; declare the analysed media and "
                "its markers before the node that needs them")
        moved.add(current)
        pending.extend(_inputs_of(node))
        pending.extend(dependencies.get(current, ()))
    return moved


def _nested(branches: Mapping[str, Sequence[Node]] | None) -> list[Node]:
    found: list[Node] = []
    for children in (branches or {}).values():
        for child in children:
            found.append(child)
            found.extend(_nested(child.branches))
    return found


def _data_inputs(node: Node, plan: Plan, registry: Registry, providers: _Providers,
                 decoder: JsonDecoder) -> list[str]:
    try:
        exprs = list(walk_exprs(node.args))
    except RefError:
        return []
    inputs = [expr.ref if isinstance(expr, RefExpr) else expr.item for expr in exprs]
    inputs.extend(clock_providers(plan.clock, _node_needs(node, registry, decoder), providers, own=node.id))
    return list(dict.fromkeys(inputs))


def _closure_walk(plan: Plan, node: Node, registry: Registry) -> tuple[list[str], list[str]]:
    order = {candidate.id: index for index, (candidate, _ancestors) in enumerate(plan.walk())}
    by_id = {candidate.id: candidate for candidate, _ancestors in plan.walk()}
    with using_registry(registry):
        providers = _Providers(list(by_id.values()), registry)
        decoder = JsonDecoder(registry)
        found: dict[str, None] = {}
        seen: dict[str, None] = {}
        pending = [node]
        while pending:
            current = pending.pop()
            if current.id in seen:
                continue
            seen[current.id] = None
            if content_dependent(current) or machine_dependent(current, registry):
                found[current.id] = None
            for producer_id in _data_inputs(current, plan, registry, providers, decoder):
                producer = by_id.get(producer_id)
                if producer is None or producer.id == current.id:
                    continue
                if _pure(producer, registry):
                    pending.append(producer)
                else:
                    found[producer.id] = None

    def program(node_id: str) -> int:
        return order.get(node_id, len(order))

    return sorted(seen, key=program), sorted(found, key=program)


def pure_closure(plan: Plan, node: Node, registry: Registry | None = None) -> tuple[str, ...]:
    chosen = registry if registry is not None else default_registry()
    closure, _inputs = _closure_walk(plan, node, chosen)
    return tuple(closure)


def cache_inputs(plan: Plan, node: Node, registry: Registry | None = None) -> tuple[str, ...] | None:
    chosen = registry if registry is not None else default_registry()
    if not _pure(node, chosen):
        return None
    _closure, inputs = _closure_walk(plan, node, chosen)
    return tuple(inputs)


def cacheable(plan: Plan, node: Node, registry: Registry | None = None) -> bool:
    return cache_inputs(plan, node, registry) == ()


def derived_seed(root: int, path: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{root}:{path}".encode()).digest()[:8], "big") & (2**63 - 1)


def node_seed(root: int, node: Node, field: str = "") -> int:
    if not node.hash:
        raise PlanError(f"node {node.id} ({node.op}) has no hash; seeds derive from the node hash, call hash_plan first")
    return derived_seed(root, f"{node.hash}/{field}" if field else node.hash)


__all__ = [
    "CROCKFORD",
    "EACH_OP",
    "NODE_HASH_EXCLUDED",
    "NODE_ID_LENGTH",
    "PINNED_ARTIFACT_URI",
    "PLAN_HASH_EXCLUDED",
    "RESOURCE_ID_LENGTH",
    "b32",
    "binding_needs",
    "bounds_needs",
    "cache_inputs",
    "cacheable",
    "canonical_dump",
    "canonical_items",
    "canonical_json",
    "clock_dependencies",
    "clock_needs",
    "clock_payload",
    "clock_providers",
    "content_dependent",
    "derive_id",
    "derived_seed",
    "digest",
    "hash_plan",
    "kinds_payload",
    "link_clock",
    "machine_dependent",
    "node_hash",
    "node_needs",
    "node_seed",
    "plan_hash",
    "pure_closure",
    "resource_hash",
    "resource_id",
    "scene_set_values",
    "session_kinds",
    "session_presets",
    "stored_clock_payload",
    "tempo_payload",
    "time_needs",
]
