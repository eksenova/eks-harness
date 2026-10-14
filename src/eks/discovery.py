from __future__ import annotations

import difflib
import errno
import hashlib
import importlib
import importlib.machinery
import importlib.util
import linecache
import os
import re
import sys
import threading
import time
import tomllib
import types
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eks.tracing import register_plugin_root
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    ENTRY_MODULE_PREFIX,
    PROJECT_PLUGIN,
    SCOPED_MODULE_PATTERN,
    AnySpec,
    EntryKind,
    EntrySpec,
    Provenance,
    Registry,
    RegistryState,
    base_registry,
    contributing,
    default_registry,
    definition_of,
)

if TYPE_CHECKING:
    from eks_harness.plugins.host import PluginHost
    from eks_harness.plugins.isolation import ImportScope

HARNESS_DIR = ".harness"
ENTRY_FOLDERS: Mapping[str, EntryKind] = {
    "flows": "flow",
    "seeds": "seed",
    "edits": "edit",
    "scenes": "scene",
    "scores": "score",
}
SPEC_SEPARATOR = "::"
MODULE_PREFIX = ENTRY_MODULE_PREFIX
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "__pycache__"})
APP_FILE = "app.toml"
PROJECT_FILE = "project.toml"
_NON_IDENTIFIER = re.compile(r"\W", re.ASCII)
_CACHE_CAPACITY = 64
_RACY_NS = 2_000_000_000
_PLUGINS_FOLDER = "plugins"


class DiscoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppInfo:
    name: str
    root: Path
    platforms: tuple[str, ...]
    tables: Mapping[str, Any] = field(default_factory=dict[str, Any], compare=False)


@dataclass(frozen=True, slots=True)
class EntryFile:
    path: Path
    kind: EntryKind | None


@dataclass(frozen=True, slots=True)
class _LoadedFile:
    digest: str
    specs: tuple[EntrySpec, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class _LoadedModule:
    name: str
    module: types.ModuleType
    contributions: tuple[tuple[str, Any, AnySpec], ...]
    sources: tuple[str, ...]
    files: frozenset[Path] = frozenset()
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class _TreeKey:
    root: Path | None


@dataclass(frozen=True, slots=True)
class _DirState:
    stamp: tuple[int, int, int]
    seen: int
    children: tuple[str, ...]
    harness: tuple[int, int, int] | None
    nested: bool


@dataclass
class _Builtins:
    registry: Registry | None = None
    tables: dict[str, dict[Any, Any]] = field(default_factory=dict[str, dict[Any, Any]])


_LOCK = threading.RLock()
_FILES: dict[tuple[Registry, str], _LoadedFile] = {}
_PLUGIN_MODULES: dict[tuple[Registry, str, str], _LoadedModule] = {}
_TREES: dict[tuple[Path | None, bool, bool], Registry] = {}
_TREE_OF: dict[Registry, Path | None] = {}
_OWNERS: OrderedDict[_TreeKey | Registry, None] = OrderedDict()
_BUILTINS = _Builtins()
_FROZEN: weakref.WeakSet[Registry] = weakref.WeakSet()
_RELEASE_HOOKS: list[Callable[[Sequence[Registry]], None]] = []
_SCANS: OrderedDict[Path, dict[str, _DirState]] = OrderedDict()
_SCAN_LOCK = threading.Lock()


class _EntryLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, source: bytes) -> None:
        super().__init__(fullname, path)
        self._source = source

    def get_data(self, path: str) -> bytes:
        if path == self.path:
            return self._source
        return super().get_data(path)

    def get_code(self, fullname: str) -> types.CodeType:
        return compile(self._source, self.path, "exec", dont_inherit=True)


def split_spec(spec: str) -> tuple[Path, str | None]:
    text, separator, name = spec.partition(SPEC_SEPARATOR)
    if not text.strip():
        raise DiscoveryError(f"entry spec {spec!r} names no file; write path/to/file.py or path/to/file.py::name")
    if not separator:
        return Path(text), None
    if not name.strip() or SPEC_SEPARATOR in name:
        raise DiscoveryError(f"entry spec {spec!r} needs one entry name after '::', such as {text}::checkout")
    return Path(text), name


