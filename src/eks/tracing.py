from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import inspect
import io
import linecache
import math
import os
import posixpath
import re
import sys
import tokenize
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import CodeType, FrameType
from typing import Any, cast

from pydantic import BaseModel, JsonValue

from eks.ref import (
    SENTINEL_CLOSE,
    SENTINEL_OPEN,
    Ref,
    SentinelTable,
    encode,
    sentinel_scope,
)
from eks_harness.ir.hashing import (
    canonical_dump,
    derive_id,
    digest,
    hash_plan,
    link_clock,
    resource_id,
)
from eks_harness.ir.ops.ctl import (
    ALLOWED_DYNAMIC_GLOBALS,
    DATA_BINDING_KINDS,
    SANDBOX_BUILTINS,
    SandboxScan,
    sandbox_leaks,
    scan_sandbox_source,
)
from eks_harness.ir.plan import (
    Binding,
    Clock,
    FunctionSource,
    Node,
    Plan,
    PlanError,
    PlanKind,
    Requirement,
    Resource,
    SessionSpec,
    SourceInfo,
)
from eks_harness.ir.refs import ItemExpr, RefExpr, referenced_nodes, walk_exprs
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    EntrySpec,
    OpSpec,
    Provenance,
    Registry,
    ResourceRole,
    out_problem,
)
from eks_harness.ir.target import Target
from eks_harness.ir.time import Time, ensure_time, format_time
from eks_harness.ir.types import RefTypeError, parse_ref_type

LIBRARY_PACKAGES = ("eks", "eks_harness")
TRANSPARENT_MODULES = frozenset({"contextlib"})
ROOT_SCOPE = "root"
PARALLEL_FLAG = "parallel"
TEMPLATE_FLAG = "template"
EACH_OP = "ctl.each"
PARALLEL_OP = "ctl.parallel"
RESOURCE_FIELDS = ("app", "definition", "seed", "session", "name")
SINGLE_LANE = ""
FREE_LANE = "*"
_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")
PLUGIN_SCHEME = "plugin:"
PACKAGE_SCHEME = "pkg:"
FILE_SCHEME = "file:"
OWNED_SCHEMES = (PLUGIN_SCHEME, PACKAGE_SCHEME)
_PLUGIN_ROOTS: dict[PurePath, str] = {}
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})
_IMPORT_OP = "IMPORT_NAME"
_SKIPPED_TOKENS = frozenset({
    tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER,
})


class TraceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CallSite:
    file: str
    line: int
    col: int
    qualname: str
    text: str

    def src(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Scope:
    key: str
    container: str | None
    branch: str
    nodes: list[Node] = field(default_factory=list)
    parallel: bool = False
    tails: dict[str, str] = field(default_factory=dict)
    each: str | None = None
    flags: frozenset[str] = frozenset()
    template: bool = False
    closed: str | None = None


@dataclass(frozen=True, slots=True)
class _ContainerMode:
    parallel: bool
    each: bool


@dataclass(order=True)
class _Finalizer:
    order: int
    sequence: int
    fn: Callable[[TraceContext], None] = field(compare=False)


def _is_library(frame: FrameType) -> bool:
    module = frame.f_globals.get("__name__", "")
    if not isinstance(module, str):
        return False
    if module in TRANSPARENT_MODULES:
        return True
    return any(module == package or module.startswith(package + ".") for package in LIBRARY_PACKAGES)


def _call_text(frame: FrameType) -> tuple[str | None, int, int]:
    code = frame.f_code
    index = frame.f_lasti // 2
    positions = list(code.co_positions())
    line = frame.f_lineno or code.co_firstlineno
    if index < 0 or index >= len(positions):
        return None, line, 0
    start_line, end_line, start_col, end_col = positions[index]
    if start_line is None or end_line is None or start_col is None or end_col is None:
        return None, start_line or line, start_col or 0
    linecache.checkcache(code.co_filename)
    lines = linecache.getlines(code.co_filename, frame.f_globals)
    if not lines or start_line < 1 or end_line > len(lines):
        return None, start_line, start_col
    chunk = [text.encode("utf-8") for text in lines[start_line - 1:end_line]]
    if len(chunk) == 1:
        raw = chunk[0][start_col:end_col]
    else:
        raw = b"\n".join([chunk[0][start_col:], *chunk[1:-1], chunk[-1][:end_col]])
    text = normalize_call_text(raw.decode("utf-8", errors="replace"))
    return (text or None), start_line, start_col


def normalize_call_text(text: str) -> str:
    try:
        tokens = [
            token.string for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type not in _SKIPPED_TOKENS
        ]
    except (tokenize.TokenError, SyntaxError):
        return " ".join(text.split())
    return " ".join(token for token in tokens if token)


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _real_path(file: str) -> PurePath:
    path = PurePath(file)
    if path.is_absolute() or os.path.exists(file):
        return PurePath(os.path.realpath(file))
    return path


def register_plugin_root(plugin_id: str, root: Path) -> None:
    _PLUGIN_ROOTS[PurePath(os.path.realpath(root))] = plugin_id


def plugin_roots() -> dict[PurePath, str]:
    return dict(_PLUGIN_ROOTS)


def _owned_by(path: PurePath, module: str | None) -> tuple[str, PurePath] | None:
    plugins = [(plugin_id, root) for root, plugin_id in _PLUGIN_ROOTS.items() if path.is_relative_to(root)]
    if plugins:
        plugin_id, root = max(plugins, key=lambda item: len(item[1].parts))
        return f"{PLUGIN_SCHEME}{plugin_id}", root
    name = module if module is not None and module != "__main__" else _module_of_file(path)
    if name is None:
        return None
    top = name.split(".", 1)[0]
    package = sys.modules.get(top)
    locations = list(getattr(package, "__path__", None) or ())
    file = getattr(package, "__file__", None)
    roots = [PurePath(os.path.realpath(location)) for location in locations]
    if not roots and isinstance(file, str):
        roots = [PurePath(os.path.realpath(file)).parent]
    for root in roots:
        if path.is_relative_to(root):
            return f"{PACKAGE_SCHEME}{top}", root
    return None


def _module_of_file(path: PurePath) -> str | None:
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if isinstance(file, str) and name != "__main__" and PurePath(os.path.realpath(file)) == path:
            return name
    return None


def _walk(nodes: Sequence[Node]) -> Iterator[Node]:
    for node in nodes:
        yield node
        if node.branches is not None:
            for children in node.branches.values():
                yield from _walk(children)


class TraceContext:
    def __init__(
        self,
        *,
        kind: PlanKind,
        name: str,
        registry: Registry,
        entry: EntrySpec | None = None,
        clock: Clock | None = None,
        session: SessionSpec | None = None,
        params: Mapping[str, Any] | None = None,
        tree: Path | None = None,
        entry_code: Any = None,
    ) -> None:
        self._kind: PlanKind = kind
        self._name = name
        self._registry = registry
        self._entry = entry
        self._clock = clock if clock is not None else Clock()
        self._session = session
        self._params: dict[str, Any] = dict(params or {})
        self._tree = Path(tree).resolve() if tree is not None else None
        self._entry_code: CodeType | None = entry_code if isinstance(entry_code, CodeType) else None
        self._sentinels = SentinelTable()
        self._root = Scope(key=f"{kind}:{name}", container=None, branch=ROOT_SCOPE)
        self._scopes: list[Scope] = [self._root]
        self._nodes: dict[str, Node] = {}
        self._containers: dict[str, _ContainerMode] = {}
        self._branch_tails: dict[tuple[str, str], dict[str, str]] = {}
        self._opened_tails: dict[str, dict[str, str]] = {}
        self._heads: dict[str, dict[str, str]] = {}
        self._occurrences: dict[str, int] = {}
        self._resources: dict[str, Resource] = {}
        self._personas: list[str] = []
        self._requires: dict[str, str] = {}
        self._finalizers: list[_Finalizer] = []
        self._finalizer_count = 0
        self._outputs: JsonValue = None
        self._finalized = False

    @property
    def plan_kind(self) -> PlanKind:
        return self._kind

    @property
    def name(self) -> str:
        return self._name

    @property
    def entry(self) -> EntrySpec | None:
        return self._entry

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def session(self) -> SessionSpec | None:
        return self._session

    @property
    def params(self) -> Mapping[str, Any]:
        return self._params

    @property
    def tree(self) -> Path | None:
        return self._tree

    @property
    def sentinels(self) -> SentinelTable:
        return self._sentinels

    def scope(self) -> Scope:
        return self._scopes[-1]

    def in_runtime_each(self) -> bool:
        return any(scope.each is not None and not scope.template for scope in self._scopes)

    def record(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        uses: Sequence[str] | None = None,
        pin: Time | None = None,
        label: str | None = None,
        out: str | None = None,
        id_key: str | None = None,
        key_suffix: str | None = None,
    ) -> Ref[Any]:
        spec = self._registry.op(op)
        if spec.container:
            raise TraceError(f"{op} is a container op; record it with `with ctx.container({op!r}, ...):`")
        node = self._add(spec, args, uses=uses, pin=pin, label=label, out=out, id_key=id_key,
                         key_suffix=key_suffix, container=False)
        return Ref(node.id, node.out)

    @contextmanager
    def container(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        branch: str = "body",
        uses: Sequence[str] | None = None,
        pin: Time | None = None,
        label: str | None = None,
        out: str | None = None,
        id_key: str | None = None,
        flags: Sequence[str] = (),
        parallel: bool = False,
        each: bool = False,
    ) -> Iterator[Ref[Any]]:
        spec = self._registry.op(op)
        if not spec.container:
            raise TraceError(f"{op} is not a container op; record it with ctx.record({op!r}, ...)")
        self._check_branch(spec, branch)
        opened = dict(self.scope().tails)
        node = self._add(spec, args, uses=uses, pin=pin, label=label, out=out, id_key=id_key, key_suffix=None,
                         container=True)
        self._containers[node.id] = _ContainerMode(parallel=parallel, each=each)
        self._opened_tails[node.id] = opened
        with self._branch_scope(node, branch, flags):
            yield Ref(node.id, node.out)

    @contextmanager
    def branch(self, node_id: str, branch: str, *, flags: Sequence[str] = ()) -> Iterator[None]:
        node = next((candidate for candidate in self.scope().nodes if candidate.id == node_id), None)
        if node is None or node.branches is None:
            raise TraceError(
                f"node {node_id!r} is not a container recorded in the current scope; "
                "open the branch after the container, at the same nesting level"
            )
        self._check_branch(self._registry.op(node.op), branch)
        with self._branch_scope(node, branch, flags):
            yield

    def node(self, node_id: str) -> Node:
        node = self._nodes.get(node_id)
        if node is None:
            raise TraceError(f"node {node_id!r} is not recorded in this plan trace")
        return node

    def last_node(self) -> Node | None:
        nodes = self.scope().nodes
        return nodes[-1] if nodes else None

    def last_closed_container(self) -> Node | None:
        closed = self.scope().closed
        return self._nodes.get(closed) if closed is not None else None

    def resource(self, kind: str, **identity: Any) -> str:
        values = {key: encode(value) for key, value in identity.items() if value is not None}
        rid = resource_id(kind, values)
        if rid not in self._resources:
            fields = {key: values[key] for key in RESOURCE_FIELDS if key in values}
            params = {key: value for key, value in values.items() if key not in RESOURCE_FIELDS}
            self._resources[rid] = Resource.model_validate({"id": rid, "kind": kind, **fields, "params": params})
        return rid

    def declared_resources(self) -> list[Resource]:
        return [self._resources[rid] for rid in sorted(self._resources)]

    def session_resources(self, spec: SessionSpec) -> dict[ResourceRole, str]:
        resources: dict[ResourceRole, str] = {
            "lease": self.resource("lease." + spec.platform, app=spec.app, session=spec.key)}
        if needs_backend(spec):
            resources["backend"] = self.session_backend(spec)
        return resources

    def session_backend(self, spec: SessionSpec) -> str:
        return self.resource("backend", app=spec.app, definition=spec.backend, seed=spec.seed, session=spec.key)

    def update_clock(self, **fields: Any) -> Clock:
        current_fields = {name: getattr(self._clock, name) for name in Clock.model_fields}
        self._clock = Clock.model_validate({**current_fields, **fields})
        return self._clock

    @property
    def clock(self) -> Clock:
        return self._clock

    def add_persona(self, key: str) -> None:
        if key not in self._personas:
            self._personas.append(key)

    def require(self, provenance: Provenance) -> None:
        if provenance.plugin in (BUILTIN_PLUGIN, PROJECT_PLUGIN):
            return
        self._requires[provenance.plugin] = provenance.version

    def on_finalize(self, fn: Callable[[TraceContext], None], *, order: int = 0) -> None:
        self._finalizers.append(_Finalizer(order, self._finalizer_count, fn))
        self._finalizer_count += 1

    def call_site(self) -> CallSite | None:
        frames = self._user_frames()
        try:
            return self._call_site(frames[-1]) if frames else None
        finally:
            del frames

    def has_flag(self, flag: str) -> bool:
        return flag in self.scope().flags

    def set_outputs(self, value: Any) -> None:
        outputs = encode(value)
        for expr in walk_exprs(outputs):
            if isinstance(expr, ItemExpr):
                raise TraceError(
                    f"the returned value uses the item of eks.each {expr.item!r}; items exist only inside "
                    "`with eks.each(...) as item:`, so return a value recorded outside the loop"
                )
            if expr.ref not in self._nodes:
                raise TraceError(
                    f"the returned value references node {expr.ref!r}, which is not part of this plan; it was "
                    "recorded inside a literal eks.each body or another trace, so return values recorded "
                    "outside it"
                )
        self._outputs = outputs

    def finalize(self) -> Plan:
        if self._finalized:
            raise TraceError(f"the plan trace of {self._kind} {self._name!r} is already finalized")
        self._require_root("finalize")
        while self._finalizers:
            self._finalizers.sort()
            finalizer = self._finalizers.pop(0)
            finalizer.fn(self)
            self._require_root(f"finalizer {getattr(finalizer.fn, '__qualname__', finalizer.fn)!r}")
        self._finalized = True
        plan = Plan.model_validate({
            "kind": self._kind,
            "name": self._name,
            "params": encode(self._params),
            "clock": self._clock,
            "session": self._session,
            "resources": [self._resources[rid] for rid in sorted(self._resources)],
            "nodes": self._root.nodes,
            "outputs": self._outputs,
            "personas": list(self._personas),
            "requires": [Requirement(plugin=plugin, version=self._requires[plugin]) for plugin in sorted(self._requires)],
            "base": self._base_dir(),
            "tags": list(self._tags()),
            "source": self._source_info(),
        })
        try:
            link_clock(plan, self._registry)
        except PlanError as error:
            raise TraceError(str(error)) from error
        versions = {node.op: self._registry.op(node.op).executor_version
                    for node, _ancestors in plan.walk() if self._registry.has_op(node.op)}
        return hash_plan(plan, executor_versions=versions)

    @contextmanager
    def template(self) -> Iterator[Scope]:
        frames = self._user_frames()
        try:
            template_id = self._node_id(EACH_OP, None, frames)
        finally:
            del frames
        outer = self.scope()
        scope = Scope(
            key=f"{template_id}/body", container=None, branch="body", parallel=outer.parallel,
            each=template_id, flags=outer.flags | {TEMPLATE_FLAG}, template=True)
        self._scopes.append(scope)
        try:
            yield scope
        finally:
            self._pop(scope)
            outer.closed = None

    def lane_heads(self, scope: Scope) -> dict[str, str]:
        return dict(self._heads.get(scope.key, {}))

    def splice(self, nodes: Sequence[Node], *, heads: Mapping[str, str], tails: Mapping[str, str]) -> None:
        scope = self.scope()
        by_id = {node.id: node for node in nodes}
        joined: dict[str, list[str]] = {}
        for lane, head in heads.items():
            if head not in by_id:
                raise TraceError(f"lane head {head!r} is not one of the spliced nodes")
            tail = scope.tails.get(lane)
            if tail is not None:
                tails_of_head = joined.setdefault(head, [])
                if tail not in tails_of_head:
                    tails_of_head.append(tail)
            else:
                self._heads.setdefault(scope.key, {}).setdefault(lane, head)
        for head, lane_tails in joined.items():
            node = by_id[head]
            node.after = [*lane_tails, *(edge for edge in node.after if edge not in lane_tails)]
        for node in nodes:
            for inner in _walk((node,)):
                if inner.id in self._nodes:
                    raise TraceError(f"node id {inner.id!r} is recorded twice in this plan trace")
                self._nodes[inner.id] = inner
            scope.nodes.append(node)
        scope.tails.update(tails)
        scope.closed = None

    def drop_nodes(self, node_ids: Sequence[str]) -> None:
        for node_id in node_ids:
            self._nodes.pop(node_id, None)

    def _check_branch(self, spec: OpSpec, branch: str) -> None:
        if spec.branches and branch not in spec.branches:
            raise TraceError(f"{spec.name} has the branches {list(spec.branches)}, not {branch!r}")

    @contextmanager
    def _branch_scope(self, node: Node, branch: str, flags: Sequence[str]) -> Iterator[Scope]:
        mode = self._containers.get(node.id) or _ContainerMode(parallel=node.op == PARALLEL_OP,
                                                               each=node.op == EACH_OP)
        outer = self.scope()
        inherited = outer.flags | frozenset(flags) | ({PARALLEL_FLAG} if mode.parallel else frozenset())
        scope = Scope(
            key=f"{node.id}/{branch}", container=node.id, branch=branch, parallel=mode.parallel,
            tails=dict(self._branch_tails.get((node.id, branch), {})), each=node.id if mode.each else None,
            flags=inherited)
        self._scopes.append(scope)
        try:
            yield scope
        finally:
            self._pop(scope)
            if node.branches is None:
                node.branches = {}
            node.branches.setdefault(branch, []).extend(scope.nodes)
            self._branch_tails[(node.id, branch)] = dict(scope.tails)
            outer.closed = node.id
            if outer.parallel:
                self._join_body_lanes(outer, node)

    def _join_body_lanes(self, scope: Scope, node: Node) -> None:
        opened = self._opened_tails.get(node.id, {})
        lanes = self._body_lanes(node) or self._node_lanes(node.uses)
        joined: list[str] = []
        for lane in lanes:
            tail = opened.get(lane)
            if tail is not None and tail != node.id and tail not in node.after and tail not in joined:
                joined.append(tail)
        for lane in lanes:
            current = scope.tails.get(lane)
            if current is not None and current not in (opened.get(lane), node.id):
                raise TraceError(
                    f"{node.op} {node.id!r} drives lane {lane!r} in its body, but node {current!r} was recorded "
                    "on that lane after the container; reopen a container's branch before recording more nodes "
                    "on its lanes"
                )
            if current is None:
                self._heads.setdefault(scope.key, {}).setdefault(lane, node.id)
            scope.tails[lane] = node.id
        if joined:
            node.after = [*joined, *node.after]

    def _body_lanes(self, node: Node) -> list[str]:
        lanes: list[str] = []
        for children in (node.branches or {}).values():
            for child in _walk(children):
                if not self._registry.has_op(child.op):
                    continue
                child_spec = self._registry.op(child.op)
                if child_spec.ordering != "program" or (child_spec.container and not child.uses):
                    continue
                for lane in self._node_lanes(child.uses):
                    if lane not in lanes:
                        lanes.append(lane)
        return lanes

    @staticmethod
    def _node_lanes(uses: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(uses)) or [FREE_LANE]

    def _pop(self, scope: Scope) -> None:
        if self._scopes[-1] is not scope:
            open_keys = [open_scope.key for open_scope in self._scopes[1:]]
            raise TraceError(f"mismatched scope nesting: closing {scope.key!r} while {open_keys} are open")
        self._scopes.pop()

    def _require_root(self, what: str) -> None:
        if len(self._scopes) != 1:
            open_keys = [scope.key for scope in self._scopes[1:]]
            raise TraceError(f"{what}: scopes {open_keys} are still open; close every with block before finalizing")

    def _add(
        self,
        spec: OpSpec,
        args: BaseModel | Mapping[str, Any] | None,
        *,
        uses: Sequence[str] | None,
        pin: Time | None,
        label: str | None,
        out: str | None,
        id_key: str | None,
        key_suffix: str | None,
        container: bool,
    ) -> Node:
        if self._finalized:
            raise TraceError(f"cannot record {spec.name}: the plan trace is already finalized")
        for field_name, text in (("label", label), ("id_key", id_key)):
            if text is not None and (SENTINEL_OPEN in text or SENTINEL_CLOSE in text):
                raise TraceError(
                    f"{spec.name}: {field_name} must be plain text; refs are resolved only inside args, "
                    "so build the text from plan-time values"
                )
        data = self._normalize_args(spec, args)
        self._check_exprs(spec.name, data)
        used = list(uses or ())
        for rid in used:
            if rid not in self._resources:
                raise TraceError(f"{spec.name} uses resource {rid!r}, which is not declared; declare it with ctx.resource(...)")
        node_out = out or spec.out
        try:
            parse_ref_type(node_out)
        except RefTypeError as error:
            raise TraceError(f"{spec.name}: {error}") from error
        problem = out_problem(spec, data, node_out)
        if problem is not None:
            raise TraceError(problem)
        frames = self._user_frames()
        try:
            node_id = self._node_id(spec.name, id_key, frames, key_suffix)
            src = self._call_site(frames[-1]).src() if frames else None
        finally:
            del frames
        if node_id in self._nodes:
            raise TraceError(f"node id {node_id!r} is recorded twice in this plan trace")
        scope = self.scope()
        lanes = self._lanes(scope, used)
        laned = spec.ordering == "program" and not (container and scope.parallel and not used)
        node = Node(
            id=node_id, op=spec.name, v=spec.version, args=data, uses=used,
            after=self._after(scope, lanes if laned else (), data), out=node_out,
            pin=ensure_time(pin, "pin") if pin is not None else None, label=label, src=src,
            branches={} if container else None)
        scope.nodes.append(node)
        scope.closed = None
        self._nodes[node_id] = node
        if laned:
            for lane in lanes:
                if lane not in scope.tails:
                    self._heads.setdefault(scope.key, {}).setdefault(lane, node_id)
                scope.tails[lane] = node_id
        self.require(spec.provenance)
        return node

    def _normalize_args(self, spec: OpSpec, args: BaseModel | Mapping[str, Any] | None) -> dict[str, JsonValue]:
        if isinstance(args, BaseModel):
            raw: Any = args.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif args is None:
            raw = {}
        else:
            raw = dict(args)
        model = spec.args.model_validate(encode(raw))
        return canonical_dump(model)

    def _check_exprs(self, op: str, data: Mapping[str, JsonValue]) -> None:
        items = {scope.each for scope in self._scopes if scope.each is not None}
        enclosing = {scope.container for scope in self._scopes if scope.container is not None}
        for expr in walk_exprs(dict(data)):
            if isinstance(expr, RefExpr) and expr.ref not in self._nodes:
                raise TraceError(
                    f"{op} references node {expr.ref!r}, which is not recorded in this plan trace; "
                    "use refs returned by builders of the same plan"
                )
            if isinstance(expr, RefExpr) and expr.ref in enclosing:
                raise TraceError(
                    f"{op} references its enclosing container {expr.ref!r}, whose value exists only after "
                    "the body ran; use it after the with block"
                )
            if isinstance(expr, ItemExpr) and expr.item not in items:
                raise TraceError(
                    f"{op} uses the item of eks.each {expr.item!r} outside its with block; "
                    "use the item only inside `with eks.each(...) as item:`"
                )

    def _lanes(self, scope: Scope, uses: Sequence[str]) -> list[str]:
        if not scope.parallel:
            return [SINGLE_LANE]
        return self._node_lanes(uses)

    def _after(self, scope: Scope, lanes: Sequence[str], data: Mapping[str, JsonValue]) -> list[str]:
        after: list[str] = []
        for lane in lanes:
            tail = scope.tails.get(lane)
            if tail is not None and tail not in after:
                after.append(tail)
        for ref in referenced_nodes(dict(data)):
            if ref not in after:
                after.append(ref)
        return after

    def _node_id(
        self, op: str, id_key: str | None, frames: Sequence[FrameType], key_suffix: str | None = None,
    ) -> str:
        scope = self.scope()
        if id_key is not None:
            key = f"{scope.key}/{id_key}"
        else:
            chain = ">".join(self._segment(frame) for frame in frames) or "lib"
            key = f"{scope.key}/{chain}/{op}"
            if key_suffix is not None:
                key = f"{key}:{key_suffix}"
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        return derive_id("n", f"{key}#{occurrence}")

    def _user_frames(self) -> list[FrameType]:
        if self._entry_code is None:
            return []
        found: list[FrameType] = []
        frame = inspect.currentframe()
        try:
            while frame is not None:
                if not _is_library(frame):
                    found.append(frame)
                    if frame.f_code is self._entry_code:
                        found.reverse()
                        return found
                frame = frame.f_back
            return []
        finally:
            del frame

    def _segment(self, frame: FrameType) -> str:
        text, line, col = _call_text(frame)
        call = _short_hash(text) if text is not None else f"L{line}C{col}"
        return f"{self._relpath(frame.f_code.co_filename, _frame_module(frame))}:{frame.f_code.co_qualname}:{call}"

    def _call_site(self, frame: FrameType) -> CallSite:
        text, _line, col = _call_text(frame)
        line = frame.f_lineno or frame.f_code.co_firstlineno
        return CallSite(
            file=self._relpath(frame.f_code.co_filename, _frame_module(frame)), line=line, col=col,
            qualname=frame.f_code.co_qualname, text=text or "")

    def _anchor_dir(self) -> PurePath | None:
        if self._tree is not None:
            return PurePath(self._tree)
        if self._entry is not None and self._entry.file:
            return _real_path(self._entry.file).parent
        return None

    def _relpath(self, file: str, module: str | None = None) -> str:
        path = _real_path(file)
        if not path.is_absolute():
            return path.as_posix()
        anchor = self._anchor_dir()
        if anchor is None:
            return path.name
        if self._tree is not None and not path.is_relative_to(anchor):
            owner = _owned_by(path, module)
            if owner is None:
                raise TraceError(
                    f"{path.as_posix()} is outside the tree {anchor.as_posix()} and belongs to no loaded plugin or "
                    "importable package; node ids, src and media paths of such code would depend on where the tree "
                    "sits, so move the code into the tree or into a plugin"
                )
            scheme, root = owner
            return f"{scheme}/{path.relative_to(root).as_posix()}"
        try:
            return path.relative_to(anchor, walk_up=True).as_posix()
        except ValueError:
            return path.as_posix()

    def _base_dir(self) -> str:
        if self._tree is None or self._entry is None or not self._entry.file:
            return "."
        written = self._relpath(self._entry.file, self._entry.module)
        if written.startswith(OWNED_SCHEMES):
            return "."
        parent = PurePath(written).parent.as_posix()
        return parent or "."

    def media_path(self, path: str, origin: str | None = None) -> str:
        uri = self.media_uri(path, origin)
        return uri.removeprefix(FILE_SCHEME) if uri.startswith(FILE_SCHEME) else uri

    def media_uri(self, path: str, origin: str | None = None) -> str:
        if posixpath.isabs(path) or _DRIVE_PATH.match(path):
            return FILE_SCHEME + path
        author, module = (origin, None) if origin is not None else self._author()
        if author is None and self._entry is not None and self._entry.file:
            author, module = self._entry.file, self._entry.module
        if author is None:
            return FILE_SCHEME + posixpath.normpath(path)
        written = self._relpath(author, module)
        if written.startswith(OWNED_SCHEMES):
            scheme, _slash, inner = written.partition("/")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(inner) or ".", path))
            if resolved == ".." or resolved.startswith("../"):
                raise TraceError(
                    f"media path {path!r} written in {written} leaves {scheme}; reference files inside it, or pass "
                    "an artifact: or https: URI"
                )
            return f"{scheme}/{resolved}"
        directory = PurePath(written).parent.as_posix() or "."
        if posixpath.isabs(directory) or _DRIVE_PATH.match(directory):
            return FILE_SCHEME + posixpath.normpath(posixpath.join(directory, path))
        resolved = posixpath.normpath(posixpath.join(directory, path))
        if self._tree is not None and (resolved == ".." or resolved.startswith("../")):
            raise TraceError(
                f"media path {path!r} written in {written} resolves to {resolved}, outside the tree "
                f"{self._tree.as_posix()}; plans address files inside the tree, so move the file into it or pass an "
                "artifact: or https: URI"
            )
        return FILE_SCHEME + posixpath.relpath(resolved, self._base_dir())

    def _author(self) -> tuple[str | None, str | None]:
        frames = self._user_frames()
        try:
            if not frames:
                return None, None
            return frames[-1].f_code.co_filename, _frame_module(frames[-1])
        finally:
            del frames

    def _source_info(self) -> SourceInfo | None:
        if self._entry is None:
            return None
        file = self._relpath(self._entry.file, self._entry.module) if self._entry.file else self._entry.module
        return SourceInfo(file=file, qualname=self._entry.qualname, line=self._entry.line)

    def _tags(self) -> tuple[str, ...]:
        if self._entry is None:
            return ()
        tags: Sequence[Any] = getattr(self._entry.options, "tags", ())
        return tuple(str(tag) for tag in tags)


