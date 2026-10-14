from __future__ import annotations

import hashlib
import inspect
import os
import re
import sys
import threading
import traceback
import types
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import JsonValue
from pydantic_core import PydanticSerializationError, to_jsonable_python

from eks.discovery import (
    _FILES,
    _LOCK,
    _OWNERS,
    _TREE_OF,
    ENTRY_FOLDERS,
    HARNESS_DIR,
    SPEC_SEPARATOR,
    AppInfo,
    DiscoveryError,
    _declares_name,
    _harness_dirs,
    _run_release_hooks,
    builtins_registry,
    discover,
    find_app,
    find_tree,
    load_file,
    module_name,
    split_spec,
)
from eks.discovery import EntryFile as DiscoveredFile
from eks.planning import (
    REQUIRES_FINALIZE_ORDER,
    _require_contributions,
    app_defaults,
    inherited,
    merged_session,
)
from eks.session import Session
from eks.tracing import TraceContext, tracing
from eks.validation import validate as validate_plan
from eks.workspace import plan_spec
from eks_harness.cli.plan_cmds import (
    Entry,
    _authoring_errors,
    _chain,
    _entry,
    _message,
    _source_line,
)
from eks_harness.engine.errors import BuildError
from eks_harness.engine.model import MAIN_SESSION, EntryFolder, IssueOut, Platform
from eks_harness.engine.registries import HubRegistries, TreeRegistry
from eks_harness.engine.validator import Report
from eks_harness.ir.entries import (
    EditOptions,
    FlowOptions,
    SceneOptions,
    ScoreOptions,
    SeedOptions,
)
from eks_harness.ir.plan import Clock, Plan, SessionSpec
from eks_harness.ir.registry import (
    ENTRY_MODULE_PREFIX,
    AnySpec,
    BackendPolicySpec,
    BackendSpec,
    CatalogSpec,
    CheckSpec,
    EntrySpec,
    MacroSpec,
    OpSpec,
    PresetSpec,
    Registry,
    RendererSpec,
    ResourceSpec,
    definition_of,
    using_registry,
)

INLINE_PREFIX = "_eks_inline_"
ONESHOT_NAME = "do"
_INLINE_DIGITS = 16
_INLINE_FILE = re.compile(rf"^{re.escape(INLINE_PREFIX)}[0-9a-f]{{{_INLINE_DIGITS}}}\.py$")
_OPTION_TYPES = (FlowOptions, SeedOptions, EditOptions, SceneOptions, ScoreOptions)
_EXITS = (SystemExit, KeyboardInterrupt)


@dataclass(frozen=True, slots=True)
class BuildRequest:
    spec: str | None = None
    source: str | None = None
    entry: str | None = None
    folder: EntryFolder = "flows"
    tree: Path | None = None
    cwd: Path | None = None
    params: Mapping[str, str] | None = None
    platform: Platform | None = None
    plugins: bool = True


@dataclass(frozen=True, slots=True)
class OneShotRequest:
    op: str
    args: Mapping[str, JsonValue]
    platform: Platform | None = None
    app: str | None = None
    persona: str | None = None
    seed: str | None = None
    backend: str | None = None
    tree: Path | None = None
    plugins: bool = True


@dataclass(frozen=True)
class BuiltPlan:
    plan: Plan
    tree: Path | None
    registry: TreeRegistry
    spec: str | None
    issues: tuple[IssueOut, ...]


@dataclass(frozen=True, slots=True)
class EntryOut:
    kind: str
    name: str
    file: str
    qualname: str
    line: int | None
    params: tuple[Mapping[str, JsonValue], ...]
    tags: tuple[str, ...]
    description: str
    app: str | None
    platforms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntryFile:
    path: str
    kind: str | None
    entries: tuple[EntryOut, ...]
    error: str | None = None


