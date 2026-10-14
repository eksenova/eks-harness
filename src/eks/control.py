from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, cast, overload

from pydantic import JsonValue

from eks.ref import (
    ItemRef,
    Json,
    Ref,
    contains_refs,
    encode,
    node_of,
    type_of,
)
from eks.tracing import (
    EACH_OP,
    PARALLEL_FLAG,
    Scope,
    TraceContext,
    TraceError,
    active_session,
    capture_function,
    current,
)
from eks_harness.ir.ops.ctl import ALLOWED_DYNAMIC_GLOBALS
from eks_harness.ir.plan import Node
from eks_harness.ir.refs import (
    ITEM_KEY,
    REF_KEY,
    ItemExpr,
    RefError,
    RefExpr,
    apply_path,
    map_exprs,
)
from eks_harness.ir.types import parse_ref_type

WHEN_OP = "ctl.when"
PARALLEL_OP = "ctl.parallel"
DYNAMIC_OP = "ctl.dynamic"


@contextmanager
def when(cond: Ref[bool] | Ref[Json]) -> Iterator[None]:
    if not isinstance(cond, Ref):
        raise TypeError(
            f"eks.when needs a Ref condition, got {type(cond).__name__} {cond!r}; "
            "branch on plan-time Python values with a plain if statement"
        )
    with current().container(WHEN_OP, {"cond": cond}, branch="then"):
        yield


@contextmanager
def otherwise() -> Iterator[None]:
    ctx = current()
    last = ctx.last_closed_container()
    if last is None or last.op != WHEN_OP or last.branches is None or "else" in last.branches:
        raise TraceError(
            "eks.otherwise() must directly follow a `with eks.when(...):` block in the same scope, "
            "and a when takes only one otherwise"
        )
    with ctx.branch(last.id, "else"):
        yield


@overload
def each[T](over: Ref[list[T]], *, limit: int | None = None) -> AbstractContextManager[Ref[T]]: ...


@overload
def each(over: Ref[Json] | Sequence[Any], *, limit: int | None = None) -> AbstractContextManager[Ref[Json]]: ...


def each(over: Ref[Any] | Sequence[Any], *, limit: int | None = None) -> AbstractContextManager[Ref[Any]]:
    return _each(over, limit)


@contextmanager
def _each(over: Ref[Any] | Sequence[Any], limit: int | None) -> Iterator[Ref[Any]]:
    if isinstance(over, Ref):
        with _runtime_each(over, limit) as item:
            yield item
        return
    if isinstance(over, str | bytes | bytearray | Mapping) or not isinstance(over, list | tuple):
        raise TypeError(
            f"eks.each takes a Ref or a list or tuple, got {type(over).__name__} {over!r}; "
            "wrap a single string in a list and loop over mappings with .items() in Python"
        )
    with _literal_each(cast("Sequence[Any]", over), limit) as item:
        yield item


@contextmanager
def _runtime_each(over: Ref[Any], limit: int | None) -> Iterator[Ref[Json]]:
    produced = parse_ref_type(type_of(over))
    if produced.name not in ("list", "json"):
        raise TypeError(
            f"eks.each iterates over a list, but this Ref produces {produced}; pass a Ref to a list such as "
            "s.api.get(...).items, or a Python list or tuple"
        )
    item_type = str(produced.item) if produced.name == "list" and produced.item is not None else "json"
    args: dict[str, Any] = {"over": over}
    if limit is not None:
        args["limit"] = limit
    with current().container(EACH_OP, args, each=True) as ref:
        yield ItemRef(node_of(ref), item_type)


@contextmanager
def _literal_each(over: Sequence[Any], limit: int | None) -> Iterator[Ref[Json]]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError(f"eks.each limit must be a positive integer, got {limit!r}")
    chosen = list(over if limit is None else over[:limit])
    elements: list[JsonValue] = []
    for index, element in enumerate(chosen):
        if contains_refs(element):
            raise TypeError(
                f"eks.each element {index} ({element!r}) holds refs; loop over refs with a Python for "
                "statement or pass a runtime list Ref to eks.each"
            )
        elements.append(encode(element))
    ctx = current()
    with ctx.template() as scope:
        yield ItemRef(_template_id(scope), "json")
    _expand(ctx, scope, elements)


