from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from eks_harness.ir import SCHEMA_ID
from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.presets import Viewport
from eks_harness.ir.refs import reject_field_name_keys
from eks_harness.ir.target import Platform
from eks_harness.ir.time import Fps, Span, TempoMap, Time

PlanKind = Literal["flow", "seed", "edit", "scene", "score"]
BindingKind = Literal["session", "literal", "time", "target", "namespace", "factory"]
NODE_ID_PATTERN = r"^n[0-9a-z]{10}(?:\.[0-9]+)*$"
RESOURCE_ID_PATTERN = r"^r[0-9a-z]{7}$"
HASH_PATTERN = r"^(?:sha256:[0-9a-f]{64})?$"
TIMEZONE_PATTERN = re.compile(r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+)$")
LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class PlanError(ValueError):
    pass


class _IR(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, serialize_by_alias=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_field_name_keys(cls, data: object) -> object:
        return reject_field_name_keys(cls, data)


class SourceInfo(_IR):
    file: str = Field(description="Source file of the entry, relative to the tree root")
    qualname: str = Field(description="Qualified name of the entry function")
    line: int | None = Field(default=None, description="First line of the entry function")


class Requirement(_IR):
    plugin: str = Field(description="Id of a plugin whose contributions the plan uses")
    version: str = Field(description="Version of that plugin the plan was built with")


class Binding(_IR):
    kind: BindingKind = Field(description="How the value is rebuilt in the sandbox: session, literal, time, target, namespace or factory")
    value: JsonValue = Field(default=None, description="JSON form of the bound value")


class FunctionSource(_IR):
    name: str = Field(description="Function name")
    source: str = Field(description="Source text of the function")
    hash: str = Field(pattern=HASH_PATTERN, description="Hash of the source text, part of the node hash")
    params: tuple[str, ...] = Field(default=(), description="Parameter names")
    freevars: dict[str, Binding] = Field(default_factory=dict, description="Closure variables and how they are rebuilt")
    globals: tuple[str, ...] = Field(default=(), description="Global names the function reads")


def check_timezone(value: str | None) -> str | None:
    if value is not None and TIMEZONE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"timezone {value!r} is not an IANA time zone name such as 'Europe/Istanbul' or 'UTC'")
    return value


def check_locale(value: str | None) -> str | None:
    if value is not None and LOCALE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"locale {value!r} is not a BCP 47 tag such as 'tr-TR'")
    return value


class CheckUse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, description="Registered check kind such as console_errors, visual or perf")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Parameters of the check's params model")