def module_name(path: Path) -> str:
    resolved = path.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    return f"{MODULE_PREFIX}{digest}_{_NON_IDENTIFIER.sub('_', resolved.stem)}"


def _module_entries(registry: Registry, name: str) -> list[EntrySpec]:
    return [spec for spec in registry.entries() if spec.module == name]


def _intact(registry: Registry, name: str, loaded: _LoadedFile) -> bool:
    present = {(spec.kind, spec.name): spec for spec in _module_entries(registry, name)}
    return all(present.get((spec.kind, spec.name)) is spec for spec in loaded.specs)


def _execute(path: Path, name: str, source: bytes) -> None:
    loader = _EntryLoader(name, str(path), source)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    if spec is None:
        raise DiscoveryError(f"cannot load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    linecache.checkcache(str(path))
    try:
        loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise


def load_file(path: Path) -> list[EntrySpec]:
    target = path.expanduser().resolve()
    if not target.is_file():
        raise DiscoveryError(f"{path} does not exist or is not a file")
    reg = default_registry()
    name = module_name(target)
    source = target.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    key = (reg, name)
    with _LOCK:
        _touch(_owner_of(reg))
        cached = _FILES.get(key)
        if cached is not None and cached.digest == digest and name in sys.modules and _intact(reg, name, cached):
            return list(cached.specs)
        state = reg.snapshot()
        previous = _FILES.pop(key, None)
        reg.drop_module(name)
        try:
            _execute(target, name, source)
        except BaseException:
            reg.rollback(state, name)
            if previous is not None:
                _FILES[key] = previous
            raise
        entries = _module_entries(reg, name)
        _FILES[key] = _LoadedFile(digest, tuple(entries), target)
        return entries


def _listing(entries: Sequence[EntrySpec]) -> str:
    return ", ".join(f"{spec.name} ({spec.kind})" for spec in entries) or "nothing"


def resolve_entry(spec: str, *, kind: EntryKind | None = None, cwd: Path | None = None) -> EntrySpec:
    path, name = split_spec(spec)
    if not path.expanduser().is_absolute():
        path = (cwd or Path.cwd()) / path
    entries = load_file(path)
    what = kind or "entry"
    candidates = [entry for entry in entries if kind is None or entry.kind == kind]
    if name is not None:
        matches = [entry for entry in candidates if entry.name == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise DiscoveryError(
                f"{path} defines several entries named {name!r} ({_listing(matches)}); pass the kind or rename "
                "one of them")
        close = difflib.get_close_matches(name, [entry.name for entry in candidates], n=3, cutoff=0.6)
        hint = f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
        raise DiscoveryError(f"{path} has no {what} named {name!r}{hint} (it defines {_listing(entries)})")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise DiscoveryError(f"{path} defines no {what} (it defines {_listing(entries)})")
    choices = ", ".join(f"{spec}{SPEC_SEPARATOR}{entry.name}" for entry in candidates)
    several = f"several {kind} entries" if kind is not None else "several entries"
    raise DiscoveryError(f"{path} defines {several} ({_listing(candidates)}); name one: {choices}")


def find_tree(start: Path) -> Path | None:
    from eks_harness.plugins.project import find_tree as find_project_tree

    return find_project_tree(start)


def _children(directory: str) -> tuple[list[str], bool] | None:
    try:
        with os.scandir(directory) as scanned:
            children: list[str] = []
            harness = False
            for item in scanned:
                try:
                    if not item.is_dir():
                        continue
                    if item.name == HARNESS_DIR:
                        harness = True
                    elif (item.name not in SKIP_DIRS and not item.name.startswith(".")
                          and not item.is_symlink()):
                        children.append(item.name)
                except OSError:
                    continue
    except OSError:
        return None
    return sorted(children), harness


def _stat_stamp(path: str) -> tuple[int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns


def _settled(stamp: tuple[int, int, int] | None, recorded: tuple[int, int, int] | None, seen: int) -> bool:
    return stamp is not None and stamp == recorded and stamp[2] + _RACY_NS < seen


def _dir_state(here: str, cached: _DirState | None) -> _DirState | None:
    seen = time.time_ns()
    stamp = _stat_stamp(here)
    if stamp is None:
        return None
    harness_path = os.path.join(here, HARNESS_DIR)
    if cached is not None and _settled(stamp, cached.stamp, cached.seen) and (
            cached.harness is None or _settled(_stat_stamp(harness_path), cached.harness, cached.seen)):
        return cached
    listed = _children(here)
    if listed is None:
        return None
    children, has_harness = listed
    harness = _stat_stamp(harness_path) if has_harness else None
    nested = harness is not None and os.path.isfile(os.path.join(harness_path, PROJECT_FILE))
    return _DirState(stamp, seen, tuple(children), harness, nested)


def _scan(root: Path) -> list[tuple[Path, bool]]:
    with _SCAN_LOCK:
        cached = _SCANS.get(root, {})
    top = str(root)
    states: dict[str, _DirState] = {}
    found: list[tuple[Path, bool]] = []
    pending = [top]
    while pending:
        here = pending.pop()
        state = _dir_state(here, cached.get(here))
        if state is None:
            continue
        states[here] = state
        if here != top and state.nested:
            continue
        found.append((Path(here), state.harness is not None))
        pending.extend(os.path.join(here, name) for name in reversed(state.children))
    with _SCAN_LOCK:
        _SCANS[root] = states
        _SCANS.move_to_end(root)
        while len(_SCANS) > _CACHE_CAPACITY:
            _SCANS.popitem(last=False)
    return found


def _walk(root: Path) -> Iterator[Path]:
    for here, _harness in _scan(root):
        yield here


def _harness_parents(root: Path) -> list[Path]:
    return [here for here, harness in _scan(root) if harness]


def _sources(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.py") if p.is_file() and not p.name.startswith("_"))


def discover(tree: Path) -> list[EntryFile]:
    root = tree.expanduser().resolve()
    return _entry_files(root, _harness_parents(root))


def _entry_files(root: Path, parents: Sequence[Path]) -> list[EntryFile]:
    found = [EntryFile(path, kind) for folder, kind in ENTRY_FOLDERS.items()
             for path in _sources(root / HARNESS_DIR / folder)]
    apps = [EntryFile(path, "flow") for directory in parents if directory != root
            for path in _sources(directory / HARNESS_DIR / "flows")]
    return found + sorted(apps, key=lambda item: item.path)


def load_tree(tree: Path, *, kinds: Sequence[EntryKind] | None = None) -> list[EntrySpec]:
    return _load_entries(discover(tree), kinds)


def _load_entries(files: Sequence[EntryFile], kinds: Sequence[EntryKind] | None) -> list[EntrySpec]:
    wanted = None if kinds is None else frozenset(kinds)
    entries: list[EntrySpec] = []
    for item in files:
        if wanted is not None and item.kind not in wanted:
            continue
        entries.extend(spec for spec in load_file(item.path)
                       if wanted is None or spec.kind in wanted)
    return entries


def sync_tree(tree: Path, *, kinds: Sequence[EntryKind] | None = None) -> list[EntrySpec]:
    root = tree.expanduser().resolve()
    return _sync_files(root, discover(root), kinds)


def _sync_files(root: Path, files: Sequence[EntryFile], kinds: Sequence[EntryKind] | None) -> list[EntrySpec]:
    registry = default_registry()
    with _LOCK:
        _touch(_owner_of(registry))
        stale = [key for key, loaded in _FILES.items()
                 if key[0] is registry and loaded.path.is_relative_to(root) and not loaded.path.is_file()]
        for key in stale:
            registry.drop_module(key[1])
            del _FILES[key]
        if stale:
            _drop_unused_entry_modules(key[1] for key in stale)
            _run_release_hooks([registry])
        return _load_entries(files, kinds)


def _project_modules(harnesses: Sequence[Path]) -> list[str]:
    marker = os.sep + HARNESS_DIR + os.sep
    found: list[str] = []
    for name, module in list(sys.modules.items()):
        if name.startswith(MODULE_PREFIX):
            continue
        location = getattr(module, "__file__", None)
        if not isinstance(location, str) or marker not in location:
            continue
        resolved = _file_of(module)
        if resolved is not None and any(
                resolved.is_relative_to(harness) and not resolved.is_relative_to(harness / _PLUGINS_FOLDER)
                for harness in harnesses):
            found.append(name)
    return found


def _forget_project_code(root: Path, parents: Sequence[Path], changed: Iterable[Path]) -> None:
    registry = default_registry()
    harnesses = [directory / HARNESS_DIR for directory in parents]
    with _LOCK:
        _touch(_owner_of(registry))
        for source in changed:
            try:
                _discard_bytecode(source)
            except OSError as error:
                raise DiscoveryError(
                    f"{source}: cannot clear its compiled module before importing the tree's code again: "
                    f"{error}") from error
            linecache.checkcache(str(source))
        stale = [key for key, loaded in _FILES.items() if key[0] is registry and loaded.path.is_relative_to(root)]
        for key in stale:
            registry.drop_module(key[1])
            del _FILES[key]
        for name in _project_modules(harnesses):
            sys.modules.pop(name, None)
        importlib.invalidate_caches()
        if stale:
            _drop_unused_entry_modules(key[1] for key in stale)
            _run_release_hooks([registry])


def _default_host() -> PluginHost:
    from eks_harness.config import load as load_config
    from eks_harness.paths import resolve_paths
    from eks_harness.plugins.host import PluginHost

    paths = resolve_paths()
    return PluginHost(paths, load_config(paths))


def _contributions(before: Mapping[str, Mapping[Any, AnySpec]], after: Mapping[str, Mapping[Any, AnySpec]]
                   ) -> tuple[tuple[str, Any, AnySpec], ...]:
    return tuple((table, key, spec) for table, rows in after.items() for key, spec in rows.items()
                 if before.get(table, {}).get(key) is not spec)


def _still_registered(registry: Registry, loaded: _LoadedModule) -> bool:
    tables = registry.snapshot().tables
    return all(tables.get(table, {}).get(key) is spec for table, key, spec in loaded.contributions)


def _defining_modules(plugin_id: str, top: str, contributions: Sequence[tuple[str, Any, AnySpec]]) -> tuple[str, ...]:
    names = {definition_of(spec).module for _, _, spec in contributions if spec.provenance.plugin == plugin_id}
    names.discard(top)
    names.discard("")
    return tuple(sorted(names, key=lambda name: (-name.count("."), name)))


def _execute_plugin_module(scope: ImportScope | None, module: str, full_name: str,
                           loaded: _LoadedModule | None) -> types.ModuleType:
    current = sys.modules.get(full_name)
    if current is None:
        return scope.import_module(module) if scope is not None else importlib.import_module(module)
    if loaded is not None and loaded.name == full_name:
        for source in loaded.sources:
            existing = sys.modules.get(source)
            if existing is not None:
                importlib.reload(existing)
    return importlib.reload(current)


def _plugin_module_name(scope: ImportScope | None, module: str) -> str:
    return scope.scoped_name(module) if scope is not None and scope.owns(module) else module


def _shared_import(registry: Registry, plugin_id: str, module: str, full_name: str,
                   digest: str | None = None) -> _LoadedModule | None:
    current = sys.modules.get(full_name)
    if current is None:
        return None
    for (owner, other_id, other_module), loaded in _PLUGIN_MODULES.items():
        if (owner is not registry and other_id == plugin_id and other_module == module and loaded.name == full_name
                and loaded.module is current and (digest is None or loaded.digest == digest)):
            return loaded
    return None


def _usable(registry: Registry, loaded: _LoadedModule | None, full_name: str, digest: str | None) -> bool:
    return (loaded is not None and loaded.name == full_name and sys.modules.get(full_name) is loaded.module
            and (digest is None or loaded.digest == digest) and _still_registered(registry, loaded))


def _current(registry: Registry, plugin_id: str, scope: ImportScope | None, modules: Sequence[str],
             digest: str) -> bool:
    for module in modules:
        full_name = _plugin_module_name(scope, module)
        if _usable(registry, _PLUGIN_MODULES.get((registry, plugin_id, module)), full_name, digest):
            continue
        if _shared_import(registry, plugin_id, module, full_name, digest) is None:
            return False
    return True


def _plugin_sources(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        found.extend(Path(current) / name for name in sorted(files) if name.endswith(".py"))
    return found


def _discard_bytecode(source: Path) -> None:
    try:
        os.unlink(importlib.util.cache_from_source(str(source)))
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS):
            raise


def _modules_under(root: Path, sources: Sequence[Path]) -> list[str]:
    basenames = {source.name for source in sources}
    found: list[str] = []
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if not isinstance(location, str) or os.path.basename(location) not in basenames:
            continue
        resolved = _file_of(module)
        if resolved is not None and resolved.is_relative_to(root):
            found.append(name)
    return found


def _refresh_plugin(registry: Registry, plugin_id: str, root: Path, scope: ImportScope | None,
                    modules: Sequence[str]) -> None:
    names: set[str] = set()
    for key in [key for key in _PLUGIN_MODULES if key[0] is registry and key[1] == plugin_id]:
        loaded = _PLUGIN_MODULES.pop(key)
        for owner in (loaded.name, *loaded.sources):
            registry.drop_module(owner, plugin_id)
        names.update((loaded.name, *loaded.sources))
    location = root.resolve()
    sources = _plugin_sources(location)
    for source in sources:
        _discard_bytecode(source)
        linecache.checkcache(str(source))
    stale = {*_modules_under(location, sources), *(_plugin_module_name(scope, module) for module in modules)}
    for name in stale:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    _drop_scoped(names | stale)


def _import_plugin_module(plugin_id: str, scope: ImportScope | None, module: str, registry: Registry,
                          digest: str | None = None) -> None:
    full_name = _plugin_module_name(scope, module)
    key = (registry, plugin_id, module)
    loaded = _PLUGIN_MODULES.get(key)
    if _usable(registry, loaded, full_name, digest):
        return
    state = registry.snapshot()
    _PLUGIN_MODULES.pop(key, None)
    owners = (*(loaded.sources if loaded is not None else ()), full_name)
    for owner in owners:
        registry.drop_module(owner, plugin_id)
    shared = _shared_import(registry, plugin_id, module, full_name, digest)
    try:
        if shared is not None:
            registry.adopt(shared.contributions)
            imported = shared.module
        else:
            imported = _execute_plugin_module(scope, module, full_name, loaded)
    except BaseException:
        for owner in (*owners, *(shared.sources if shared is not None else ())):
            registry.rollback(state, owner, plugin_id)
        raise
    contributions = _contributions(state.tables, registry.snapshot().tables)
    sources = _defining_modules(plugin_id, full_name, contributions)
    _PLUGIN_MODULES[key] = _LoadedModule(full_name, imported, contributions, sources,
                                         _module_files(imported, sources), digest)
    if loaded is not None:
        _drop_scoped({loaded.name, *loaded.sources} - {full_name, *sources})


def _file_of(module: types.ModuleType | None) -> Path | None:
    location = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(location, str):
        return None
    try:
        return Path(location).resolve()
    except OSError:
        return None


def _module_files(imported: types.ModuleType, sources: Sequence[str]) -> frozenset[Path]:
    files = {_file_of(imported), *(_file_of(sys.modules.get(source)) for source in sources)}
    return frozenset(path for path in files if path is not None)


def load_plugins(tree: Path | None, *, host: Any | None = None, prune: bool = False) -> list[str]:
    plugin_host: PluginHost = host if host is not None else _default_host()
    return _load_plugins(tree, plugin_host, prune=prune, digests=None)


def _load_plugins(tree: Path | None, plugin_host: PluginHost, *, prune: bool,
                  digests: Mapping[str, str] | None) -> list[str]:
    registry = default_registry()
    loaded: list[str] = []
    with _LOCK:
        _touch(_owner_of(registry))
        records = plugin_host.active(tree)
        if prune:
            manifests = {record.manifest.id: frozenset(record.manifest.modules)
                         for record in records if record.manifest is not None}
            _prune(registry, manifests)
        for record in records:
            manifest = record.manifest
            if manifest is None:
                continue
            register_plugin_root(manifest.id, manifest.root)
            if not manifest.modules:
                continue
            scope = plugin_host.import_scope(record)
            digest = digests.get(manifest.id) if digests is not None else None
            if digest is not None and not _current(registry, manifest.id, scope, manifest.modules, digest):
                try:
                    _refresh_plugin(registry, manifest.id, record.root, scope, manifest.modules)
                except OSError as error:
                    raise DiscoveryError(
                        f"plugin {manifest.id!r} ({manifest.root}): cannot clear its compiled modules before "
                        f"importing it again: {error}") from error
            state = registry.snapshot()
            previous = {module: _PLUGIN_MODULES.get((registry, manifest.id, module)) for module in manifest.modules}
            attempted: list[str] = []
            with contributing(manifest.id, manifest.version):
                for module in manifest.modules:
                    attempted.append(module)
                    try:
                        _import_plugin_module(manifest.id, scope, module, registry, digest)
                    except Exception as error:
                        _rollback_plugin(registry, state, manifest.id, scope, attempted, previous)
                        raise DiscoveryError(
                            f"plugin {manifest.id!r} ({manifest.root}): cannot import module {module!r}: "
                            f"{type(error).__name__}: {error}") from error
            loaded.extend(f"{manifest.id}:{module}" for module in manifest.modules)
    return loaded


def _plugin_of(spec: object) -> str | None:
    provenance = getattr(spec, "provenance", None)
    return provenance.plugin if isinstance(provenance, Provenance) else None


def _prune(registry: Registry, active: Mapping[str, frozenset[str]]) -> None:
    keep = {BUILTIN_PLUGIN, PROJECT_PLUGIN, *active}
    state = registry.snapshot()
    tables: dict[str, dict[Any, Any]] = {}
    dropped = False
    for table, rows in state.tables.items():
        kept: dict[Any, Any] = {}
        for key, spec in rows.items():
            plugin = _plugin_of(spec)
            if plugin is not None and plugin not in keep:
                dropped = True
            else:
                kept[key] = spec
        tables[table] = kept
    if dropped:
        registry.restore(RegistryState(tables))
    names: set[str] = set()
    stale = [key for key in _PLUGIN_MODULES if key[0] is registry and key[1] not in keep]
    for key in stale:
        loaded = _PLUGIN_MODULES.pop(key)
        names.update((loaded.name, *loaded.sources))
    removed = [key for key in _PLUGIN_MODULES
               if key[0] is registry and key[1] in active and key[2] not in active[key[1]]]
    for key in removed:
        loaded = _PLUGIN_MODULES.pop(key)
        for owner in (loaded.name, *loaded.sources):
            registry.drop_module(owner, key[1])
        names.update((loaded.name, *loaded.sources))
    _drop_scoped(names)
    if dropped or stale or removed:
        _run_release_hooks([registry])


def _same_tables(left: Mapping[str, Mapping[Any, Any]], right: Mapping[str, Mapping[Any, Any]]) -> bool:
    if left.keys() != right.keys():
        return False
    for table, rows in left.items():
        other = right[table]
        if rows.keys() != other.keys() or any(other[key] is not spec for key, spec in rows.items()):
            return False
    return True


def builtins_registry() -> Registry:
    tables = {table: {key: spec for key, spec in rows.items() if _plugin_of(spec) == BUILTIN_PLUGIN}
              for table, rows in base_registry().snapshot().tables.items()}
    with _LOCK:
        current = _BUILTINS.registry
        if current is not None and _same_tables(_BUILTINS.tables, tables):
            return current
        registry = Registry()
        registry.restore(RegistryState({table: dict(rows) for table, rows in tables.items()}))
        _BUILTINS.registry = registry
        _BUILTINS.tables = tables
        return registry


def tree_registry(tree: Path | None, *, plugins: bool = True, isolated: bool = False) -> Registry:
    root = tree.expanduser().resolve() if tree is not None else None
    with _LOCK:
        key = (root, plugins, isolated)
        registry = _TREES.get(key)
        if registry is None:
            registry = Registry()
            _TREES[key] = registry
            _TREE_OF[registry] = root
        _touch(_TreeKey(root))
        registry.rebase(builtins_registry() if isolated else base_registry())
        return registry


def forget_file(path: Path) -> None:
    target = path.expanduser().resolve()
    name = module_name(target)
    with _LOCK:
        touched: dict[Registry, None] = {}
        for key in [key for key in _FILES if key[1] == name]:
            del _FILES[key]
            if key[0] in _FROZEN:
                continue
            key[0].drop_module(name)
            touched[key[0]] = None
        stale_modules: set[str] = {name}
        for key, loaded in list(_PLUGIN_MODULES.items()):
            if target not in loaded.files:
                continue
            registry, plugin_id, _module = key
            for owner in (loaded.name, *loaded.sources):
                registry.drop_module(owner, plugin_id)
            del _PLUGIN_MODULES[key]
            touched[registry] = None
            stale_modules.update((loaded.name, *loaded.sources))
        stale_modules.update(_modules_of_file(target))
        for module in stale_modules:
            sys.modules.pop(module, None)
        linecache.checkcache(str(target))
        if touched:
            _run_release_hooks(list(touched))


def forget_tree(tree: Path) -> None:
    root = tree.expanduser().resolve()
    with _LOCK:
        _evict(_TreeKey(root))


def _modules_of_file(target: Path) -> list[str]:
    found: list[str] = []
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if isinstance(location, str) and os.path.basename(location) == target.name and _file_of(module) == target:
            found.append(name)
    return found


def _owner_of(registry: Registry) -> _TreeKey | Registry | None:
    if registry is base_registry():
        return None
    if registry in _TREE_OF:
        return _TreeKey(_TREE_OF[registry])
    return registry


def _touch(owner: _TreeKey | Registry | None) -> None:
    if owner is None:
        return
    _OWNERS[owner] = None
    _OWNERS.move_to_end(owner)
    while len(_OWNERS) > _CACHE_CAPACITY:
        oldest = next(iter(_OWNERS))
        _evict(oldest)


def _evict(owner: _TreeKey | Registry) -> None:
    _OWNERS.pop(owner, None)
    if isinstance(owner, Registry):
        _release([owner])
        return
    for key in [key for key in _TREES if key[0] == owner.root]:
        _TREES.pop(key)
    registries = [registry for registry, root in _TREE_OF.items() if root == owner.root]
    for registry in registries:
        _TREE_OF.pop(registry, None)
    if registries:
        _release(registries)


def _publish(working: Registry, root: Path | None, published: Registry | None = None) -> Registry:
    with _LOCK:
        if published is None:
            published = Registry()
            published.rebase(builtins_registry())
            published.restore(working.snapshot())
            _FROZEN.add(published)
        _OWNERS.pop(published, None)
        _TREE_OF[published] = root
        for (owner, name), loaded in list(_FILES.items()):
            if owner is working and (published, name) not in _FILES and _intact(published, name, loaded):
                _FILES[(published, name)] = loaded
        _touch(_TreeKey(root))
        return published


def _retire(published: Registry) -> None:
    with _LOCK:
        if published not in _FROZEN:
            return
        _TREE_OF.pop(published, None)
        _OWNERS.pop(published, None)
        _release([published])


def _release(registries: Sequence[Registry]) -> None:
    chosen = set(registries)
    entry_names = [key[1] for key in _FILES if key[0] in chosen]
    for key in [key for key in _FILES if key[0] in chosen]:
        del _FILES[key]
    plugin_names: set[str] = set()
    for key in [key for key in _PLUGIN_MODULES if key[0] in chosen]:
        loaded = _PLUGIN_MODULES.pop(key)
        plugin_names.update((loaded.name, *loaded.sources))
    _drop_unused_entry_modules(entry_names)
    _drop_scoped(plugin_names)
    _run_release_hooks(registries)


def _scope_of(name: str) -> str:
    return name.partition(".")[0]


def _drop_scoped(names: Iterable[str]) -> None:
    scoped = {name for name in names if SCOPED_MODULE_PATTERN.match(name) is not None}
    if not scoped:
        return
    used = {name for loaded in _PLUGIN_MODULES.values() for name in (loaded.name, *loaded.sources)}
    for name in scoped - used:
        sys.modules.pop(name, None)
    stale = {_scope_of(name) for name in scoped} - {_scope_of(name) for name in used}
    if stale:
        for name in [name for name in sys.modules if _scope_of(name) in stale]:
            sys.modules.pop(name, None)


def _drop_unused_entry_modules(names: Iterable[str]) -> None:
    used = {key[1] for key in _FILES}
    for name in set(names) - used:
        sys.modules.pop(name, None)


def _run_release_hooks(registries: Sequence[Registry]) -> None:
    for hook in _RELEASE_HOOKS:
        hook(registries)


def legacy_backends(tree: Path | None, *, host: Any | None = None) -> list[str]:
    plugin_host: PluginHost = host if host is not None else _default_host()
    return sorted({contribution.id for contribution in plugin_host.contributions("backend", tree)})


def _rollback_plugin(registry: Registry, state: RegistryState, plugin_id: str, scope: ImportScope | None,
                     modules: Sequence[str], previous: Mapping[str, _LoadedModule | None]) -> None:
    owners: set[str] = set()
    for module in modules:
        owners.add(_plugin_module_name(scope, module))
        for loaded in (previous.get(module), _PLUGIN_MODULES.get((registry, plugin_id, module))):
            if loaded is not None:
                owners.add(loaded.name)
                owners.update(loaded.sources)
    for owner in sorted(owners):
        registry.rollback(state, owner, plugin_id)
    for module in modules:
        earlier = previous.get(module)
        if earlier is None:
            _PLUGIN_MODULES.pop((registry, plugin_id, module), None)
        else:
            _PLUGIN_MODULES[(registry, plugin_id, module)] = earlier


def _harness_dirs(root: Path) -> Iterator[Path]:
    for directory in _walk(root):
        harness = directory / HARNESS_DIR
        if (harness / APP_FILE).is_file():
            yield harness


def _declares_name(harness: Path) -> str | None:
    try:
        data = tomllib.loads((harness / APP_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    app = data.get("app")
    declared = app.get("name") if isinstance(app, dict) else None
    return str(declared) if declared else None


def find_app(tree: Path, name: str) -> AppInfo | None:
    from eks_harness.drivers.profile import ProfileError, load_profile

    root = tree.expanduser().resolve()
    by_name: list[AppInfo] = []
    by_directory: list[AppInfo] = []
    seen: set[Path] = set()
    for harness in _harness_dirs(root):
        directory = harness.parent
        if directory in seen:
            continue
        seen.add(directory)
        declared = _declares_name(harness)
        try:
            profile = load_profile(harness)
        except ProfileError as error:
            if name in (declared, directory.name):
                raise DiscoveryError(f"app {name!r}: {error}") from error
            continue
        own = profile.name if declared is not None else directory.name
        info = AppInfo(name=own, root=directory, platforms=tuple(profile.platforms), tables=profile.raw)
        if declared is not None and own == name:
            by_name.append(info)
        elif directory.name == name:
            by_directory.append(info)
    for matches in (by_name, by_directory):
        if len(matches) > 1:
            roots = ", ".join(str(info.root) for info in matches)
            raise DiscoveryError(f"several apps are named {name!r} ({roots}); give them distinct [app] names")
        if matches:
            return matches[0]
    return None


__all__ = [
    "APP_FILE",
    "ENTRY_FOLDERS",
    "HARNESS_DIR",
    "MODULE_PREFIX",
    "SKIP_DIRS",
    "SPEC_SEPARATOR",
    "AppInfo",
    "DiscoveryError",
    "EntryFile",
    "builtins_registry",
    "discover",
    "find_app",
    "find_tree",
    "forget_file",
    "forget_tree",
    "legacy_backends",
    "load_file",
    "load_plugins",
    "load_tree",
    "module_name",
    "resolve_entry",
    "split_spec",
    "sync_tree",
    "tree_registry",
]