class PlanBuilder:
    def __init__(self, registries: HubRegistries) -> None:
        self.registries = registries
        self._inline = threading.Lock()

    def build(self, request: BuildRequest) -> BuiltPlan:
        if (request.spec is None) == (request.source is None):
            raise BuildError("a plan is built from exactly one of spec and source")
        if request.spec is not None:
            spec = _with_entry(request.spec, request.entry)
            base = _base(request.cwd, request.tree)
            tree = tree_of(spec, request.tree, base)
            return self._plan(spec, tree, base, request, label=spec)
        return self._inline_build(request.source or "", request)

    def validate(self, plan: Plan, *, tree: Path | None, plugins: bool = True) -> tuple[TreeRegistry, list[IssueOut]]:
        root = _resolved(tree)
        entry = _tree_entry(self.registries, root, plugins, root)
        return entry, self._check(plan, entry, root)

    def oneshot(self, request: OneShotRequest) -> BuiltPlan:
        tree = _resolved(request.tree)
        if tree is not None and not tree.is_dir():
            raise BuildError(f"tree {tree} is not a directory on the hub")
        base = tree if tree is not None else Path.cwd().resolve()
        entry = _tree_entry(self.registries, tree, request.plugins, base)
        try:
            with using_registry(entry.registry):
                plan = _trace_oneshot(request, entry.registry, tree)
        except BuildError:
            raise
        except (Exception, SystemExit, KeyboardInterrupt) as problem:
            error = _located_error(problem, f"do {request.op}", _tree_frame(tree, base), base)
            if error is None:
                raise
            raise error from problem
        return BuiltPlan(plan=plan, tree=tree, registry=entry, spec=None, issues=tuple(self._check(plan, entry, tree)))

    def files(self, tree: Path, *, kind: str | None = None, plugins: bool = True) -> list[EntryFile]:
        root = tree.expanduser().resolve()
        if not root.is_dir():
            raise BuildError(f"tree {root} is not a directory on the hub")
        wanted = _entry_kind(kind)
        discovered = [(item, item.path.resolve().relative_to(root).as_posix()) for item in discover(root)]
        try:
            entry = _tree_entry(self.registries, root, plugins, root)
        except BuildError as failed:
            return _unloaded(failed, discovered, wanted)
        found: list[EntryFile] = []
        for item, relative in discovered:
            if wanted is not None and item.kind != wanted:
                continue
            try:
                with using_registry(entry.registry):
                    specs = load_file(item.path)
                    entries = tuple(_entry_out(spec, relative, root) for spec in specs)
            except (Exception, SystemExit, KeyboardInterrupt) as problem:
                error = _load_error(problem, item.path, root)
                if error is None:
                    raise
                found.append(EntryFile(path=relative, kind=item.kind, entries=(), error=error))
                continue
            found.append(EntryFile(path=relative, kind=item.kind, entries=entries))
        return found

    def entries(self, tree: Path, *, kind: str | None = None, plugins: bool = True) -> list[EntryOut]:
        return [entry for file in self.files(tree, kind=kind, plugins=plugins) if file.error is None
                for entry in file.entries]

    def _plan(self, spec: str, tree: Path | None, base: Path, request: BuildRequest, *,
              label: str | None, entry: TreeRegistry | None = None) -> BuiltPlan:
        try:
            chosen = entry if entry is not None else self.registries.for_tree(tree, plugins=request.plugins)
            plan = plan_spec(spec, params=dict(request.params or {}), platform=request.platform, cwd=base,
                             plugins=request.plugins, registry=chosen.registry)
        except (Exception, SystemExit, KeyboardInterrupt) as problem:
            error = _build_error(problem, spec, base)
            if error is None:
                raise
            raise error from problem
        return BuiltPlan(plan=plan, tree=tree, registry=chosen, spec=label,
                         issues=tuple(self._check(plan, chosen, tree)))

    def _inline_build(self, source: str, request: BuildRequest) -> BuiltPlan:
        tree = _inline_tree(request)
        base = _resolved(request.cwd) or tree
        folder = tree / HARNESS_DIR / request.folder
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        path = folder / f"{INLINE_PREFIX}{source_digest[:_INLINE_DIGITS]}.py"
        spec = _with_entry(str(path), request.entry)
        name = module_name(path)
        with self._inline, self._holding(tree):
            _sweep_inline(tree)
            entry = _tree_entry(self.registries, tree, request.plugins, base)
            scratch = _InlineRegistry(name)
            inline = replace(entry, registry=scratch,
                             project_digest=_inline_digest(entry.project_digest, source_digest))
            created = [directory for directory in (folder.parent, folder) if not directory.exists()]
            planned = False
            previous = _track(scratch, entry, name)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
                built = self._plan(spec, tree, base, request, label=None, entry=inline)
                planned = True
                return built
            finally:
                path.unlink(missing_ok=True)
                _untrack(scratch, name, previous, planned=planned)
                for directory in reversed(created):
                    _remove_empty(directory)

    def _holding(self, tree: Path) -> ExitStack:
        stack = ExitStack()
        for stripe in self.registries._stripes_of((tree, True), (tree, False)):
            stack.enter_context(stripe)
        return stack

    def _check(self, plan: Plan, entry: TreeRegistry, tree: Path | None) -> list[IssueOut]:
        report = validate_plan(plan, registry=entry.registry, tree=tree)
        return _in_walk_order(plan, [*issues_of(report), *self.registries.requirement_issues(plan, entry)])