def needs_backend(spec: SessionSpec) -> bool:
    return spec.seed is not None or spec.backend is not None or spec.persona is not None


def _frame_module(frame: FrameType) -> str | None:
    name = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else None


class SessionCore:
    def __init__(self, ctx: TraceContext, spec: SessionSpec) -> None:
        self._ctx = ctx
        self._spec = spec
        self._resources: dict[ResourceRole, str] = ctx.session_resources(spec)

    @property
    def ctx(self) -> TraceContext:
        return self._ctx

    @property
    def spec(self) -> SessionSpec:
        return self._spec

    @property
    def platform(self) -> str:
        return self._spec.platform

    @property
    def app(self) -> str:
        return self._spec.app

    @property
    def resources(self) -> Mapping[ResourceRole, str]:
        return dict(self._resources)

    def record_op(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        at: Time | None = None,
        label: str | None = None,
        id_key: str | None = None,
        key_suffix: str | None = None,
    ) -> Ref[Any]:
        spec = self._ctx.registry.op(op)
        self._check_pin(spec, at)
        return self._ctx.record(op, args, uses=self._uses(spec), pin=at, label=label, id_key=id_key,
                                key_suffix=key_suffix)

    @contextmanager
    def record_container(
        self,
        op: str,
        args: BaseModel | Mapping[str, Any] | None = None,
        *,
        branch: str = "body",
        at: Time | None = None,
        label: str | None = None,
        flags: Sequence[str] = (),
    ) -> Iterator[Ref[Any]]:
        spec = self._ctx.registry.op(op)
        self._check_pin(spec, at)
        with self._ctx.container(op, args, branch=branch, uses=self._uses(spec), pin=at, label=label,
                                 flags=flags) as ref:
            yield ref

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _SESSIONS.set((*_SESSIONS.get(), self))
        try:
            yield
        finally:
            _SESSIONS.reset(token)

    def _uses(self, spec: OpSpec) -> list[str]:
        if "backend" in spec.resources and "backend" not in self._resources:
            self._resources["backend"] = self._ctx.session_backend(self._spec)
        return [self._resources[role] for role in spec.resources]

    def _check_pin(self, spec: OpSpec, at: Time | None) -> None:
        if at is None:
            return
        if not spec.pinnable:
            raise TraceError(f"{spec.name} cannot be pinned with at=; only pinnable ops take a time")
        if self._ctx.in_runtime_each():
            raise TraceError(
                f"{spec.name} is pinned at {format_time(at)!r} inside a runtime eks.each body; every iteration "
                "would land on the same time, which no take can honour for two or more items. Pin a step "
                "outside the loop, or unroll it with a Python for loop over a literal list so each step gets "
                "its own at="
            )


