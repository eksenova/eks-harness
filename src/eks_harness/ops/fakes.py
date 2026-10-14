from __future__ import annotations

import hashlib
import json
import mimetypes
import struct
import threading
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, JsonValue, ValidationError

from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverPort,
    DriverReply,
    FileRole,
    SnapshotKind,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import (
    NATIVE_OPS,
    BodyOutcome,
    ContainerEntry,
    ExecContext,
    ExecResult,
    ExecutorSpec,
    ResolveContext,
    pattern_matches,
)
from eks_harness.engine.model import (
    BackendIdentity,
    CheckOutcome,
    CheckpointOutcome,
    FakeAnswer,
    FakeProfile,
    LeaseHandle,
    RunInfo,
    SeedRestoreResult,
)
from eks_harness.engine.resources import (
    CASSETTE_OP,
    CassetteHook,
    CassetteRequest,
    SeedRestoreRequest,
    cassette_key,
)
from eks_harness.ir.catalog.markers import MarkerBase
from eks_harness.ir.hashing import canonical_json, derive_id, digest, machine_dependent
from eks_harness.ir.ops.data import RESOURCE_TYPE_PREFIX
from eks_harness.ir.plan import CheckUse, Node, Plan, SessionSpec
from eks_harness.ir.registry import OpSpec, Registry, using_registry
from eks_harness.ir.time import TICKS_PER_SECOND, Time, format_time
from eks_harness.ir.types import MediaInfo, MediaKind

FAKE_EXECUTOR = "eks.fake"
FAKE_VERSION = "1"
FAKE_DRIVER = "fake"
FAKE_PLATFORMS: frozenset[str] = frozenset({"web", "ios", "android"})
FAKE_URL = "https://app.fake.local/"
SHOT_SIZE = (64, 48)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MACHINE_OPS = frozenset({
    "media.markers", "check.media", "edit.render", "gfx.render", "blender.render", "scene.render", "audio.mix",
})
_AUDIO_OPS = frozenset({"audio.mix", "media.tts", "media.stems"})
_MEDIA_KINDS: frozenset[str] = frozenset(get_args(MediaKind))
_MARKER_POINTS = 16
_MARKER_STEP_TICKS = TICKS_PER_SECOND // 2
_FAKE_PORT_VERSION = "fake-1"
_LIST_PREFIX = "list["
_SHOT_KIND = "screenshot"


@dataclass(frozen=True, slots=True)
class _FakeFile:
    data: bytes
    filename: str
    kind: str


def covers(patterns: Sequence[str], op: OpSpec) -> bool:
    return any(pattern_matches(pattern, op) for pattern in patterns)


def answer_for(profile: FakeProfile, node: Node, attempt: int) -> FakeAnswer | None:
    answer = profile.answers.get(node.id)
    if answer is None:
        answer = profile.answers.get(node.op)
    if answer is None:
        return None
    if answer.times is not None and attempt > answer.times:
        return None
    return answer