def _in_walk_order(plan: Plan, issues: Sequence[IssueOut]) -> list[IssueOut]:
    positions = {node.id: index for index, (node, _ancestors) in enumerate(plan.walk())}
    unknown = len(positions)

    def position(issue: IssueOut) -> int:
        if issue.node is None:
            return -1
        return positions.get(issue.node, unknown)

    unique: dict[tuple[str | None, ...], IssueOut] = {}
    for issue in issues:
        unique.setdefault((issue.severity, issue.code, issue.message, issue.node, issue.field, issue.src), issue)
    return sorted(unique.values(), key=position)


def tree_of(spec: str | None, tree: Path | None, cwd: Path | None) -> Path | None:
    if spec is None:
        return None
    try:
        path, _name = split_spec(spec)
    except DiscoveryError as problem:
        raise BuildError(str(problem)) from problem
    path = path.expanduser()
    base = _base(cwd, tree)
    file = (path if path.is_absolute() else base / path).resolve()
    explicit = _resolved(tree)
    if explicit is not None and not file.is_relative_to(explicit):
        raise BuildError(f"{file} is not inside the tree {explicit}; pass the tree that holds the entry file")
    found = find_tree(file.parent)
    return found.resolve() if found is not None else explicit


def issues_of(report: object) -> list[IssueOut]:
    if not isinstance(report, Report):
        raise TypeError(f"issues_of takes a validation Report, got {type(report).__name__}")
    return [IssueOut(severity=issue.severity, code=issue.code, message=issue.message, node=issue.node,
                     field=issue.field, src=issue.src) for issue in report.issues]


def _resolved(path: Path | None) -> Path | None:
    return path.expanduser().resolve() if path is not None else None


def _base(cwd: Path | None, tree: Path | None) -> Path:
    chosen = _resolved(cwd) or _resolved(tree)
    return chosen if chosen is not None else Path.cwd().resolve()


def _with_entry(spec: str, entry: str | None) -> str:
    if entry is None:
        return spec
    _text, separator, name = spec.partition(SPEC_SEPARATOR)
    if not separator:
        return f"{spec}{SPEC_SEPARATOR}{entry}"
    if name != entry:
        raise BuildError(f"the spec {spec!r} names entry {name!r} but entry={entry!r} was given; name one")
    return spec


def _inline_tree(request: BuildRequest) -> Path:
    tree = _resolved(request.tree)
    if tree is None and request.cwd is not None:
        found = find_tree(request.cwd.expanduser().resolve())
        tree = found.resolve() if found is not None else None
    if tree is None:
        raise BuildError("inline source is planned inside a tree; pass the tree it belongs to")
    if not tree.is_dir():
        raise BuildError(f"tree {tree} is not a directory on the hub")
    return tree


def _remove_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        return


