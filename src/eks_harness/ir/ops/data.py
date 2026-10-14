from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from eks_harness.ir.ops.api import BACKEND
from eks_harness.ir.refs import RefExpr, RefKind, RJson
from eks_harness.ir.registry import NAME_PATTERN, Args, Phase, op

RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
RESOURCE_TYPE_PREFIX = "resource:"
RESOURCE_KIND_PATTERN = NAME_PATTERN
ResourceRef = Annotated[RefExpr, RefKind("json")]


def refers_to(kind: str) -> RefKind:
    if not isinstance(kind, str) or RESOURCE_KIND_PATTERN.fullmatch(kind) is None:
        raise ValueError(f"refers_to() takes a resource kind name such as 'federation', got {kind!r}")
    return RefKind(RESOURCE_TYPE_PREFIX + kind)


def referred_kind(ref_type: str | None) -> str | None:
    if ref_type is None or not ref_type.startswith(RESOURCE_TYPE_PREFIX):
        return None
    return ref_type.removeprefix(RESOURCE_TYPE_PREFIX)
PATH_SEPARATOR = "/"
KEY_SEPARATOR = ":"
INDEX_SEPARATOR = "#"


def resource_segment(kind: str, key: str | None, index: int) -> str:
    return f"{kind}{KEY_SEPARATOR}{key}" if key is not None else f"{kind}{INDEX_SEPARATOR}{index}"


def resource_path(parent: str | None, segment: str) -> str:
    return f"{parent}{PATH_SEPARATOR}{segment}" if parent is not None else segment


def keyless_position(path: str) -> tuple[str | None, str, int] | None:
    parent, _slash, segment = path.rpartition(PATH_SEPARATOR)
    kind, separator, index = segment.partition(INDEX_SEPARATOR)
    if not separator or not index.isdigit() or KEY_SEPARATOR in kind:
        return None
    return parent or None, kind, int(index)


RESERVED_ROOT_KINDS = frozenset({"add", "base", "create", "extend", "now", "seed"})
RESERVED_CHILD_KINDS = frozenset({"expr", "key", "kind", "path", "ref"})


class Resource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=False)


@op("data", name="create", out="json", resources=BACKEND, ordering="graph",
    description="Create one seed resource (API or SQL) after its parent and the resources it references",
    examples=[
        {"kind": "tenant", "key": "harman", "path": "tenant:harman", "fields": {"name": "Harman Kuyumculuk"}},
        {
            "kind": "bank_account",
            "key": "kuveyt",
            "path": "tenant:harman/bank_account:kuveyt",
            "parent": {"$ref": "n0000000001", "path": []},
            "fields": {"currency": "TRY", "opening": 250000},
        },
    ])
class Create(Args):
    kind: str = Field(min_length=1, description="Registered resource kind such as tenant or bank_account")
    key: str | None = Field(default=None, pattern=RESOURCE_KEY_PATTERN, description="Key of the resource among its siblings; keyless kinds count siblings instead")
    path: str = Field(min_length=1, description="Resource path in the seed graph, such as tenant:harman/bank_account:kuveyt")
    parent: ResourceRef | None = Field(default=None, description="$ref to the parent resource's create node")
    fields: dict[str, RJson] = Field(default_factory=dict, description="Fields of the resource kind's model")
    phase: Phase = Field(default="api", description="Creation phase: schema, api, finalize or mongo")


@op("data", name="extend", resources=BACKEND, ordering="graph",
    description="Restore another seed's snapshot into the backend; the engine runs every data.extend of a seed "
                "before any of its data.create nodes, which the seed declares after its extends",
    examples=[{"base": "standard", "hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000"}])
class Extend(Args):
    base: str = Field(min_length=1, description="Name of the seed whose snapshot is restored first")
    hash: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$",
        description="Plan hash of the base seed when this seed was planned; part of this seed's snapshot key")


@op("data", name="base", out="json", resources=BACKEND, ordering="graph",
    description="Look up a resource that a base seed (a data.extend of this seed, directly or through its own bases) "
                "creates, by its path, so this seed can add children to it or refer to it without declaring it again",
    examples=[{"base": "standard", "kind": "tenant", "key": "harman", "path": "tenant:harman"}])
class BaseResource(Args):
    base: str = Field(min_length=1, description="Name of the seed this seed extends whose snapshot holds the resource")
    kind: str = Field(min_length=1, description="Resource kind of the base resource, such as tenant")
    key: str | None = Field(default=None, pattern=RESOURCE_KEY_PATTERN, description="Key of the base resource; unset for keyless kinds")
    path: str = Field(min_length=1, description="Resource path in the base seed graph, such as tenant:harman")


__all__ = [
    "INDEX_SEPARATOR",
    "KEY_SEPARATOR",
    "PATH_SEPARATOR",
    "RESERVED_CHILD_KINDS",
    "RESERVED_ROOT_KINDS",
    "RESOURCE_KEY_PATTERN",
    "RESOURCE_KIND_PATTERN",
    "RESOURCE_TYPE_PREFIX",
    "BaseResource",
    "Create",
    "Extend",
    "Resource",
    "ResourceRef",
    "keyless_position",
    "referred_kind",
    "refers_to",
    "resource_path",
    "resource_segment",
]