class Bound:
    __slots__ = ("session",)

    def __init__(self, session: SessionCore) -> None:
        self.session = session


_CURRENT: ContextVar[TraceContext | None] = ContextVar("eks_trace", default=None)
_SESSIONS: ContextVar[tuple[SessionCore, ...]] = ContextVar("eks_sessions", default=())


def current() -> TraceContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise TraceError("plan builders work only inside eks.plan(...); build the plan with eks.plan(entry, ...)")
    return ctx


def active() -> TraceContext | None:
    return _CURRENT.get()


@contextmanager
def tracing(ctx: TraceContext) -> Iterator[TraceContext]:
    token = _CURRENT.set(ctx)
    sessions = _SESSIONS.set(())
    try:
        with sentinel_scope(ctx.sentinels):
            yield ctx
    finally:
        _SESSIONS.reset(sessions)
        _CURRENT.reset(token)


def current_session() -> SessionCore:
    sessions = _SESSIONS.get()
    if not sessions:
        raise TraceError("no active session; use this builder inside a flow or inside `with take.flow() as s:`")
    return sessions[-1]


def active_session() -> SessionCore | None:
    sessions = _SESSIONS.get()
    return sessions[-1] if sessions else None


def record(
    op: str,
    args: BaseModel | Mapping[str, Any] | None = None,
    *,
    uses: Sequence[str] | None = None,
    pin: Time | None = None,
    label: str | None = None,
    out: str | None = None,
    id_key: str | None = None,
    key_suffix: str | None = None,
) -> Ref[Any]:
    return current().record(op, args, uses=uses, pin=pin, label=label, out=out, id_key=id_key,
                            key_suffix=key_suffix)