class SessionSpec(_IR):
    key: str = Field(default="main", description="Session key: main for flows, take0, take1 and so on for takes")
    app: str = Field(description="App profile name such as web-app")
    platform: Platform = Field(description="Platform the session drives")
    seed: str | None = Field(default=None, description="Seed scenario restored into the backend")
    persona: str | None = Field(default=None, description="Persona the session logs in as")
    backend: str | None = Field(default=None, description="Backend definition such as eksenapi; the seed's backend when unset")
    viewport: str | None = Field(default=None, description="Viewport preset of the session")
    pace: str | None = Field(default=None, description="Pace preset used while recording")
    timeout: Duration | None = Field(default=None, description="Default timeout of the session's ops")
    noise: tuple[str, ...] = Field(default=(), description="Message substrings ignored by the default checkpoint checks")
    root_seed: int | None = Field(default=None, description="Root random seed of the session; the seed scenario's when unset")
    now: str | None = Field(default=None, description="Frozen wall clock of the device, ISO 8601 with an offset; the seed scenario's when unset")
    timezone: str | None = Field(default=None, description="IANA time zone of the device or browser, such as Europe/Istanbul; the app profile's when unset")
    locale: str | None = Field(default=None, description="Locale of the device or browser, such as tr-TR; the app profile's when unset")
    permissions: Literal["ask", "grant", "deny"] | None = Field(default=None, description="Runtime permission prompts on iOS and Android: grant or deny the app's permissions before launch, or ask (leave the OS prompts, reachable through native targets); ask when unset")
    network_ignore: tuple[str, ...] = Field(default=(), description="URL substrings left out of network capture and the failed requests check")
    viewports: dict[str, Viewport] = Field(default_factory=dict, description="Viewports the app profile defines, by name; they win over viewport presets of the same name")
    checks: tuple[CheckUse, ...] = Field(default=(), description="Checks every checkpoint without its own checks= runs on top of the platform's default checks, such as performance budgets")
    defaults: tuple[CheckUse, ...] = Field(default=(), description="The platform's default checks, resolved when the plan is built: every registered check with default=True of the checkpoint, visual or perf family that runs on the session platform and whose kind checks does not name; checkpoints without their own checks= run these and then checks")

    @field_validator("now")
    @classmethod
    def _normalize_now(cls, value: str | None) -> str | None:
        return normalize_now(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return check_timezone(value)

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return check_locale(value)


class Clock(_IR):
    now: str | None = Field(default=None, description="Frozen wall clock of the plan, ISO 8601 with an offset")
    seed: int = Field(default=0, description="Root random seed; every node derives its own seed from it")
    fps: Fps | None = Field(default=None, description="Frame rate that frame times resolve against")
    window: Span | None = Field(default=None, description="Time window of the plan, such as the cut of a score")
    duration: Duration | None = Field(default=None, description="Length of the plan's timeline; 'end' resolves to it")
    tempo: TempoMap | None = Field(default=None, description="Tempo map that beat, downbeat and bar times resolve against")
    cues: dict[str, Time] = Field(default_factory=dict, description="Named cue times, referenced as cue:<name>")

    @field_validator("now")
    @classmethod
    def _normalize_now(cls, value: str | None) -> str | None:
        return normalize_now(value)


class Resource(_IR):
    id: str = Field(pattern=RESOURCE_ID_PATTERN, description="Resource id, derived from its kind and identity")
    kind: str = Field(description="Resource kind: lease.web, lease.ios, lease.android or backend")
    app: str | None = Field(default=None, description="App profile the lease or backend serves")
    definition: str | None = Field(default=None, description="Backend definition such as eksenapi")
    seed: str | None = Field(default=None, description="Seed scenario restored into the backend")
    session: str | None = Field(default=None, description="Key of the session the resource belongs to")
    name: str | None = Field(default=None, description="Display name of the resource")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Extra parameters for the resource planner")


class Node(_IR):
    id: str = Field(pattern=NODE_ID_PATTERN, description="Stable node id, derived from the source position and call path")
    op: str = Field(description="Registered op name such as ui.tap")
    v: str = Field(default="1", description="Op version the node was built with")
    args: dict[str, JsonValue] = Field(default_factory=dict, description="Arguments of the op, validated by its argument model")
    uses: list[str] = Field(default_factory=list, description="Ids of the resources the node runs on")
    after: list[str] = Field(default_factory=list, description="Ids of earlier nodes this node waits for")
    out: str = Field(default="none", description="Ref type of the node's output")
    pin: Time | None = Field(default=None, description="Take time the node is pinned to (at=)")
    label: str | None = Field(default=None, description="Human label of the node")
    src: str | None = Field(default=None, description="Source position, file:line")
    branches: dict[str, list[Node]] | None = Field(default=None, description="Child nodes per branch of a container op")
    hash: str = Field(default="", pattern=HASH_PATTERN, description="Merkle hash of the node")


class Plan(_IR):
    schema_: Literal["eks/plan@1"] = Field(default=SCHEMA_ID, alias="schema", description="Schema id of the plan IR")
    kind: PlanKind = Field(description="Entry kind: flow, seed, edit, scene or score")
    name: str = Field(description="Entry name")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="Parameters the entry was planned with")
    clock: Clock = Field(default_factory=Clock, description="Frozen clock, root seed and time base of the plan")
    session: SessionSpec | None = Field(default=None, description="Session of a flow plan")
    resources: list[Resource] = Field(default_factory=list, description="Leases and backends the plan needs")
    nodes: list[Node] = Field(default_factory=list, description="Root nodes in program order")
    outputs: JsonValue = Field(default=None, description="Values the entry returns, with $ref expressions")
    personas: list[str] = Field(default_factory=list, description="Persona keys a seed plan creates")
    requires: list[Requirement] = Field(default_factory=list, description="Plugins the plan was built with")
    base: str = Field(default=".", description="Directory of the entry file relative to the tree root")
    tags: list[str] = Field(default_factory=list, description="Tags of the entry")
    source: SourceInfo | None = Field(default=None, description="Where the entry is defined")
    hash: str = Field(default="", pattern=HASH_PATTERN, description="Merkle hash of the plan")

    def walk(self) -> Iterator[tuple[Node, tuple[Node, ...]]]:
        yield from _walk(self.nodes, ())

    def node(self, node_id: str) -> Node:
        for node, _ancestors in self.walk():
            if node.id == node_id:
                return node
        raise PlanError(f"plan {self.name!r} has no node {node_id!r}")

    def resource(self, resource_id: str) -> Resource:
        for resource in self.resources:
            if resource.id == resource_id:
                return resource
        raise PlanError(f"plan {self.name!r} has no resource {resource_id!r}")

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True, indent=indent)

    @classmethod
    def from_json(cls, text: str | bytes) -> Plan:
        return cls.model_validate_json(text)


def _walk(nodes: list[Node], ancestors: tuple[Node, ...]) -> Iterator[tuple[Node, tuple[Node, ...]]]:
    for node in nodes:
        yield node, ancestors
        if node.branches is not None:
            inner = (*ancestors, node)
            for branch in node.branches.values():
                yield from _walk(branch, inner)


def normalize_now(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid clock time {value!r}; write ISO 8601 with an offset such as 2026-01-15T09:00+03:00") from error
    if moment.utcoffset() is None:
        raise ValueError(f"clock time {value!r} has no UTC offset; write it with one such as {value}+03:00")
    return moment.isoformat(timespec="seconds")


__all__ = [
    "HASH_PATTERN",
    "NODE_ID_PATTERN",
    "RESOURCE_ID_PATTERN",
    "Binding",
    "BindingKind",
    "CheckUse",
    "Clock",
    "FunctionSource",
    "Node",
    "Plan",
    "PlanError",
    "PlanKind",
    "Requirement",
    "Resource",
    "SessionSpec",
    "SourceInfo",
    "check_locale",
    "check_timezone",
    "normalize_now",
]
