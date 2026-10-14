from __future__ import annotations

from collections.abc import Mapping
from typing import cast, get_args

from pydantic import JsonValue, ValidationError

from eks_harness.api.errors import ApiError
from eks_harness.engine.drivers import (
    CallPhase,
    CallSite,
    DriverFile,
    DriverPort,
    DriverReply,
)
from eks_harness.engine.errors import DriverError, NodeFailure
from eks_harness.engine.executors import (
    BodyOutcome,
    ContainerEntry,
    ExecContext,
    ExecResult,
    ExecutorSpec,
    builtin_provenance,
)
from eks_harness.ir.registry import Location
from eks_harness.ir.types import Artifact, MediaInfo, MediaKind
from eks_harness.ops import BUILTIN_EXECUTOR_VERSION, WORKER_EXECUTOR

WORKER_LOCATIONS: frozenset[str] = frozenset({"worker"})

_CONTAINER_SUFFIX = ".container"
_MEDIA_KINDS: frozenset[str] = frozenset(get_args(MediaKind))
_SERVER_ERROR = 500


def _require_port(call: ExecContext, op: str) -> DriverPort:
    port = call.driver
    if port is None:
        kinds = {resource.id: resource.kind for resource in call.plan.resources}
        leases = [kinds[resource_id] for resource_id in call.node.uses
                  if kinds.get(resource_id, "").startswith("lease.")]
        held = f" ({', '.join(leases)})" if leases else ""
        raise DriverError("driver_missing",
                          f"node {call.node.id} ({op}) needs a driver port for its lease{held}, and no driver serves "
                          "it on this hub",
                          detail={"op": op, "leases": cast(list[JsonValue], leases)})
    return port


def _port_call(call: ExecContext, op: str, args: Mapping[str, JsonValue], phase: CallPhase) -> DriverReply:
    port = _require_port(call, op)
    call.check_cancelled()
    site = CallSite(call.node, call.instance, call.attempt)
    try:
        return port.call(op, args, site=site, phase=phase, timeout=call.remaining())
    except NodeFailure:
        raise
    except Exception as error:
        raise DriverError("driver_error",
                          f"the {port.platform} driver failed {op} ({phase}) of node {call.node.id}: "
                          f"{type(error).__name__}: {error}",
                          detail={"op": op, "phase": phase, "type": type(error).__name__,
                                  "message": str(error)}) from error


def _file_name(file: DriverFile) -> str:
    name = file.name or file.path.name
    suffix = file.path.suffix
    if suffix and not name.lower().endswith(suffix.lower()):
        return name + suffix
    return name


_BOXES_SUFFIX = ".boxes.json"


def _ingest_file(call: ExecContext, file: DriverFile, meta: Mapping[str, JsonValue] | None = None) -> Artifact:
    try:
        return call.artifacts.ingest(file.path, kind=file.kind, name=_file_name(file), caption=file.caption,
                                     meta=meta)
    except ApiError as error:
        if error.status >= _SERVER_ERROR:
            raise
        raise _unreadable(call, file, error.message) from error
    except OSError as error:
        raise _unreadable(call, file, str(error)) from error


def _unreadable(call: ExecContext, file: DriverFile, reason: str) -> DriverError:
    return DriverError("driver_error",
                       f"the {file.role} file {file.path.name} of node {call.node.id} ({call.op}) could not be "
                       f"ingested: {reason}",
                       detail={"file": file.path.name, "role": file.role})


def _media_kind(call: ExecContext, reply_kind: JsonValue, file: DriverFile, artifact: Artifact) -> str:
    for candidate in (reply_kind, file.kind, artifact.kind):
        if isinstance(candidate, str) and candidate in _MEDIA_KINDS:
            return candidate
    expected = ", ".join(get_args(MediaKind))
    raise DriverError("driver_error",
                      f"the driver's media reply for node {call.node.id} ({call.op}) names no media kind (got "
                      f"{reply_kind!r}, file {file.kind!r}, artifact {artifact.kind!r}); expected one of {expected}",
                      detail={"artifact": artifact.id})