def user_frame(frame: FrameType | None) -> FrameType | None:
    while frame is not None and _is_library(frame):
        frame = frame.f_back
    return frame


def capture_function(
    fn: Callable[..., Any],
    *,
    allowed_globals: Sequence[str] = (),
    allowed_builtins: Collection[str] = SANDBOX_BUILTINS,
) -> FunctionSource:
    name = getattr(fn, "__name__", "")
    code = getattr(fn, "__code__", None)
    if not isinstance(code, CodeType) or name == "<lambda>":
        raise TraceError(f"{fn!r} must be a function defined with def so its source can be captured")
    source = _function_source(fn, name)
    _check_imports(code, name, allowed_globals)
    scan = _sandbox_scan(source, name)
    freevars = _closure_bindings(fn, code, name)
    module_globals: dict[str, Any] = getattr(fn, "__globals__", {})
    builtin_names = vars(builtins)
    closure_names = set(code.co_freevars)
    for builtin_name in sorted(scan.outer | scan.shadowing):
        builtin = builtin_name not in module_globals and builtin_name not in closure_names
        if builtin and builtin_name in builtin_names and builtin_name not in allowed_builtins:
            raise _denied_builtin(name, builtin_name, allowed_builtins)
    used_globals: list[str] = []
    for global_name in sorted((_global_names(code) | scan.body) - closure_names):
        if global_name not in module_globals and global_name in builtin_names:
            if global_name not in allowed_builtins:
                raise _denied_builtin(name, global_name, allowed_builtins)
            used_globals.append(global_name)
            continue
        if global_name in allowed_globals:
            used_globals.append(global_name)
        elif global_name in module_globals and _is_json_literal(module_globals[global_name]):
            if global_name in freevars:
                raise TraceError(f"{name}: {global_name!r} is both a closure variable and a global")
            freevars[global_name] = Binding(kind="literal", value=encode(module_globals[global_name]))
        else:
            raise TraceError(
                f"{name} uses the global {global_name!r}, which a dynamic function cannot carry; "
                f"pass it as a parameter default or use only {sorted(allowed_globals)}"
            )
    _check_leaks(source, name, freevars, _importable(allowed_globals))
    params = tuple(inspect.signature(fn).parameters)
    return FunctionSource(
        name=name, source=source, hash=digest(source.encode("utf-8")), params=params, freevars=freevars,
        globals=tuple(used_globals))


