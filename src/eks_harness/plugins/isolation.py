from __future__ import annotations

import builtins
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
import threading
import types
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCOPE_PREFIX = "_eks_harness_plugin_"
RESERVED_NAMES = frozenset({"eks_harness"})
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "site-packages", "dist", "build"})
_SLUG = re.compile(r"[^A-Za-z0-9]+")


def module_names(directory: Path, *, regular_only: bool = False) -> set[str]:
    names: set[str] = set()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return names
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in SKIP_DIRS:
            continue
        if entry.is_file() and entry.suffix == ".py" and entry.stem.isidentifier():
            names.add(entry.stem)
        elif entry.is_dir() and name.isidentifier() and (
                (entry / "__init__.py").is_file() or (not regular_only and _holds_python(entry))):
            names.add(name)
    return names - RESERVED_NAMES


def _holds_python(directory: Path) -> bool:
    try:
        return any(child.suffix == ".py" and child.is_file() for child in directory.iterdir())
    except OSError:
        return False


def _spec_location(spec: importlib.machinery.ModuleSpec | None) -> Path | None:
    if spec is None:
        return None
    if spec.origin and spec.has_location:
        return Path(spec.origin)
    locations = list(spec.submodule_search_locations or [])
    return Path(locations[0]) if locations else None


@dataclass(eq=False)
class ImportScope:
    plugin_id: str
    root: Path
    generation: str
    prefix: str
    search_paths: tuple[Path, ...]
    top_names: frozenset[str]
    regular_names: frozenset[str] = frozenset()
    builtins: dict[str, Any] = field(default_factory=dict, repr=False)

    def owns(self, name: str) -> bool:
        return name.partition(".")[0] in self.top_names

    def contains(self, location: Path) -> bool:
        try:
            resolved = location.resolve()
        except OSError:
            return False
        return any(resolved == base or resolved.is_relative_to(base) for base in self.search_paths)

    def scoped_name(self, name: str) -> str:
        return f"{self.prefix}.{name}"

    def import_module(self, name: str) -> types.ModuleType:
        if self.owns(name):
            return importlib.import_module(self.scoped_name(name))
        return importlib.import_module(name)

    def scoped_import(self, name: str, globals: Any = None, locals: Any = None, fromlist: Sequence[str] = (),
                      level: int = 0) -> types.ModuleType:
        if level != 0 or not self.owns(name):
            return builtins.__import__(name, globals, locals, fromlist, level)
        module = builtins.__import__(self.scoped_name(name), globals, locals, fromlist, 0)
        if fromlist:
            return module
        return sys.modules[self.scoped_name(name.partition(".")[0])]


class _ScopedLoader(importlib.abc.Loader):
    def __init__(self, loader: Any, scope: ImportScope) -> None:
        self._loader = loader
        self._scope = scope

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        return self._loader.create_module(spec)

    def exec_module(self, module: types.ModuleType) -> None:
        module.__dict__["__builtins__"] = self._scope.builtins
        self._loader.exec_module(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _ScopeRootLoader(importlib.abc.Loader):
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        return None


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, scope: ImportScope, name: str) -> None:
        self._scope = scope
        self._name = name
        self._target_spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        target = importlib.import_module(self._scope.scoped_name(self._name))
        self._target_spec = target.__spec__
        return target

    def exec_module(self, module: types.ModuleType) -> None:
        module.__spec__ = self._target_spec


class ScopeRegistry(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_prefix: dict[str, ImportScope] = {}
        self._latest: dict[tuple[str, Path], ImportScope] = {}
        self._installed = False

    def scope(self, plugin_id: str, root: Path, generation: str, python_paths: Iterable[str]) -> ImportScope:
        root = root.resolve()
        prefix = self._prefix(plugin_id, root, generation)
        with self._lock:
            existing = self._by_prefix.get(prefix)
            if existing is not None:
                return existing
            search_paths = tuple(dict.fromkeys(
                path for path in ((root / relative).resolve() for relative in python_paths) if path.is_dir()))
            names: set[str] = set()
            regular: set[str] = set()
            for path in search_paths:
                names |= module_names(path)
                regular |= module_names(path, regular_only=True)
            scope = ImportScope(plugin_id=plugin_id, root=root, generation=generation, prefix=prefix,
                                search_paths=search_paths, top_names=frozenset(names),
                                regular_names=frozenset(regular))
            scope.builtins = {**builtins.__dict__, "__import__": scope.scoped_import}
            self._by_prefix[prefix] = scope
            self._latest[(plugin_id, root)] = scope
            self._install()
            return scope

    def scopes(self) -> list[ImportScope]:
        with self._lock:
            return list(self._latest.values())

    def find_spec(self, fullname: str, path: Sequence[str] | None = None,
                  target: types.ModuleType | None = None) -> importlib.machinery.ModuleSpec | None:
        if fullname.startswith(SCOPE_PREFIX):
            return self._scoped_spec(fullname, path, target)
        return self._alias_spec(fullname, path, target)

    def _scoped_spec(self, fullname: str, path: Sequence[str] | None,
                     target: types.ModuleType | None) -> importlib.machinery.ModuleSpec | None:
        prefix, _, rest = fullname.partition(".")
        with self._lock:
            scope = self._by_prefix.get(prefix)
        if scope is None:
            return None
        if not rest:
            spec = importlib.machinery.ModuleSpec(prefix, _ScopeRootLoader(), is_package=True)
            spec.submodule_search_locations = [str(p) for p in scope.search_paths]
            return spec
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _ScopedLoader(spec.loader, scope)
        return spec

    def _alias_spec(self, fullname: str, path: Sequence[str] | None,
                    target: types.ModuleType | None) -> importlib.machinery.ModuleSpec | None:
        top = fullname.partition(".")[0]
        with self._lock:
            candidates = [s for s in self._latest.values() if top in s.top_names]
        if not candidates:
            return None
        location = _spec_location(importlib.machinery.PathFinder.find_spec(fullname, path, target))
        if location is not None:
            owners = [s for s in candidates if s.contains(location)]
        elif path is None and len({(s.plugin_id, s.root) for s in candidates}) == 1:
            owners = [s for s in candidates if top in s.regular_names]
        else:
            owners = []
        if not owners:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(owners[-1], fullname))

    def _install(self) -> None:
        if self._installed and self in sys.meta_path:
            return
        sys.meta_path.insert(0, self)
        self._installed = True

    @staticmethod
    def _prefix(plugin_id: str, root: Path, generation: str) -> str:
        digest = hashlib.sha256(f"{plugin_id}\0{root}\0{generation}".encode()).hexdigest()[:12]
        return f"{SCOPE_PREFIX}{_SLUG.sub('_', plugin_id).strip('_')}_{digest}"


REGISTRY = ScopeRegistry()