class _InlineRegistry(Registry):
    def __init__(self, module: str) -> None:
        super().__init__()
        self._inline_module = module

    def _shadow(self, table: str, key: object, spec: AnySpec) -> None:
        if definition_of(spec).module != self._inline_module:
            return
        rows: dict[object, AnySpec] = getattr(self, table)
        existing = rows.get(key)
        if existing is None:
            return
        owner = definition_of(existing).module
        if owner.startswith(ENTRY_MODULE_PREFIX) and owner != self._inline_module:
            setattr(self, table, {name: row for name, row in rows.items() if name != key})

    def register_op(self, spec: OpSpec) -> OpSpec:
        self._shadow("_ops", spec.name, spec)
        return super().register_op(spec)

    def register_macro(self, spec: MacroSpec) -> MacroSpec:
        self._shadow("_macros", spec.name, spec)
        return super().register_macro(spec)

    def register_resource(self, spec: ResourceSpec) -> ResourceSpec:
        self._shadow("_resources", spec.kind, spec)
        return super().register_resource(spec)

    def register_check(self, spec: CheckSpec) -> CheckSpec:
        self._shadow("_checks", spec.kind, spec)
        return super().register_check(spec)

    def register_catalog(self, spec: CatalogSpec) -> CatalogSpec:
        self._shadow("_catalog", (spec.category, spec.kind), spec)
        return super().register_catalog(spec)

    def register_renderer(self, spec: RendererSpec) -> RendererSpec:
        self._shadow("_renderers", spec.media_kind, spec)
        return super().register_renderer(spec)

    def register_backend(self, spec: BackendSpec) -> BackendSpec:
        self._shadow("_backends", spec.name, spec)
        return super().register_backend(spec)

    def register_backend_policy(self, spec: BackendPolicySpec) -> BackendPolicySpec:
        self._shadow("_policies", spec.name, spec)
        return super().register_backend_policy(spec)

    def register_preset(self, spec: PresetSpec) -> PresetSpec:
        self._shadow("_presets", (spec.category, spec.name), spec)
        return super().register_preset(spec)

    def register_entry(self, spec: EntrySpec) -> EntrySpec:
        self._shadow("_entries", (spec.kind, spec.name), spec)
        return super().register_entry(spec)


def _track(scratch: Registry, entry: TreeRegistry, name: str) -> types.ModuleType | None:
    with _LOCK:
        state = entry.registry.snapshot()
        scratch.rebase(builtins_registry())
        scratch.restore(state)
        _TREE_OF[scratch] = entry.tree
        return sys.modules.get(name)


def _untrack(scratch: Registry, name: str, previous: types.ModuleType | None, *, planned: bool) -> None:
    with _LOCK:
        _TREE_OF.pop(scratch, None)
        _OWNERS.pop(scratch, None)
        for key in [key for key in _FILES if key[0] is scratch]:
            del _FILES[key]
        loaded = sys.modules.get(name)
        if loaded is not None and loaded is not previous:
            if planned:
                weakref.finalize(scratch, _forget_module, name, loaded)
            elif previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    if not planned:
        _run_release_hooks([scratch])


def _forget_module(name: str, module: types.ModuleType) -> None:
    if sys.modules.get(name) is module:
        sys.modules.pop(name, None)


def _inline_digest(project: str | None, source_digest: str) -> str:
    combined = hashlib.sha256(f"{project or ''}\0{source_digest}".encode())
    return "sha256:" + combined.hexdigest()


def _sweep_inline(tree: Path) -> None:
    for folder in ENTRY_FOLDERS:
        directory = tree / HARNESS_DIR / folder
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if _INLINE_FILE.match(name) is None:
                continue
            stale = directory / name
            try:
                stale.unlink(missing_ok=True)
            except OSError as problem:
                raise BuildError(f"cannot delete the inline file {stale} a stopped hub left behind ({problem}); "
                                 "delete it by hand") from problem


def _exit_message(problem: BaseException, what: str) -> str:
    return (f"{_message(problem, False)} raised while loading or planning {what}; entry code must return or raise "
            "an exception instead of exiting the process")


def _build_error(problem: BaseException, spec: str, base: Path) -> BuildError | None:
    return _located_error(problem, spec, _entry(spec, base), base)


def _located_error(problem: BaseException, what: str, entry: Entry, base: Path) -> BuildError | None:
    where = _source_line(problem, entry, base)
    if isinstance(problem, _EXITS):
        return BuildError(_exit_message(problem, what), where=where)
    authoring = isinstance(problem, _authoring_errors())
    if not authoring and where is None:
        return None
    return BuildError(_message(problem, authoring), where=where)


def _load_error(problem: BaseException, path: Path, root: Path) -> str | None:
    error = _build_error(problem, str(path), root)
    if error is None:
        return None
    return _shown_error(error)