def _denied_builtin(name: str, builtin: str, allowed_builtins: Collection[str]) -> TraceError:
    return TraceError(
        f"{name} uses the builtin {builtin!r}, which the sandbox does not provide because it is "
        f"not deterministic or has side effects; the allowed builtins are {sorted(allowed_builtins)}"
    )


def _sandbox_scan(source: str, name: str) -> SandboxScan:
    try:
        scan = scan_sandbox_source(source)
    except (SyntaxError, ValueError) as error:
        raise TraceError(f"cannot parse the source of {name}: {error}") from error
    for use in scan.escapes:
        raise TraceError(
            f"{name} reaches {use.name!r} on line {use.line} of its source, which can leave the sandbox; dunder "
            "names, private (_name) and internal (ctx) attributes, the frame, generator, coroutine and traceback "
            "attributes, the model parsing and construction API (parse_file, parse_raw, from_orm, model_rebuild "
            "and the like), eks members other than the sandbox ones and .format on anything but a string literal "
            "are not allowed, so pass what it needs as a parameter default"
        )
    return scan


def _check_leaks(source: str, name: str, freevars: Mapping[str, Binding], modules: Sequence[str]) -> None:
    data_names = [var for var, binding in freevars.items() if binding.kind in DATA_BINDING_KINDS]
    for use in sandbox_leaks(source, data_names=data_names, data_modules=modules, params_are_data=not modules):
        raise TraceError(
            f"{name} passes a value that may hold an object address to {use.name} on line {use.line} of its "
            "source; generators, iterators (map, filter, zip, enumerate, reversed), functions, methods, classes "
            "and sandbox objects print their memory address and hash by identity, so the result would change "
            "between runs. Format or hash only data (parameters, JSON literals, numbers and strings) and "
            "materialize iterators with list(...) first"
        )


