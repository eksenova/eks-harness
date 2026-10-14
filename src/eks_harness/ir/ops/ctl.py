from __future__ import annotations

import ast
import builtins
import re
import string
import symtable
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from eks_harness.ir.plan import FunctionSource
from eks_harness.ir.refs import RCond, RJson, RList
from eks_harness.ir.registry import Args, op

SANDBOX_BUILTINS: tuple[str, ...] = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "frozenset", "int", "isinstance",
    "len", "list", "map", "max", "min", "pow", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple",
    "zip",
)
ALLOWED_DYNAMIC_GLOBALS: tuple[str, ...] = ("eks", "by", "t", "expect", *SANDBOX_BUILTINS)
SANDBOX_EKS_ATTRIBUTES: tuple[str, ...] = (
    "Span", "Target", "Time", "TimeSet", "annotate", "by", "checks", "each", "expect", "otherwise", "overlay",
    "parallel", "t", "when",
)
SANDBOX_EKS_NAMESPACES: Mapping[str, tuple[str, ...]] = {
    "checks": ("to_use", "use"),
    "overlay": ("annotate", "arrow", "blur", "callout", "caption", "custom", "highlight", "icon", "image", "redact",
                "spotlight", "step", "title"),
}
BPY_MODULES: tuple[str, ...] = ("math", "mathutils")
INTROSPECTION_ATTRIBUTES: frozenset[str] = frozenset({
    "ag_await", "ag_code", "ag_frame", "cr_await", "cr_code", "cr_frame", "cr_origin", "f_back", "f_builtins",
    "f_code", "f_globals", "f_locals", "f_trace", "gi_code", "gi_frame", "gi_yieldfrom", "tb_frame", "tb_next",
})
INTERNAL_ATTRIBUTES: frozenset[str] = frozenset({"ctx"})
HOST_ATTRIBUTES: frozenset[str] = frozenset({
    "construct", "from_orm", "model_construct", "model_rebuild", "parse_file", "parse_obj", "parse_raw",
    "update_forward_refs",
})
DATA_BINDING_KINDS: tuple[str, ...] = ("literal", "target", "time")
FORMAT_ATTRIBUTES: frozenset[str] = frozenset({"format", "format_map"})
_BUILTIN_NAMES = frozenset(vars(builtins))
_FIELD_ATTRIBUTE = re.compile(r"\.([^.\[]+)")
_SANDBOX_EKS = "eks"
_STR = "str"
_SCALAR_BUILTINS = frozenset({"all", "any", "bool", "float", "int", "isinstance", "len", "range", "str"})
_OPERATOR_BUILTINS = frozenset({"abs", "divmod", "pow", "round"})
_BOOLEAN_COMPARISONS = (ast.Is, ast.IsNot, ast.In, ast.NotIn)
_COLLECTING_BUILTINS = frozenset({"frozenset", "list", "set", "sorted", "tuple"})
_ITERATOR_BUILTINS = frozenset({"enumerate", "filter", "map", "reversed", "zip"})
_HASHING_BUILTINS = frozenset({"frozenset", "set"})
_KEYED_BUILTINS = frozenset({"max", "min", "sorted"})
_MAPPING_BUILTINS = frozenset({"filter", "map"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
type _Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
type _Kind = Literal["expr", "elems", "data", "opaque", "module", "param"]


@dataclass(frozen=True, slots=True)
class SandboxUse:
    line: int
    name: str
    column: int = 0


@dataclass(frozen=True, slots=True)
class SandboxScan:
    body: frozenset[str]
    outer: frozenset[str]
    shadowing: frozenset[str]
    imports: tuple[SandboxUse, ...]
    escapes: tuple[SandboxUse, ...]

    def builtins(self, provided: Collection[str] = ()) -> tuple[str, ...]:
        names = self.body | self.outer | self.shadowing
        return tuple(sorted(name for name in names if name in _BUILTIN_NAMES and name not in provided))

    def denied_builtins(self, provided: Collection[str] = ()) -> tuple[str, ...]:
        return tuple(name for name in self.builtins(provided) if name not in SANDBOX_BUILTINS)


def blocked_attribute(name: str) -> bool:
    return (name.startswith("_") or name in INTROSPECTION_ATTRIBUTES or name in INTERNAL_ATTRIBUTES
            or name in HOST_ATTRIBUTES)


def scan_sandbox_source(source: str) -> SandboxScan:
    tree = ast.parse(source)
    evaluated = _LocalAnnotations().visit(ast.parse(source))
    top = symtable.symtable(ast.unparse(evaluated), "<sandbox>", "exec")
    outer = frozenset(symbol.get_name() for symbol in top.get_symbols() if symbol.is_referenced())
    body: set[str] = set()
    shadowing: set[str] = set()
    for table in _nested_tables(top):
        for symbol in table.get_symbols():
            if symbol.is_global():
                body.add(symbol.get_name())
            elif isinstance(table, symtable.Class) and symbol.is_local() and symbol.is_referenced():
                shadowing.add(symbol.get_name())
    imports: list[SandboxUse] = []
    escapes: list[SandboxUse] = []
    parents = _parents(tree)
    for found in ast.walk(tree):
        line = getattr(found, "lineno", 0)
        column = getattr(found, "end_col_offset", None) or 0
        if isinstance(found, ast.Import):
            imports.extend(SandboxUse(line, alias.name, column) for alias in found.names)
        elif isinstance(found, ast.ImportFrom):
            imports.append(SandboxUse(line, "." * found.level + (found.module or ""), column))
        elif isinstance(found, ast.Name) and found.id.startswith("__") and found.id not in _BUILTIN_NAMES:
            escapes.append(SandboxUse(line, found.id, column))
        elif isinstance(found, ast.Name) and found.id == _SANDBOX_EKS:
            chain = _eks_chain(found, parents)
            if chain is not None:
                escapes.append(SandboxUse(line, chain, column))
        elif isinstance(found, ast.Attribute) and _escaping_attribute(found, parents):
            escapes.append(SandboxUse(line, found.attr, column))
        elif isinstance(found, ast.MatchClass):
            escapes.extend(SandboxUse(line, name, column) for name in found.kwd_attrs if blocked_attribute(name))
        elif isinstance(found, ast.Constant) and isinstance(found.value, str):
            escapes.extend(SandboxUse(line, name, column) for name in _format_attributes(found.value)
                           if blocked_attribute(name))
    return SandboxScan(body=frozenset(body), outer=outer, shadowing=frozenset(shadowing),
                       imports=tuple(sorted(imports, key=_position)), escapes=tuple(sorted(escapes, key=_position)))


def _position(use: SandboxUse) -> tuple[int, int]:
    return use.line, use.column


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _eks_chain(name: ast.Name, parents: Mapping[ast.AST, ast.AST]) -> str | None:
    first = parents.get(name)
    if not isinstance(first, ast.Attribute) or first.value is not name:
        return name.id
    chain = f"{name.id}.{first.attr}"
    if first.attr not in SANDBOX_EKS_ATTRIBUTES:
        return chain
    members = SANDBOX_EKS_NAMESPACES.get(first.attr)
    if members is None:
        return None
    second = parents.get(first)
    if not isinstance(second, ast.Attribute) or second.value is not first:
        return chain
    return None if second.attr in members else f"{chain}.{second.attr}"


def _escaping_attribute(attribute: ast.Attribute, parents: Mapping[ast.AST, ast.AST]) -> bool:
    if blocked_attribute(attribute.attr):
        return True
    if attribute.attr not in FORMAT_ATTRIBUTES:
        return False
    call = parents.get(attribute)
    receiver = attribute.value
    return not (isinstance(call, ast.Call) and call.func is attribute and isinstance(receiver, ast.Constant)
                and isinstance(receiver.value, str))


class _LocalAnnotations(ast.NodeTransformer):
    def __init__(self) -> None:
        self.in_function = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self.scoped(node, inside=True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self.scoped(node, inside=True)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self.scoped(node, inside=False)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if self.in_function:
            node.annotation = ast.copy_location(ast.Constant(value=None), node.annotation)
        return self.generic_visit(node)

    def scoped(self, node: ast.AST, *, inside: bool) -> ast.AST:
        outer, self.in_function = self.in_function, inside
        try:
            return self.generic_visit(node)
        finally:
            self.in_function = outer


def _nested_tables(table: symtable.SymbolTable) -> Iterator[symtable.SymbolTable]:
    for child in table.get_children():
        yield child
        yield from _nested_tables(child)


def _format_attributes(text: str) -> list[str]:
    try:
        fields = list(string.Formatter().parse(text))
    except ValueError:
        return []
    names: list[str] = []
    for _literal, field, spec, _conversion in fields:
        if field:
            names.extend(_FIELD_ATTRIBUTE.findall(field))
        if spec:
            names.extend(_format_attributes(spec))
    return names


@dataclass(frozen=True, slots=True)
class _Source:
    kind: _Kind
    node: ast.AST | None = None
    param: str = ""


_OPAQUE = _Source("opaque")
_MODULE = _Source("module")


def sandbox_leaks(source: str, *, data_names: Collection[str] = (), data_modules: Collection[str] = (),
                  params_are_data: bool = True) -> tuple[SandboxUse, ...]:
    return _Flow(ast.parse(source), frozenset(data_names), frozenset(data_modules), params_are_data).leaks()


def _own_nodes(function: _Function) -> Iterator[ast.AST]:
    stack: list[ast.AST] = [function.body] if isinstance(function, ast.Lambda) else list(function.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _boolean_comparison(node: ast.Compare) -> bool:
    return all(isinstance(operator, _BOOLEAN_COMPARISONS) for operator in node.ops)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript | ast.Call | ast.Starred):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


class _Flow:
    def __init__(self, tree: ast.Module, data_names: frozenset[str], data_modules: frozenset[str],
                 params_are_data: bool) -> None:
        self.tree = tree
        self.data_names = data_names
        self.data_modules = data_modules
        self.params_are_data = params_are_data
        self.parents = _parents(tree)
        self.outer = next((node for node in tree.body if isinstance(node, _FUNCTIONS)), None)
        self.skipped = self.def_time_nodes()
        self.sources: dict[str, list[_Source]] = {}
        self.functions: dict[str, list[_Function]] = {}
        self.contexts: dict[_Function, list[dict[str, list[_Source]]]] = {}
        self.escaped: set[_Function] = set()
        self.pending_links: list[tuple[str, ast.AST]] = []
        self.pending_mutations: list[tuple[str, _Source]] = []
        self.collect()
        self.group: dict[str, str] = {}
        self.mutations = [(root, source) for root, source in self.pending_mutations if self.local(root)]
        for name, value in self.pending_links:
            for other in self.alias_names(value):
                if self.local(name) and self.local(other):
                    self.union(name, other)
        self.data: set[str] = set(self.sources)
        self.elements: set[str] = set(self.sources)
        self.returning: list[_Function] = []
        self.solve()

    def def_time_nodes(self) -> set[ast.AST]:
        outer = self.outer
        if outer is None:
            return set()
        roots: list[ast.AST] = [*outer.decorator_list, *outer.args.defaults]
        roots.extend(default for default in outer.args.kw_defaults if default is not None)
        arguments = [*outer.args.posonlyargs, *outer.args.args, *outer.args.kwonlyargs]
        arguments.extend(argument for argument in (outer.args.vararg, outer.args.kwarg) if argument is not None)
        roots.extend(argument.annotation for argument in arguments if argument.annotation is not None)
        if outer.returns is not None:
            roots.append(outer.returns)
        roots.extend(outer.type_params)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
                roots.append(node.annotation)
            elif isinstance(node, _FUNCTIONS) and node.returns is not None:
                roots.append(node.returns)
        return {node for root in roots for node in ast.walk(root)}

    def local(self, name: str) -> bool:
        return name in self.sources and not self.is_module(name)

    def find(self, name: str) -> str:
        while self.group.get(name, name) != name:
            name = self.group[name]
        return name

    def union(self, first: str, second: str) -> None:
        self.group[self.find(first)] = self.find(second)

    def alias_names(self, node: ast.AST) -> Iterator[str]:
        if isinstance(node, ast.Name):
            yield node.id
            return
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Compare) and _boolean_comparison(node)):
            return
        if isinstance(node, ast.Call) and self.builtin(node.func) in _SCALAR_BUILTINS:
            return
        for child in ast.iter_child_nodes(node):
            yield from self.alias_names(child)

    def bind(self, name: str, source: _Source) -> None:
        self.sources.setdefault(name, []).append(source)
        if source.kind in ("expr", "elems") and source.node is not None:
            self.pending_links.append((name, source.node))

    def mutate(self, root: str | None, source: _Source) -> None:
        if root is None:
            return
        self.pending_mutations.append((root, source))
        if source.node is not None:
            self.pending_links.append((root, source.node))

    def collect(self) -> None:
        for node in ast.walk(self.tree):
            if node in self.skipped:
                continue
            if isinstance(node, _FUNCTIONS):
                self.define(node)
            elif isinstance(node, ast.Lambda):
                self.parameters(node)
            elif isinstance(node, ast.ClassDef):
                self.define_class(node)
            elif isinstance(node, ast.Match):
                self.match(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self.assign(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self.assign(node.target, node.value)
            elif isinstance(node, ast.AugAssign):
                self.augment(node)
            elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                self.target(node.target, node.iter, direct=False)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if item.optional_vars is not None:
                        self.opaque_names(item.optional_vars)
            elif isinstance(node, ast.NamedExpr):
                self.bind(node.target.id, _Source("expr", node.value))
            elif isinstance(node, ast.Import | ast.ImportFrom):
                self.imported(node)
            elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name is not None:
                self.bind(node.name, _OPAQUE)
            elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                self.bind(node.rest, _OPAQUE)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and node not in self.skipped:
                self.call(node)
        self.mark_escapes()

    def define(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node is self.outer:
            source = _Source("data") if self.params_are_data else _OPAQUE
            arguments = node.args
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg,
                             arguments.kwarg):
                if argument is not None:
                    self.bind(argument.arg, source)
            return
        self.bind(node.name, _OPAQUE)
        self.functions.setdefault(node.name, []).append(node)
        if node.decorator_list:
            self.escaped.add(node)
        self.parameters(node)
        for found in _own_nodes(node):
            if isinstance(found, ast.Return | ast.Yield | ast.YieldFrom) and found.value is not None:
                self.pending_links.append((node.name, found.value))

    def define_class(self, node: ast.ClassDef) -> None:
        self.bind(node.name, _OPAQUE)
        anchor = ast.Name(id=node.name, ctx=ast.Load())
        for found in ast.walk(node):
            if isinstance(found, ast.Name) and isinstance(found.ctx, ast.Store):
                self.pending_links.append((found.id, anchor))
            elif isinstance(found, ast.arg):
                self.pending_links.append((found.arg, anchor))
            elif isinstance(found, _SCOPES) and not isinstance(found, ast.Lambda) and found is not node:
                self.pending_links.append((found.name, anchor))

    def match(self, node: ast.Match) -> None:
        for case in node.cases:
            for found in ast.walk(case.pattern):
                name = found.rest if isinstance(found, ast.MatchMapping) else getattr(found, "name", None)
                if isinstance(name, str):
                    self.pending_links.append((name, node.subject))

    def parameters(self, function: _Function) -> None:
        arguments = function.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            self.bind(argument.arg, _Source("param", function, argument.arg))
        for argument in (arguments.vararg, arguments.kwarg):
            if argument is not None:
                self.bind(argument.arg, _OPAQUE)
        positional = self.positional(function)
        defaults = list(zip(positional[len(positional) - len(arguments.defaults):], arguments.defaults, strict=True))
        defaults.extend((argument, default) for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults,
                                                                         strict=True) if default is not None)
        for argument, default in defaults:
            self.pending_links.append((argument.arg, default))

    def add_context(self, function: _Function, context: dict[str, list[_Source]]) -> None:
        self.contexts.setdefault(function, []).append(context)
        for name, sources in context.items():
            for source in sources:
                if source.node is not None:
                    self.pending_links.append((name, source.node))

    def assign(self, target: ast.expr, value: ast.expr) -> None:
        self.target(target, value, direct=True)
        if isinstance(value, ast.Lambda) and isinstance(target, ast.Name):
            self.functions.setdefault(target.id, []).append(value)

    def augment(self, node: ast.AugAssign) -> None:
        source = _Source("expr", node.value)
        if isinstance(node.target, ast.Name):
            self.bind(node.target.id, source)
        self.mutate(_root_name(node.target), source)

    def target(self, target: ast.expr, value: ast.expr, *, direct: bool) -> None:
        source = _Source("expr" if direct else "elems", value)
        if isinstance(target, ast.Name):
            self.bind(target.id, source)
        elif isinstance(target, ast.Starred):
            self.target(target.value, value, direct=direct)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self.target(element, value, direct=False)
        else:
            self.mutate(_root_name(target), source)

    def opaque_names(self, target: ast.AST) -> None:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                self.bind(node.id, _OPAQUE)

    def imported(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            module = alias.name if isinstance(node, ast.Import) else (node.module or "")
            relative = isinstance(node, ast.ImportFrom) and node.level > 0
            known = not relative and module.split(".")[0] in self.data_modules
            self.bind(alias.asname or alias.name.split(".")[0], _MODULE if known else _OPAQUE)

    def call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            root = _root_name(func.value)
            for argument in node.args:
                self.mutate(root, self.argument_source(argument))
            for keyword in node.keywords:
                self.mutate(root, _Source("expr", keyword.value))
        for function in self.callables(func):
            self.add_context(function, self.arguments(function, node))
        builtin = self.builtin(func)
        if builtin in _MAPPING_BUILTINS and node.args:
            iterables = node.args[1:] if builtin == "map" else node.args[1:2]
            for function in self.callables(node.args[0]):
                self.add_context(function, self.element_context(function, iterables))
        if builtin in _KEYED_BUILTINS:
            for keyword in node.keywords:
                if keyword.arg == "key":
                    for function in self.callables(keyword.value):
                        self.add_context(function, self.key_context(function, node.args))

    @staticmethod
    def argument_source(argument: ast.expr) -> _Source:
        if isinstance(argument, ast.Starred):
            return _Source("elems", argument.value)
        return _Source("expr", argument)

    def callables(self, node: ast.AST) -> list[_Function]:
        if isinstance(node, ast.Lambda):
            return [node]
        if isinstance(node, ast.Name):
            functions = self.functions.get(node.id, [])
            if functions and len(functions) == len(self.sources.get(node.id, ())):
                return functions
        return []

    def builtin(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in _BUILTIN_NAMES and node.id not in self.sources:
            return node.id
        return None

    @staticmethod
    def positional(function: _Function) -> list[ast.arg]:
        return [*function.args.posonlyargs, *function.args.args]

    def arguments(self, function: _Function, call: ast.Call) -> dict[str, list[_Source]]:
        positional = self.positional(function)
        names = [argument.arg for argument in (*positional, *function.args.kwonlyargs)]
        if any(isinstance(argument, ast.Starred) for argument in call.args) or any(
                keyword.arg is None for keyword in call.keywords):
            return {name: [_OPAQUE] for name in names}
        bound: dict[str, list[_Source]] = {}
        for argument, parameter in zip(call.args, positional, strict=False):
            bound[parameter.arg] = [_Source("expr", argument)]
        for keyword in call.keywords:
            if keyword.arg is not None:
                bound[keyword.arg] = [_Source("expr", keyword.value)]
        defaults = function.args.defaults
        for parameter, default in zip(positional[len(positional) - len(defaults):], defaults, strict=True):
            bound.setdefault(parameter.arg, [_Source("expr", default)])
        for parameter, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
            if default is not None:
                bound.setdefault(parameter.arg, [_Source("expr", default)])
        return bound

    def element_context(self, function: _Function, iterables: list[ast.expr]) -> dict[str, list[_Source]]:
        positional = self.positional(function)
        if any(isinstance(iterable, ast.Starred) for iterable in iterables):
            return {}
        return {parameter.arg: [_Source("elems", iterable)]
                for parameter, iterable in zip(positional, iterables, strict=False)}

    def key_context(self, function: _Function, arguments: list[ast.expr]) -> dict[str, list[_Source]]:
        positional = self.positional(function)
        if not positional or any(isinstance(argument, ast.Starred) for argument in arguments):
            return {}
        if len(arguments) == 1:
            return {positional[0].arg: [_Source("elems", arguments[0])]}
        return {positional[0].arg: [_Source("expr", argument) for argument in arguments]}

    def mark_escapes(self) -> None:
        for node in ast.walk(self.tree):
            if node in self.skipped:
                continue
            if isinstance(node, ast.Lambda) and not self.consumed(node):
                parent = self.parents.get(node)
                assigned = isinstance(parent, ast.Assign | ast.AnnAssign) and parent.value is node and all(
                    isinstance(target, ast.Name)
                    for target in (parent.targets if isinstance(parent, ast.Assign) else [parent.target]))
                if not assigned:
                    self.escaped.add(node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and not self.consumed(node):
                self.escaped.update(self.functions.get(node.id, ()))

    def consumed(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        if isinstance(parent, ast.Call):
            if parent.func is node:
                return True
            builtin = self.builtin(parent.func)
            return builtin in _MAPPING_BUILTINS and bool(parent.args) and parent.args[0] is node
        if isinstance(parent, ast.keyword) and parent.arg == "key":
            call = self.parents.get(parent)
            return isinstance(call, ast.Call) and self.builtin(call.func) in _KEYED_BUILTINS
        return False

    def is_module(self, name: str) -> bool:
        sources = self.sources.get(name)
        if sources is None:
            return name in self.data_modules
        return all(source.kind == "module" for source in sources)

    def solve(self) -> None:
        changed = True
        while changed:
            changed = False
            tainted = {self.find(root) for root, source in self.mutations if not self.source_data(source)}
            for name in sorted(self.data):
                if self.find(name) in tainted or not all(self.source_data(source) for source in self.sources[name]):
                    self.data.discard(name)
                    changed = True
            for name in sorted(self.elements):
                if self.find(name) in tainted or not all(
                        self.source_elements(source) for source in self.sources[name]):
                    self.elements.discard(name)
                    changed = True

    def source_data(self, source: _Source) -> bool:
        node = source.node
        if source.kind == "data":
            return True
        if source.kind == "param" and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return self.param_data(node, source.param)
        if not isinstance(node, ast.expr):
            return False
        return self.is_data(node) if source.kind == "expr" else self.has_data_elements(node)

    def source_elements(self, source: _Source) -> bool:
        if source.kind == "expr" and isinstance(source.node, ast.expr):
            return self.has_data_elements(source.node)
        return self.source_data(source)

    def param_data(self, function: _Function, name: str) -> bool:
        contexts = self.contexts.get(function, [])
        if function in self.escaped or not contexts:
            return False
        return all(name in context and all(self.source_data(source) for source in context[name])
                   for context in contexts)

    def returns_data(self, function: _Function) -> bool:
        if function in self.returning:
            return True
        self.returning.append(function)
        try:
            nodes = list(_own_nodes(function))
            if isinstance(function, ast.AsyncFunctionDef) or any(
                    isinstance(node, ast.Yield | ast.YieldFrom | ast.Await) for node in nodes):
                return False
            if isinstance(function, ast.Lambda):
                return self.is_data(function.body)
            return all(node.value is None or self.is_data(node.value)
                       for node in nodes if isinstance(node, ast.Return))
        finally:
            self.returning.pop()

    def name_data(self, name: str) -> bool:
        if name in self.sources:
            return name in self.data
        if name in self.data_modules:
            return False
        return name in self.data_names or name in _BUILTIN_NAMES

    def module_rooted(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute | ast.Call | ast.Subscript):
            return False
        root = _root_name(node)
        return root is not None and self.is_module(root)

    def is_data(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Constant | ast.JoinedStr):
            return True
        if isinstance(node, ast.Compare):
            return _boolean_comparison(node) or all(self.is_data(item) for item in (node.left, *node.comparators))
        if isinstance(node, ast.Name):
            return self.name_data(node.id)
        if isinstance(node, ast.Attribute):
            return self.module_rooted(node)
        if isinstance(node, ast.Subscript):
            return self.is_data(node.value)
        if isinstance(node, ast.Starred):
            return self.has_data_elements(node.value)
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            return all(self.is_data(element) for element in node.elts)
        if isinstance(node, ast.Dict):
            return all(self.is_data(key) for key in node.keys if key is not None) and all(
                self.is_data(value) for value in node.values)
        if isinstance(node, ast.ListComp | ast.SetComp):
            return self.is_data(node.elt)
        if isinstance(node, ast.DictComp):
            return self.is_data(node.key) and self.is_data(node.value)
        if isinstance(node, ast.BinOp):
            return self.is_data(node.left) and self.is_data(node.right)
        if isinstance(node, ast.UnaryOp):
            return isinstance(node.op, ast.Not) or self.is_data(node.operand)
        if isinstance(node, ast.BoolOp):
            return all(self.is_data(value) for value in node.values)
        if isinstance(node, ast.IfExp):
            return self.is_data(node.body) and self.is_data(node.orelse)
        if isinstance(node, ast.NamedExpr):
            return self.is_data(node.value)
        if isinstance(node, ast.Call):
            return self.call_data(node)
        return False

    def call_data(self, node: ast.Call) -> bool:
        func = node.func
        if self.module_rooted(func):
            return True
        builtin = self.builtin(func)
        if builtin is not None:
            return self.builtin_data(builtin, node)
        functions = self.callables(func)
        if functions:
            return all(self.returns_data(function) for function in functions)
        return isinstance(func, ast.Attribute) and self.is_data(func.value) and all(
            self.has_data_elements(argument) for argument in node.args) and all(
            self.has_data_elements(keyword.value) for keyword in node.keywords)

    def builtin_data(self, builtin: str, node: ast.Call) -> bool:
        arguments = node.args
        keywords = [keyword for keyword in node.keywords if keyword.arg != "key"]
        if builtin in _SCALAR_BUILTINS:
            return True
        if builtin in _OPERATOR_BUILTINS:
            return all(self.is_data(argument) for argument in arguments) and all(
                self.is_data(keyword.value) for keyword in keywords)
        if builtin in _COLLECTING_BUILTINS:
            return all(self.has_data_elements(argument) for argument in arguments)
        if builtin == "dict":
            return all(self.has_data_elements(argument) for argument in arguments) and all(
                self.is_data(keyword.value) for keyword in keywords)
        if builtin in _KEYED_BUILTINS:
            chosen = (self.has_data_elements(arguments[0]) if len(arguments) == 1
                      else all(self.is_data(argument) for argument in arguments))
            return chosen and all(self.is_data(keyword.value) for keyword in keywords)
        if builtin == "sum":
            return bool(arguments) and self.has_data_elements(arguments[0]) and all(
                self.is_data(argument) for argument in arguments[1:])
        return False

    def has_data_elements(self, node: ast.expr) -> bool:
        if self.is_data(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.sources and node.id in self.elements
        if isinstance(node, ast.GeneratorExp):
            return self.is_data(node.elt)
        if isinstance(node, ast.IfExp):
            return self.has_data_elements(node.body) and self.has_data_elements(node.orelse)
        if isinstance(node, ast.BoolOp):
            return all(self.has_data_elements(value) for value in node.values)
        if isinstance(node, ast.NamedExpr):
            return self.has_data_elements(node.value)
        if isinstance(node, ast.Call):
            return self.iterator_elements(node)
        return False

    def iterator_elements(self, node: ast.Call) -> bool:
        builtin = self.builtin(node.func)
        arguments = node.args
        if any(isinstance(argument, ast.Starred) for argument in arguments) or not arguments:
            return False
        if builtin == "map":
            callee = arguments[0]
            functions = self.callables(callee)
            if functions:
                return all(self.returns_data(function) for function in functions)
            chosen = self.builtin(callee)
            return chosen in _SCALAR_BUILTINS or (chosen in _OPERATOR_BUILTINS and all(
                self.has_data_elements(argument) for argument in arguments[1:]))
        if builtin == "filter":
            return len(arguments) > 1 and self.has_data_elements(arguments[1])
        if builtin == "zip":
            return all(self.has_data_elements(argument) for argument in arguments)
        if builtin in ("enumerate", "reversed"):
            return self.has_data_elements(arguments[0])
        return False

    def leaks(self) -> tuple[SandboxUse, ...]:
        found: list[SandboxUse] = []
        for node in ast.walk(self.tree):
            if node in self.skipped:
                continue
            for what in self.sinks(node):
                found.append(SandboxUse(getattr(node, "lineno", 0), what, getattr(node, "col_offset", 0)))
        return tuple(sorted(found, key=_position))

    def sinks(self, node: ast.AST) -> Iterator[str]:
        if isinstance(node, ast.FormattedValue) and not self.is_data(node.value):
            yield "an f-string field"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and not self.is_data(node.right):
            yield "% formatting"
        elif isinstance(node, ast.Set) and not all(self.is_data(element) for element in node.elts):
            yield "a set display"
        elif isinstance(node, ast.SetComp) and not self.is_data(node.elt):
            yield "a set comprehension"
        elif isinstance(node, ast.Call):
            yield from self.call_sinks(node)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and self.builtin(node) == _STR:
            yield from self.str_value(node)

    def call_sinks(self, node: ast.Call) -> Iterator[str]:
        builtin = self.builtin(node.func)
        arguments = node.args
        if builtin == _STR and arguments and not self.is_data(arguments[0]):
            yield "str()"
        if builtin in _HASHING_BUILTINS and arguments and not self.has_data_elements(arguments[0]):
            yield f"{builtin}()"
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in FORMAT_ATTRIBUTES and not (
                all(self.is_data(argument) for argument in arguments)
                and all(self.is_data(keyword.value) for keyword in node.keywords)):
            yield f".{func.attr}()"

    def str_value(self, node: ast.Name) -> Iterator[str]:
        parent = self.parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            return
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return
        if isinstance(parent, ast.Call) and self.builtin(parent.func) in _MAPPING_BUILTINS and parent.args and (
                parent.args[0] is node):
            if not all(self.has_data_elements(argument) for argument in parent.args[1:]):
                yield "str applied by map() or filter()"
            return
        call = self.parents.get(parent) if isinstance(parent, ast.keyword) and parent.arg == "key" else None
        if isinstance(call, ast.Call) and self.builtin(call.func) in _KEYED_BUILTINS:
            arguments = call.args
            if not (self.has_data_elements(arguments[0]) if len(arguments) == 1
                    else all(self.is_data(argument) for argument in arguments)):
                yield "key=str"
            return
        yield "str passed as a value"


@op("ctl", name="when", description="Run the 'then' branch when the condition is truthy at run time, else the 'else' branch",
    container=True, branches=("then", "else"),
    examples=[{"cond": {"$ref": "n0000000001", "path": []}}])
class When(Args):
    cond: RCond = Field(description="Condition: a $ref to a boolean or JSON value, truthy runs the then branch")


@op("ctl", name="each", description="Run the body once per element of a runtime list, as a journaled sub-plan",
    container=True, branches=("body",),
    examples=[
        {"over": {"$ref": "n0000000001", "path": ["items", {"slice": [None, 3, None]}]}},
        {"over": {"$ref": "n0000000001", "path": ["items"]}, "limit": 5},
    ])
class Each(Args):
    over: RList = Field(description="List to iterate: a $ref or $item producing a runtime list (or JSON holding one)")
    limit: int | None = Field(default=None, ge=1, description="Largest number of items to visit")


@op("ctl", name="parallel", description="Run the body's lanes independently; nodes are ordered per resource lane only",
    container=True, branches=("body",), exports_body=True,
    examples=[{}])
class Parallel(Args):
    pass


@op("ctl", name="dynamic", out="json", location="hub",
    description="Run a sandboxed function with resolved inputs at run time; it returns a sub-plan",
    examples=[
        {
            "fn": {
                "name": "per_row",
                "source": "def per_row(n):\n    return [s.ui.tap(by.test_id('row').nth(i)) for i in range(n)]\n",
                "hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "params": ["n"],
                "freevars": {"s": {"kind": "session", "value": "main"}},
                "globals": ["by", "range"],
            },
            "inputs": {"n": {"$ref": "n0000000001", "path": []}},
        },
    ])
class Dynamic(Args):
    fn: FunctionSource = Field(description="Captured source of the @eks.dynamic function, run in the sandbox at execution time")
    inputs: dict[str, RJson] = Field(default_factory=dict, description="Values bound to the function's parameters, usually $refs")


@op("ctl", name="macro", out="json", description="A macro expanded at plan time into its body",
    container=True, branches=("body",), exports_body=True,
    examples=[{"name": "cookbook.new_invoice", "version": "1", "params": {"branch": "kadikoy", "amount": "5"}}])
class Macro(Args):
    name: str = Field(description="Qualified macro name such as cookbook.new_invoice")
    version: str = Field(default="1", description="Macro version the body was traced with")
    params: dict[str, RJson] = Field(default_factory=dict, description="Arguments the macro was called with")


__all__ = [
    "ALLOWED_DYNAMIC_GLOBALS",
    "BPY_MODULES",
    "DATA_BINDING_KINDS",
    "FORMAT_ATTRIBUTES",
    "HOST_ATTRIBUTES",
    "INTERNAL_ATTRIBUTES",
    "INTROSPECTION_ATTRIBUTES",
    "SANDBOX_BUILTINS",
    "SANDBOX_EKS_ATTRIBUTES",
    "SANDBOX_EKS_NAMESPACES",
    "Dynamic",
    "Each",
    "Macro",
    "Parallel",
    "SandboxScan",
    "SandboxUse",
    "When",
    "blocked_attribute",
    "sandbox_leaks",
    "scan_sandbox_source",
]