def _shown_error(error: BuildError) -> str:
    return f"{error.message}\n  at {error.where}" if error.where else error.message


def _tree_frame(tree: Path | None, base: Path) -> Entry:
    return Entry(None, (tree if tree is not None else base,), tree)


def _tree_entry(registries: HubRegistries, tree: Path | None, plugins: bool, base: Path | None) -> TreeRegistry:
    try:
        return registries.for_tree(tree, plugins=plugins)
    except (Exception, SystemExit, KeyboardInterrupt) as problem:
        root = _resolved(tree)
        shown = _resolved(base) or root or Path.cwd().resolve()
        what = f"the tree {root}" if root is not None else "the hub plugins"
        error = _located_error(problem, what, _tree_frame(root, shown), shown)
        if error is None:
            raise
        raise error from problem


def _failing_file(problem: BaseException, files: Mapping[Path, str]) -> str | None:
    for link in _chain(problem):
        if isinstance(link, SyntaxError) and link.filename:
            found = files.get(Path(link.filename).resolve())
            if found is not None:
                return found
        for frame in reversed(traceback.extract_tb(link.__traceback__)):
            if not frame.filename or frame.filename.startswith("<"):
                continue
            found = files.get(Path(frame.filename).resolve())
            if found is not None:
                return found
    return None


def _unloaded(failed: BuildError, discovered: Sequence[tuple[DiscoveredFile, str]],
              wanted: str | None) -> list[EntryFile]:
    cause = failed.__cause__
    paths = {item.path.resolve(): relative for item, relative in discovered}
    failing = _failing_file(cause, paths) if cause is not None else None
    if failing is None:
        raise failed
    error = _shown_error(failed)
    blocked = f"not loaded: the tree's seeds and scenes load before any entry file, and {failing} failed to load"
    return [EntryFile(path=relative, kind=item.kind, entries=(), error=error if relative == failing else blocked)
            for item, relative in discovered if wanted is None or item.kind == wanted]


def _entry_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    if kind in ENTRY_FOLDERS.values():
        return kind
    folder = ENTRY_FOLDERS.get(kind)
    if folder is not None:
        return folder
    raise BuildError(f"unknown entry kind {kind!r}; use one of {sorted(ENTRY_FOLDERS.values())}")


def _entry_out(spec: EntrySpec, relative: str, root: Path) -> EntryOut:
    options = spec.options
    tags = tuple(str(tag) for tag in options.tags) if isinstance(options, _OPTION_TYPES) else ()
    description = options.description if isinstance(options, _OPTION_TYPES) else ""
    app: str | None = None
    platforms: tuple[str, ...] = ()
    if isinstance(options, FlowOptions):
        app = options.app
        platforms = tuple(options.platforms)
        if not platforms:
            info = find_app(root, options.app)
            platforms = tuple(info.platforms) if info is not None else ()
    return EntryOut(kind=spec.kind, name=spec.name, file=relative, qualname=spec.qualname, line=spec.line,
                    params=tuple(_parameters(spec)), tags=tags, description=description, app=app,
                    platforms=platforms)


def _parameters(spec: EntrySpec) -> Iterator[dict[str, JsonValue]]:
    for parameter in list(inspect.signature(spec.fn).parameters.values())[1:]:
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        item: dict[str, JsonValue] = {"name": parameter.name, "annotation": _annotation(parameter.annotation)}
        if parameter.default is not inspect.Parameter.empty:
            try:
                item["default"] = to_jsonable_python(parameter.default)
            except PydanticSerializationError:
                item["defaultText"] = repr(parameter.default)
        yield item


def _annotation(annotation: Any) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _tree_apps(tree: Path) -> list[AppInfo]:
    found: dict[str, AppInfo] = {}
    for harness in _harness_dirs(tree):
        name = _declares_name(harness) or harness.parent.name
        if name in found:
            continue
        info = find_app(tree, name)
        if info is not None:
            found[name] = info
    return list(found.values())