def _importable(allowed_globals: Sequence[str]) -> list[str]:
    return sorted(module for module in allowed_globals if module not in ALLOWED_DYNAMIC_GLOBALS)


def _function_source(fn: Callable[..., Any], name: str) -> str:
    try:
        raw = inspect.getsource(fn)
    except (OSError, TypeError) as error:
        raise TraceError(f"cannot read the source of {name}: {error}") from error
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    try:
        verbatim = _string_continuation_lines(text)
    except (tokenize.TokenError, SyntaxError) as error:
        raise TraceError(f"cannot tokenize the source of {name}: {error}") from error
    lines = text.split("\n")
    prefix = lines[0][:len(lines[0]) - len(lines[0].lstrip(" \t"))]
    lines = [line[len(prefix):] if number not in verbatim and line.startswith(prefix) else line
             for number, line in enumerate(lines, start=1)]
    try:
        module = ast.parse("\n".join(lines))
    except SyntaxError as error:
        raise TraceError(f"cannot parse the source of {name}: {error}") from error
    first = module.body[0] if module.body else None
    if not isinstance(first, ast.FunctionDef | ast.AsyncFunctionDef):
        raise TraceError(f"{name} must be a function defined with def so its source can be captured")
    return "\n".join(lines[first.lineno - 1:]).rstrip("\n") + "\n"