def png_bytes(text: str, size: tuple[int, int] = SHOT_SIZE) -> bytes:
    width, height = size
    if width < 1 or height < 1:
        raise ValueError(f"a PNG needs a positive size, got {width}x{height}")
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    base = seed[0:3]
    band = seed[3:6]
    stripe = max(1, height // 4)
    rows = bytearray()
    for y in range(height):
        colour = band if (y // stripe) % 2 else base
        rows.append(0)
        rows.extend(colour * width)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join((_PNG_SIGNATURE, _chunk(b"IHDR", header), _chunk(b"IDAT", zlib.compress(bytes(rows), 9)),
                     _chunk(b"IEND", b"")))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def default_value(op: OpSpec, node: Node, args: BaseModel, data: Mapping[str, JsonValue]) -> JsonValue:
    return _default_value(op, node, args, data, None)


def default_resolution(op: OpSpec, node: Node, registry: Registry) -> dict[str, JsonValue] | None:
    if not machine_dependent(node, registry):
        return None
    markers: dict[str, JsonValue] = {
        name: {
            "backend": "fake-backend",
            "version": "fake-1",
            "model": "fake-model",
            "device": "fake-device",
            "compute_type": "fake-compute",
        }
        for name in _source_names(node)
    }
    return {
        "markers": markers,
        "engine": "fake-engine",
        "version": "fake-1",
        "encoder": "fake-encoder",
        "encoder_version": "fake-1",
        "inference": {
            "device": "fake-device",
            "model": "fake-model",
            "weights": "fake-weights",
            "backend_version": "fake-1",
        },
        "gpu": {"adapter": "fake-gpu", "driver": "fake-1"},
        "platform": "fake-platform",
        "browser": "fake-browser",
        "device": "fake-device",
        "model": "fake-model",
        "weights": "fake-weights",
        "backend_version": "fake-1",
    }


def _source_names(node: Node) -> list[str]:
    sources = node.args.get("sources")
    if not isinstance(sources, list):
        return []
    names: list[str] = []
    for source in sources:
        if isinstance(source, dict):
            name = source.get("name")
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


def _node_key(node: Node) -> str:
    if node.hash:
        return node.hash
    return digest(canonical_json(node.model_dump(mode="json", by_alias=True)))


def _short(node: Node) -> str:
    return _node_key(node)[7:15]


def _default_value(op: OpSpec, node: Node, args: BaseModel, data: Mapping[str, JsonValue],
                   session: SessionSpec | None) -> JsonValue:
    if op.name == "media.source":
        return _source_value(data)
    out = node.out
    if out in ("artifact", "media"):
        raise ValueError(f"{op.name} node {node.id} outputs {out}: the fake value comes from the file that "
                         "FakeExecutors.run or FakeDriverPort.call writes, not from default_value")
    if out == "none":
        return None
    if out == "bool":
        return True
    if out == "int":
        return 1
    if out == "float":
        return 1.0
    if out == "str":
        if op.name in ("ui.url", "ui.wait_url"):
            return FAKE_URL
        return f"fake {op.name} {_short(node)}"
    if out.startswith(_LIST_PREFIX):
        return []
    if out.startswith(RESOURCE_TYPE_PREFIX):
        return _resource_value(node, data, out.removeprefix(RESOURCE_TYPE_PREFIX))
    return _json_value(op, node, args, data, session)


def _json_value(op: OpSpec, node: Node, args: BaseModel, data: Mapping[str, JsonValue],
                session: SessionSpec | None) -> JsonValue:
    name = op.name
    if name == "ui.route":
        return {"name": "fake", "params": {}}
    if name in ("web.logs", "mobile.route_names"):
        return []
    if name == "expect.target":
        assertion = data.get("assertion")
        kind = cast(dict[str, JsonValue], assertion).get("kind") if isinstance(assertion, dict) else None
        return {"ok": True, "assertion": kind}
    if name == "check.checkpoint":
        return _checkpoint(data, session).model_dump(mode="json")
    if name == "check.explore":
        return {"ok": True, "screens": 0, "findings": []}
    if name == "media.markers":
        return _markers(args)
    if name == "check.media":
        return {"ok": True, "kind": data.get("kind")}
    if name in ("data.create", "data.base"):
        return _resource_value(node, data, None)
    return {}


def _source_value(data: Mapping[str, JsonValue]) -> JsonValue:
    uri = data.get("uri")
    if not isinstance(uri, str):
        raise NodeFailure("args_invalid", "media.source needs a uri")
    kind = data.get("kind")
    if isinstance(kind, str) and kind in _MEDIA_KINDS:
        return {"uri": uri, "kind": kind}
    return {"uri": uri, "kind": _guess_kind(uri)}


def _guess_kind(uri: str) -> str:
    target = urlparse(uri).path if uri.startswith(("http://", "https://")) else uri.partition(":")[2]
    mime, _encoding = mimetypes.guess_type(target)
    if mime is not None:
        major = mime.split("/", 1)[0]
        if major in ("video", "audio", "image"):
            return major
    return "file"


def _resource_value(node: Node, data: Mapping[str, JsonValue], kind: str | None) -> JsonValue:
    path = data.get("path")
    resource_path = path if isinstance(path, str) and path else node.id
    found_kind = data.get("kind")
    return {
        "id": derive_id("f", resource_path),
        "path": resource_path,
        "kind": found_kind if isinstance(found_kind, str) else kind,
        "key": data.get("key"),
    }


def _checkpoint(data: Mapping[str, JsonValue], session: SessionSpec | None) -> CheckpointOutcome:
    raw_checks = data.get("checks")
    if isinstance(raw_checks, list):
        uses = [CheckUse.model_validate(item) for item in raw_checks]
    elif session is not None:
        uses = [*session.defaults, *session.checks]
    else:
        uses = []
    label = data.get("label")
    viewport = data.get("viewport")
    return CheckpointOutcome(
        label=label if isinstance(label, str) else "",
        viewport=viewport if isinstance(viewport, str) else (session.viewport if session is not None else None),
        ok=True,
        checks=[CheckOutcome(kind=use.kind, ok=True, message="fake check passed") for use in uses],
        shots=[],
    )


def _wants_shot(data: Mapping[str, JsonValue]) -> bool:
    return data.get("shot", True) is not False


def _markers(args: BaseModel) -> JsonValue:
    grid: list[JsonValue] = [format_time(Time(ticks=index * _MARKER_STEP_TICKS)) for index in range(_MARKER_POINTS)]
    sources = cast(tuple[object, ...], getattr(args, "sources", ()))
    found: dict[str, JsonValue] = {}
    for source in sources:
        if not isinstance(source, MarkerBase):
            continue
        streams: dict[str, JsonValue] = {}
        for stream in source.stream_names():
            short = stream.removeprefix(f"{source.name}.") if stream != source.name else stream
            streams[short] = list(grid)
        found[source.name] = streams
    return found


def _artifact_file(op: OpSpec, node: Node, data: Mapping[str, JsonValue]) -> _FakeFile:
    key = _node_key(node)
    short = key[7:15]
    name = op.name
    if name in ("capture.shot", "overlay.annotate"):
        label = data.get("name")
        stem = label if isinstance(label, str) and label else f"{name.replace('.', '-')}-{short}"
        return _FakeFile(png_bytes(f"{name}:{key}"), f"{stem}.png", _SHOT_KIND)
    if name == "capture.dump":
        kind = data.get("kind")
        dump_kind = kind if isinstance(kind, str) else "dom"
        label = data.get("name")
        stem = label if isinstance(label, str) and label else f"{dump_kind}-{short}"
        text = f"fake {dump_kind} snapshot of node {node.id} {key}\n"
        return _FakeFile(text.encode("utf-8"), f"{stem}.txt", dump_kind)
    if name == "capture.har":
        label = data.get("name")
        stem = label if isinstance(label, str) and label else f"har-{short}"
        har = {"log": {"version": "1.2", "creator": {"name": FAKE_EXECUTOR, "version": FAKE_VERSION},
                       "comment": key, "entries": []}}
        return _FakeFile(_json_bytes(har), f"{stem}.har", "har")
    if name == "capture.record":
        label = data.get("name")
        stem = label if isinstance(label, str) and label else f"recording-{short}"
        body = {"fake": FAKE_EXECUTOR, "op": name, "node": key, "kind": "video"}
        return _FakeFile(_json_bytes(body), f"{stem}.json", "video")
    body = {"fake": FAKE_EXECUTOR, "op": name, "node": key}
    return _FakeFile(_json_bytes(body), f"{name.replace('.', '-')}-{short}.json", "file")


def _media_fields(op: OpSpec, data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    raw_kind = data.get("kind")
    if op.name in _AUDIO_OPS:
        kind = "audio"
    elif isinstance(raw_kind, str) and raw_kind in _MEDIA_KINDS:
        kind = raw_kind
    else:
        kind = "video"
    fields: dict[str, JsonValue] = {"kind": kind}
    for name, value in _dimensions(data).items():
        try:
            MediaInfo.model_validate({"uri": "artifact:probe", "kind": kind, name: value})
        except ValidationError:
            continue
        fields[name] = value
    return fields


def _dimensions(data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {}
    level: list[Mapping[str, JsonValue]] = [data]
    while level:
        following: list[Mapping[str, JsonValue]] = []
        for mapping in level:
            size = mapping.get("size")
            if isinstance(size, list) and len(size) == 2:
                found.setdefault("width", size[0])
                found.setdefault("height", size[1])
            for name in ("width", "height", "fps", "duration"):
                if name in mapping and mapping[name] is not None:
                    found.setdefault(name, mapping[name])
            following.extend(cast(dict[str, JsonValue], value) for value in mapping.values() if isinstance(value, dict))
        level = following
    return found


def _media_file(op: OpSpec, node: Node, fields: Mapping[str, JsonValue]) -> _FakeFile:
    key = _node_key(node)
    kind = fields.get("kind")
    media_kind = kind if isinstance(kind, str) else "video"
    body: dict[str, JsonValue] = {"fake": FAKE_EXECUTOR, "op": op.name, "node": key, **dict(fields)}
    return _FakeFile(_json_bytes(body), f"{op.name.replace('.', '-')}-{key[7:15]}.json", media_kind)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _answered(answer: FakeAnswer | None) -> bool:
    return answer is not None and "value" in answer.model_fields_set


def _resolver_op(op: OpSpec) -> bool:
    return op.name in _MACHINE_OPS or op.device_dependent


class FakeExecutors:
    def __init__(self, profile: FakeProfile, registry: Registry, *,
                 cassette_hook: CassetteHook | None = None) -> None:
        self.profile = profile
        self.registry = registry
        self.cassette_hook: CassetteHook | None = (
            cassette_hook if cassette_hook is not None else (FakeCassetteHook() if profile.cassettes else None))

    def covered(self, op: OpSpec, *, real: bool) -> bool:
        return covers(self.profile.force, op) or (not real and covers(self.profile.families, op))

    def executor(self, op: OpSpec) -> ExecutorSpec | None:
        if op.name in NATIVE_OPS:
            return None
        resolver = self.resolve if _resolver_op(op) else None
        description = f"Fake executor answering {op.name} with deterministic values"
        if op.container:
            return ExecutorSpec(FAKE_EXECUTOR, FAKE_VERSION, ops=(op.name,), container=self, resolver=resolver,
                                fake=True, locations=None, description=description)
        return ExecutorSpec(FAKE_EXECUTOR, FAKE_VERSION, ops=(op.name,), run=self.run, resolver=resolver, fake=True,
                            locations=None, description=description)

    def run(self, call: ExecContext) -> ExecResult:
        answer = answer_for(self.profile, call.node, call.attempt)
        _apply(answer, call)
        resolution = self._resolution(call.spec, call.node, call.registry)
        if answer is not None and _answered(answer):
            return ExecResult(value=answer.value, resolution=resolution, detail={"fake": True})
        return ExecResult(value=self._value(call), resolution=resolution, detail={"fake": True})

    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        found = self._resolution(context.spec, context.node, context.registry)
        return found if found is not None else {}

    def enter(self, call: ExecContext) -> ContainerEntry:
        branches = call.spec.branches or ("body",)
        detail: dict[str, JsonValue] = {"fake": True}
        hook = self.cassette_hook
        if call.op == CASSETTE_OP and hook is not None:
            detail["cassette"] = dict(hook.opened(self._cassette_request(call)))
        return ContainerEntry(branches=branches, detail=detail)

    def exit(self, call: ExecContext, body: BodyOutcome) -> ExecResult:
        answer = answer_for(self.profile, call.node, call.attempt)
        _apply(answer, call)
        artifacts: list[str] = []
        hook = self.cassette_hook
        if call.op == CASSETTE_OP and hook is not None:
            recorded: dict[str, JsonValue] = {"node": call.node.id, "status": body.status,
                                              "branches": list(body.branches)}
            closed = hook.closed(self._cassette_request(call), recorded)
            if closed is not None:
                artifacts.append(closed)
        resolution = self._resolution(call.spec, call.node, call.registry)
        value = answer.value if answer is not None and _answered(answer) else self._value(call)
        return ExecResult(value=value, artifacts=artifacts, resolution=resolution, detail={"fake": True})

    def _resolution(self, op: OpSpec, node: Node, registry: Registry) -> dict[str, JsonValue] | None:
        if not machine_dependent(node, registry):
            return None
        answer = answer_for(self.profile, node, 1)
        if answer is not None and answer.resolution is not None:
            return dict(answer.resolution)
        return default_resolution(op, node, registry)

    def _value(self, call: ExecContext) -> JsonValue:
        op, node, data = call.spec, call.node, call.data
        if op.name == "media.source":
            return _source_value(data)
        if node.out == "artifact":
            made = _artifact_file(op, node, data)
            return call.artifacts.write(made.data, filename=made.filename, kind=made.kind).model_dump(mode="json")
        if node.out == "media":
            fields = _media_fields(op, data)
            made = _media_file(op, node, fields)
            artifact = call.artifacts.write(made.data, filename=made.filename, kind=made.kind)
            info = MediaInfo.model_validate({**fields, "uri": f"artifact:{artifact.id}", "artifact": artifact.id})
            return info.model_dump(mode="json", exclude_none=True)
        value = _default_value(op, node, call.args, data, call.session)
        if op.name == "check.checkpoint" and isinstance(value, dict) and _wants_shot(data):
            label = value.get("label")
            stem = label if isinstance(label, str) and label else f"checkpoint-{_short(node)}"
            shot = call.artifacts.write(png_bytes(f"{op.name}:{_node_key(node)}"), filename=f"{stem}.png",
                                        kind=_SHOT_KIND)
            value["shots"] = [shot.id]
        return value

    def _cassette_request(self, call: ExecContext) -> CassetteRequest:
        backend = _session_backend(call)
        return CassetteRequest.model_validate({
            "run": call.run,
            "node_id": call.node.id,
            "name": call.data.get("name"),
            "mode": call.data.get("mode", "auto"),
            "key": cassette_key(call.plan, call.node, backend),
            "backend": backend,
        })


def _session_backend(call: ExecContext) -> BackendIdentity | None:
    by_id = {resource.id: resource for resource in call.plan.resources}
    sessions = {by_id[resource_id].session for resource_id in call.node.uses
                if resource_id in by_id and by_id[resource_id].kind.startswith("lease.")}
    for resource in call.plan.resources:
        if resource.kind == "backend" and (resource.id in call.node.uses or resource.session in sessions):
            handle = call.resources.backend(resource.id)
            if handle is not None:
                return handle.identity
    return None


def _apply(answer: FakeAnswer | None, call: ExecContext) -> None:
    if answer is None:
        return
    if answer.delay_ms:
        wait = answer.delay_ms / 1000.0
        remaining = call.remaining()
        call.cancel.wait(wait if remaining is None else min(wait, remaining))
        call.check_cancelled()
    if answer.fail is not None:
        raise NodeFailure(answer.code, answer.fail, retryable=answer.retryable, soft=answer.soft)


def _require_plan(cassette_hook: CassetteHook | None, plan: Plan | None) -> None:
    if cassette_hook is not None and plan is None:
        raise ValueError("a fake driver with a cassette hook needs the run's plan to derive cassette keys")


class FakeDriverPort:
    def __init__(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo, profile: FakeProfile,
                 registry: Registry, scratch: Path, *, cassette_hook: CassetteHook | None = None,
                 plan: Plan | None = None, backends: Mapping[str, BackendIdentity] | None = None) -> None:
        _require_plan(cassette_hook, plan)
        self.lease = lease
        self.session = session
        self.run = run
        self.profile = profile
        self.registry = registry
        self.scratch = scratch
        self.cassette_hook = cassette_hook
        self.plan = plan
        self.backends: dict[str, BackendIdentity] = dict(backends or {})
        self.calls: list[tuple[str, CallPhase]] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._count = 0

    @property
    def platform(self) -> str:
        return self.lease.platform

    @property
    def simulated(self) -> bool:
        return True

    def call(self, op: str, args: Mapping[str, JsonValue], *, site: CallSite, phase: CallPhase = "run",
             timeout: float | None = None) -> DriverReply:
        self._require_open()
        with self._lock:
            self.calls.append((op, phase))
            self._count += 1
            number = self._count
        if not self.registry.has_op(op):
            raise DriverError("driver_error", f"the fake driver knows no op {op}")
        spec = self.registry.op(op)
        if spec.container == (phase == "run"):
            expected = "enter or exit" if spec.container else "run"
            raise DriverError("driver_error", f"{op} takes the phase {expected}, not {phase}")
        if phase == "enter":
            detail: dict[str, JsonValue] = {"fake": True, "branches": list(spec.branches or ("body",))}
            hook = self.cassette_hook
            if op == CASSETTE_OP and hook is not None:
                detail["cassette"] = dict(hook.opened(self._cassette_request(site, args)))
            return DriverReply(detail=detail)
        reply = self._answer(op, spec, args, site, number, timeout)
        hook = self.cassette_hook
        if phase == "exit" and op == CASSETTE_OP and hook is not None:
            recorded: dict[str, JsonValue] = {"node": site.node.id, "instance": site.instance,
                                              "attempt": site.attempt}
            closed = hook.closed(self._cassette_request(site, args), recorded)
            reply = replace(reply, detail={**reply.detail, "cassette": closed})
        return reply

    def _answer(self, op: str, spec: OpSpec, args: Mapping[str, JsonValue], site: CallSite, number: int,
                timeout: float | None) -> DriverReply:
        answer = answer_for(self.profile, site.node, site.attempt)
        if answer is not None:
            if answer.delay_ms:
                self._delay(answer.delay_ms, timeout)
            if answer.fail is not None:
                raise DriverError(answer.code, answer.fail, retryable=answer.retryable, soft=answer.soft)
            if _answered(answer):
                return DriverReply(value=answer.value, detail={"fake": True, "answered": True})
        if op == "session.as":
            return DriverReply(detail={"fake": True})
        try:
            with using_registry(self.registry):
                model = spec.args.model_validate(dict(args))
        except ValidationError as problem:
            raise DriverError("args_invalid", f"{op} arguments do not validate: {problem.errors()[0]['msg']}") from None
        return self._reply(spec, site, model, args, number)

    def _cassette_request(self, site: CallSite, args: Mapping[str, JsonValue]) -> CassetteRequest:
        plan = self.plan
        if plan is None:
            raise DriverError("driver_error", "the fake driver has a cassette hook but no plan")
        backend = self._backend(plan, site.node)
        return CassetteRequest.model_validate({
            "run": self.run,
            "node_id": site.node.id,
            "name": args.get("name"),
            "mode": args.get("mode", "auto"),
            "key": cassette_key(plan, site.node, backend),
            "backend": backend,
        })

    def _backend(self, plan: Plan, node: Node) -> BackendIdentity | None:
        for resource in plan.resources:
            if resource.kind != "backend" or resource.id not in self.backends:
                continue
            if resource.id in node.uses or resource.session == self.lease.session_key:
                return self.backends[resource.id]
        if len(self.backends) == 1:
            return next(iter(self.backends.values()))
        return None

    def _reply(self, spec: OpSpec, site: CallSite, model: BaseModel, data: Mapping[str, JsonValue],
               number: int) -> DriverReply:
        node = site.node
        detail: dict[str, JsonValue] = {"fake": True}
        if spec.name == "media.source":
            return DriverReply(value=_source_value(data), detail=detail)
        if node.out == "artifact":
            made = _artifact_file(spec, node, data)
            return DriverReply(files=(self._file(made, site, number, "value"),), detail=detail)
        if node.out == "media":
            fields = _media_fields(spec, data)
            made = _media_file(spec, node, fields)
            return DriverReply(value=fields, files=(self._file(made, site, number, "value"),), detail=detail)
        value = _default_value(spec, node, model, data, self.session)
        if spec.name == "check.checkpoint" and isinstance(value, dict) and _wants_shot(data):
            label = value.get("label")
            stem = label if isinstance(label, str) and label else f"checkpoint-{_short(node)}"
            made = _FakeFile(png_bytes(f"{spec.name}:{_node_key(node)}"), f"{stem}.png", _SHOT_KIND)
            return DriverReply(value=value, files=(self._file(made, site, number, "shot"),), detail=detail)
        return DriverReply(value=value, detail=detail)

    def _file(self, made: _FakeFile, site: CallSite, number: int, role: FileRole) -> DriverFile:
        self.scratch.mkdir(parents=True, exist_ok=True)
        suffix = Path(made.filename).suffix
        path = self.scratch / f"{_node_key(site.node)[7:23]}-{site.attempt}-{number}-{role}{suffix}"
        path.write_bytes(made.data)
        return DriverFile(path=path, role=role, kind=made.kind, name=made.filename)

    def _delay(self, delay_ms: int, timeout: float | None) -> None:
        wait = delay_ms / 1000.0
        bounded = wait if timeout is None else max(0.0, min(wait, timeout))
        deadline = time.monotonic() + bounded
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._closed.wait(min(remaining, 0.05)):
                raise DriverError("driver_error", "port closed")
        if bounded < wait:
            raise DriverError("timeout", f"the fake answer waits {delay_ms} ms, longer than the call timeout of "
                                         f"{bounded:.3f} s")

    def snapshot(self, kinds: Sequence[SnapshotKind], dest: Path) -> list[DriverFile]:
        self._require_open()
        with self._lock:
            self._count += 1
            number = self._count
        dest.mkdir(parents=True, exist_ok=True)
        files: list[DriverFile] = []
        for index, kind in enumerate(kinds):
            name = f"{kind}-{number}-{index}"
            if kind == "screen":
                path = dest / f"{name}.png"
                path.write_bytes(png_bytes(f"snapshot:{self.lease.resource_id}:{number}:{index}"))
                files.append(DriverFile(path=path, role="evidence", kind=_SHOT_KIND, name=path.name,
                                        caption="fake screen snapshot"))
            else:
                path = dest / f"{name}.html"
                page = (f"<!doctype html><html><head><title>fake {kind}</title></head><body><p>fake {kind} snapshot "
                        f"of {self.lease.resource_id}</p></body></html>\n")
                path.write_text(page, encoding="utf-8")
                files.append(DriverFile(path=path, role="evidence", kind=kind, name=path.name,
                                        caption=f"fake {kind} snapshot"))
        return files

    def version(self) -> str:
        return _FAKE_PORT_VERSION

    def close(self) -> None:
        self._closed.set()

    def _require_open(self) -> None:
        if self._closed.is_set():
            raise DriverError("driver_error", "port closed")


class FakeDriverProvider:
    def __init__(self, profile: FakeProfile, registry: Registry, scratch: Path, *,
                 cassette_hook: CassetteHook | None = None, plan: Plan | None = None,
                 backends: Mapping[str, BackendIdentity] | None = None) -> None:
        _require_plan(cassette_hook, plan)
        self.profile = profile
        self.registry = registry
        self.scratch = scratch
        self.cassette_hook = cassette_hook
        self.plan = plan
        self.backends: dict[str, BackendIdentity] = dict(backends or {})

    @property
    def name(self) -> str:
        return FAKE_DRIVER

    @property
    def platforms(self) -> frozenset[str]:
        return FAKE_PLATFORMS

    def open(self, lease: LeaseHandle, session: SessionSpec | None, run: RunInfo) -> DriverPort:
        return FakeDriverPort(lease, session, run, self.profile, self.registry,
                              self.scratch / f"{run.id}-{lease.resource_id}", cassette_hook=self.cassette_hook,
                              plan=self.plan, backends=self.backends)


class FakeSeedHook:
    def restore(self, request: SeedRestoreRequest, info: Mapping[str, JsonValue], *,
                registry: Registry | None = None) -> SeedRestoreResult:
        return SeedRestoreResult(resource_id=request.resource_id, seed=request.seed, key=request.key, restored=True,
                                 snapshot=f"fake:{request.key[7:23]}", detail={"fake": True})


class FakeCassetteHook:
    def __init__(self) -> None:
        self.recorded: dict[str, dict[str, JsonValue]] = {}
        self._lock = threading.Lock()

    def available(self, request: CassetteRequest) -> bool:
        if request.mode in ("record", "auto"):
            return True
        with self._lock:
            return request.key in self.recorded

    def opened(self, request: CassetteRequest) -> Mapping[str, JsonValue]:
        return {"mode": request.mode, "key": request.key}

    def closed(self, request: CassetteRequest, recorded: Mapping[str, JsonValue]) -> str | None:
        with self._lock:
            self.recorded[request.key] = dict(recorded)
        return None


__all__ = [
    "FAKE_DRIVER",
    "FAKE_EXECUTOR",
    "FAKE_PLATFORMS",
    "FAKE_URL",
    "FAKE_VERSION",
    "SHOT_SIZE",
    "FakeCassetteHook",
    "FakeDriverPort",
    "FakeDriverProvider",
    "FakeExecutors",
    "FakeSeedHook",
    "answer_for",
    "covers",
    "default_resolution",
    "default_value",
    "png_bytes",
]