def _template_id(scope: Scope) -> str:
    if scope.each is None:
        raise TraceError(f"template scope {scope.key!r} has no item id")
    return scope.each


def _expand(ctx: TraceContext, scope: Scope, elements: Sequence[JsonValue]) -> None:
    template_id = _template_id(scope)
    template_ids = [node.id for node in _walk(scope.nodes)]
    heads = ctx.lane_heads(scope)
    for index, element in enumerate(elements):
        idmap = {node_id: f"{node_id}.{index}" for node_id in template_ids}
        copies = [_copy(ctx, node, idmap, template_id, element, index) for node in scope.nodes]
        ctx.splice(
            copies,
            heads={lane: idmap[head] for lane, head in heads.items()},
            tails={lane: idmap[tail] for lane, tail in scope.tails.items()},
        )
    ctx.drop_nodes(template_ids)


def _walk(nodes: Sequence[Node]) -> Iterator[Node]:
    for node in nodes:
        yield node
        if node.branches is not None:
            for children in node.branches.values():
                yield from _walk(children)


def _copy(
    ctx: TraceContext, node: Node, idmap: Mapping[str, str], template_id: str, element: JsonValue, index: int,
) -> Node:
    def substitute(expr: RefExpr | ItemExpr) -> JsonValue:
        dumped: dict[str, JsonValue] = expr.model_dump(mode="json", by_alias=True)
        if isinstance(expr, ItemExpr):
            if expr.item == template_id:
                try:
                    return apply_path(element, expr.path)
                except RefError as error:
                    raise TraceError(f"eks.each element {index} ({element!r}) in {node.op}: {error}") from error
            if expr.item in idmap:
                return {ITEM_KEY: idmap[expr.item], "path": dumped["path"]}
            return dumped
        if expr.ref in idmap:
            return {REF_KEY: idmap[expr.ref], "path": dumped["path"]}
        return dumped

    substituted = map_exprs(dict(node.args), substitute)
    spec = ctx.registry.op(node.op)
    args = spec.args.model_validate(substituted).model_dump(mode="json", by_alias=True, exclude_none=True)
    branches = None
    if node.branches is not None:
        branches = {
            name: [_copy(ctx, child, idmap, template_id, element, index) for child in children]
            for name, children in node.branches.items()
        }
    return Node(
        id=idmap[node.id], op=node.op, v=node.v, args=args, uses=list(node.uses),
        after=[idmap.get(edge, edge) for edge in node.after], out=node.out, pin=node.pin, label=node.label,
        src=node.src, branches=branches)


@contextmanager
def parallel() -> Iterator[None]:
    ctx = current()
    if ctx.has_flag(PARALLEL_FLAG):
        raise TraceError("eks.parallel() cannot be nested inside another eks.parallel(); lanes are already independent")
    with ctx.container(PARALLEL_OP, parallel=True):
        yield


def dynamic(fn: Callable[..., Any]) -> Ref[Json]:
    name = getattr(fn, "__name__", repr(fn))
    inputs: dict[str, Any] = {}
    for parameter in inspect.signature(fn).parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TraceError(f"@eks.dynamic {name}: *{parameter.name} is not allowed; declare every input by name")
        if parameter.default is inspect.Parameter.empty:
            raise TraceError(
                f"@eks.dynamic {name}: parameter {parameter.name!r} needs a default "
                f"(the plan-time value or Ref it receives), for example {parameter.name}=s.ui.count('#row')"
            )
        inputs[parameter.name] = encode(parameter.default)
    source = capture_function(fn, allowed_globals=ALLOWED_DYNAMIC_GLOBALS)
    session = active_session()
    uses = list(session.resources.values()) if session is not None else []
    key_suffix = getattr(fn, "__qualname__", source.name)
    return current().record(DYNAMIC_OP, {"fn": source, "inputs": inputs}, uses=uses, key_suffix=key_suffix)


__all__ = ["ALLOWED_DYNAMIC_GLOBALS", "dynamic", "each", "otherwise", "parallel", "when"]