def _string_continuation_lines(text: str) -> set[int]:
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        start, end = token.start[0], token.end[0]
        if end > start and token.type not in (tokenize.NL, tokenize.NEWLINE):
            lines.update(range(start + 1, end + 1))
    return lines


def _closure_bindings(fn: Callable[..., Any], code: CodeType, name: str) -> dict[str, Binding]:
    closure: tuple[Any, ...] = getattr(fn, "__closure__", None) or ()
    bindings: dict[str, Binding] = {}
    for var, cell in zip(code.co_freevars, closure, strict=True):
        try:
            value = cell.cell_contents
        except ValueError as error:
            raise TraceError(f"{name}: closure variable {var!r} is not assigned yet") from error
        bindings[var] = _binding(var, value, name)
    return bindings


def _binding(var: str, value: Any, name: str) -> Binding:
    from eks.assertions import expect
    from eks_harness.ir.target import by
    from eks_harness.ir.time import t

    if isinstance(value, SessionCore):
        return Binding(kind="session", value=value.spec.key)
    if isinstance(value, Bound):
        attribute = next((key for key, item in vars(value.session).items() if item is value), None)
        if attribute is None:
            raise TraceError(f"{name}: closure variable {var!r} is a namespace that is not attached to its session")
        return Binding(kind="namespace", value=f"{value.session.spec.key}.{attribute}")
    factories: dict[int, str] = {id(by): "by", id(t): "t", id(expect): "expect"}
    if id(value) in factories:
        return Binding(kind="factory", value=factories[id(value)])
    if isinstance(value, Time):
        return Binding(kind="time", value=format_time(value))
    if isinstance(value, Target):
        return Binding(kind="target", value=encode(value))
    if _is_json_literal(value):
        return Binding(kind="literal", value=encode(value))
    raise TraceError(
        f"{name}: closure variable {var!r} holds a {type(value).__name__}, which a dynamic function cannot carry; "
        "pass refs and other values as parameter defaults"
    )


def _is_json_literal(value: Any) -> bool:
    if value is None or isinstance(value, bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return SENTINEL_OPEN not in value and SENTINEL_CLOSE not in value
    if isinstance(value, list | tuple):
        return all(_is_json_literal(item) for item in cast("Sequence[Any]", value))
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        return all(isinstance(key, str) and _is_json_literal(item) for key, item in mapping.items())
    return False


def _check_imports(code: CodeType, name: str, allowed_globals: Sequence[str]) -> None:
    importable = _importable(allowed_globals)
    for module in sorted(_imported_modules(code)):
        if module.split(".")[0] not in importable:
            shown = repr(module) if module.strip(".") else "a relative module"
            allowed = f"only {importable}" if importable else "nothing"
            raise TraceError(
                f"{name} imports {shown}, which the sandbox does not provide; it may import {allowed}, so pass "
                "what it needs as a parameter default"
            )


def _imported_modules(code: CodeType) -> set[str]:
    modules: set[str] = set()
    instructions = list(dis.get_instructions(code))
    for position, instruction in enumerate(instructions):
        if instruction.opname != _IMPORT_OP or not isinstance(instruction.argval, str):
            continue
        level = instructions[position - 2].argval if position >= 2 else 0
        dots = "." * level if isinstance(level, int) and not isinstance(level, bool) else ""
        modules.add(dots + instruction.argval)
    for const in code.co_consts:
        if isinstance(const, CodeType):
            modules |= _imported_modules(const)
    return modules


def _global_names(code: CodeType) -> set[str]:
    names: set[str] = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS and isinstance(instruction.argval, str):
            names.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, CodeType):
            names |= _global_names(const)
    return names


__all__ = [
    "EACH_OP",
    "LIBRARY_PACKAGES",
    "PARALLEL_FLAG",
    "ROOT_SCOPE",
    "TEMPLATE_FLAG",
    "TRANSPARENT_MODULES",
    "Bound",
    "CallSite",
    "Scope",
    "SessionCore",
    "TraceContext",
    "TraceError",
    "active",
    "active_session",
    "capture_function",
    "current",
    "current_session",
    "normalize_call_text",
    "record",
    "tracing",
    "user_frame",
]