def _choose_app(request: OneShotRequest, tree: Path | None) -> tuple[str, Platform, AppInfo | None]:
    what = f"do {request.op}"
    if request.app is not None:
        info = find_app(tree, request.app) if tree is not None else None
        if tree is not None and info is None:
            raise BuildError(f"{what}: the tree has no app profile named {request.app!r}; pass app= with one of "
                             f"{sorted(app.name for app in _tree_apps(tree))}")
        name = request.app
    else:
        apps = _tree_apps(tree) if tree is not None else []
        matching = [app for app in apps if request.platform is None or request.platform in app.platforms]
        if len(matching) != 1:
            raise BuildError(f"{what} uses a session, but {_app_problem(matching, tree, request.platform)}; pass "
                             "app= naming the app profile")
        info = matching[0]
        name = info.name
    return name, _choose_platform(what, name, request.platform, info), info


def _app_problem(matching: Sequence[AppInfo], tree: Path | None, platform: Platform | None) -> str:
    where = "" if platform is None else f" on {platform}"
    if tree is None:
        return "there is no tree to find its app profile in"
    if matching:
        return f"the tree has several apps{where} ({', '.join(sorted(app.name for app in matching))})"
    return f"the tree has no app profile{where}"


def _choose_platform(what: str, app: str, requested: Platform | None, info: AppInfo | None) -> Platform:
    platforms = tuple(info.platforms) if info is not None else ()
    if requested is not None:
        if platforms and requested not in platforms:
            raise BuildError(f"{what}: app {app!r} runs on {list(platforms)}; platform {requested!r} is not one of them")
        return requested
    if len(platforms) == 1:
        return _platform(platforms[0])
    listed = list(platforms) if platforms else ["web", "ios", "android"]
    raise BuildError(f"{what}: app {app!r} does not name one platform; pass platform= with one of {listed}")


def _platform(value: str) -> Platform:
    if value == "web":
        return "web"
    if value == "ios":
        return "ios"
    if value == "android":
        return "android"
    raise BuildError(f"unknown platform {value!r}; use one of ['web', 'ios', 'android']")


def _session(request: OneShotRequest, registry: Registry, tree: Path | None) -> SessionSpec:
    app, platform, info = _choose_app(request, tree)
    now, root_seed, backend = inherited(registry, request.seed, now=None, root_seed=None, backend=request.backend)
    fields = merged_session(app_defaults(info.tables if info is not None else None, platform, app), {}, registry,
                            f"do {request.op}", platform)
    return SessionSpec.model_validate({
        "key": MAIN_SESSION, "app": app, "platform": platform, "seed": request.seed, "persona": request.persona,
        "backend": backend, "root_seed": root_seed, "now": now, **fields,
    })


def _trace_oneshot(request: OneShotRequest, registry: Registry, tree: Path | None) -> Plan:
    spec = registry.op(request.op)
    if spec.container:
        raise BuildError(f"do runs one plain op; {request.op} is a container op and needs a body, so write a flow")
    session: SessionSpec | None = None
    clock = Clock()
    if spec.resources:
        session = _session(request, registry, tree)
        clock = Clock(now=session.now, seed=session.root_seed or 0)
    else:
        given = [name for name, value in (("persona", request.persona), ("seed", request.seed),
                                          ("backend", request.backend)) if value is not None]
        if given:
            raise BuildError(f"{request.op} uses no lease or backend, so it runs without a session; drop "
                             f"{', '.join(name + '=' for name in given)}")
    ctx = TraceContext(kind="flow", name=ONESHOT_NAME, registry=registry, clock=clock, session=session, tree=tree)
    with tracing(ctx):
        ctx.on_finalize(_require_contributions, order=REQUIRES_FINALIZE_ORDER)
        if session is not None:
            flow = Session(ctx, session)
            with flow.activate():
                if session.persona:
                    flow.record_op("session.as", {"persona": session.persona})
                value = flow.op(request.op, **dict(request.args))
        else:
            value = ctx.record(request.op, dict(request.args))
        ctx.set_outputs({"value": value})
        return ctx.finalize()


__all__ = [
    "INLINE_PREFIX",
    "ONESHOT_NAME",
    "BuildRequest",
    "BuiltPlan",
    "EntryFile",
    "EntryOut",
    "OneShotRequest",
    "PlanBuilder",
    "issues_of",
    "tree_of",
]