def _output(call: ExecContext, file: DriverFile, artifact: Artifact, reply_value: JsonValue) -> JsonValue:
    out = call.node.out
    if out == "artifact":
        return artifact.model_dump(mode="json", by_alias=True)
    if out == "media":
        fields: dict[str, JsonValue] = dict(reply_value) if isinstance(reply_value, dict) else {}
        fields["kind"] = _media_kind(call, fields.get("kind"), file, artifact)
        fields["uri"] = f"artifact:{artifact.id}"
        fields["artifact"] = artifact.id
        try:
            info = MediaInfo.model_validate(fields)
        except ValidationError as problem:
            first = problem.errors(include_url=False)[0]
            where = ".".join(str(part) for part in first["loc"]) or "(value)"
            raise DriverError("driver_error",
                              f"the driver's media reply for node {call.node.id} ({call.op}) is not a media value: "
                              f"{where}: {first['msg']}",
                              detail={"artifact": artifact.id}) from None
        return info.model_dump(mode="json", by_alias=True, exclude_none=True)
    raise DriverError("driver_error",
                      f"the driver returned a value file for node {call.node.id} ({call.op}), whose output is {out}, "
                      "not an artifact or media",
                      detail={"artifact": artifact.id, "out": out})


def _with_shots(call: ExecContext, value: JsonValue, shots: list[str]) -> JsonValue:
    if not isinstance(value, dict):
        raise DriverError("driver_error",
                          f"the driver returned shot files for node {call.node.id} ({call.op}) with a value that is "
                          "not an object, so the shots have nowhere to go",
                          detail={"shots": cast(list[JsonValue], shots)})
    existing = value.get("shots")
    previous: list[JsonValue] = list(existing) if isinstance(existing, list) else []
    return {**value, "shots": [*previous, *shots]}


def _ingest_files(call: ExecContext, files: tuple[DriverFile, ...]) -> list[tuple[DriverFile, Artifact]]:
    boxes: dict[str, str] = {}
    for file in files:
        if file.role == "evidence" and file.path.name.endswith(_BOXES_SUFFIX):
            boxes[file.path.name.removesuffix(_BOXES_SUFFIX)] = _ingest_file(call, file).id
    ingested: list[tuple[DriverFile, Artifact]] = []
    for file in files:
        if file.role == "evidence" and file.path.name.endswith(_BOXES_SUFFIX):
            continue
        linked = boxes.get(file.path.stem) if file.role in ("value", "shot") else None
        ingested.append((file, _ingest_file(call, file, {"boxes": linked} if linked else None)))
    return ingested


def _ingest_reply(call: ExecContext, reply: DriverReply) -> JsonValue:
    value = reply.value
    produced: tuple[DriverFile, Artifact] | None = None
    shots: list[str] = []
    for file, artifact in _ingest_files(call, reply.files):
        if file.role == "value":
            if produced is not None:
                raise DriverError("driver_error",
                                  f"the driver returned more than one value file for node {call.node.id} ({call.op})",
                                  detail={"artifacts": [produced[1].id, artifact.id]})
            produced = (file, artifact)
        elif file.role == "shot":
            shots.append(artifact.id)
    if produced is not None:
        value = _output(call, produced[0], produced[1], reply.value)
    if shots:
        value = _with_shots(call, value, shots)
    return value


class WorkerExecutor:
    def run(self, call: ExecContext) -> ExecResult:
        reply = _port_call(call, call.op, call.data, "run")
        return ExecResult(value=_ingest_reply(call, reply), detail=dict(reply.detail))

    def enter(self, call: ExecContext) -> ContainerEntry:
        reply = _port_call(call, call.op, call.data, "enter")
        evidence = [_ingest_file(call, file).id for file in reply.files]
        detail: dict[str, JsonValue] = dict(reply.detail)
        if evidence:
            detail["artifacts"] = cast(list[JsonValue], evidence)
        branches = tuple(call.spec.branches) if call.spec.branches else ("body",)
        return ContainerEntry(branches=branches, detail=detail)

    def exit(self, call: ExecContext, body: BodyOutcome) -> ExecResult:
        reply = _port_call(call, call.op, call.data, "exit")
        detail: dict[str, JsonValue] = {**dict(reply.detail), "body": body.status}
        return ExecResult(value=_ingest_reply(call, reply), detail=detail)

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(WORKER_EXECUTOR, BUILTIN_EXECUTOR_VERSION, ops=("*",), run=self.run,
                            locations=cast(frozenset[Location], WORKER_LOCATIONS), provenance=builtin_provenance(),
                            description="Routes worker ops to the driver port of the node's lease")

    def container_spec(self) -> ExecutorSpec:
        return ExecutorSpec(WORKER_EXECUTOR + _CONTAINER_SUFFIX, BUILTIN_EXECUTOR_VERSION, ops=("*",), container=self,
                            locations=cast(frozenset[Location], WORKER_LOCATIONS), provenance=builtin_provenance(),
                            description="Routes worker containers (enter and exit) to the driver port of the node's "
                                        "lease")


__all__ = ["WORKER_LOCATIONS", "WorkerExecutor"]
