from __future__ import annotations

import hashlib
import itertools
import os
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from eks.discovery import (
    SKIP_DIRS,
    _entry_files,
    _forget_project_code,
    _harness_parents,
    _load_plugins,
    _publish,
    _retire,
    _sync_files,
    builtins_registry,
    forget_file,
    forget_tree,
    tree_registry,
)
from eks_harness.engine.model import IssueOut
from eks_harness.ir.plan import Plan
from eks_harness.ir.registry import (
    BUILTIN_PLUGIN,
    PROJECT_PLUGIN,
    Registry,
    using_registry,
)
from eks_harness.plugins.host import PluginHost, PluginRecord
from eks_harness.plugins.trust import SKIP_DIRS as HASH_SKIP_DIRS
from eks_harness.plugins.trust import SKIP_SUFFIXES as HASH_SKIP_SUFFIXES
from eks_harness.plugins.trust import content_hash

PRELOADED_KINDS = ("seed", "scene")
TREE_CAPACITY = 32
HARNESS_DIR = ".harness"
PLUGINS_DIR = "plugins"


@dataclass(frozen=True, slots=True)
class PluginState:
    id: str
    version: str
    root: Path
    digest: str
    kind: str


@dataclass(frozen=True)
class TreeRegistry:
    tree: Path | None
    plugins_enabled: bool
    registry: Registry
    plugins: Mapping[str, PluginState] = field(default_factory=dict[str, PluginState])
    project_digest: str | None = None
    generation: int = 0

    def plugin(self, plugin_id: str) -> str | None:
        state = self.plugins.get(plugin_id)
        return state.digest if state is not None else None

    def project(self) -> str | None:
        return self.project_digest

    def roots(self) -> dict[str, Path]:
        return {plugin_id: state.root for plugin_id, state in self.plugins.items()}


_GENERATIONS = itertools.count(1)
_STRIPES = 32

type _Stamp = tuple[tuple[str, int, int, int, int], ...]
type _Signature = tuple[object, ...]
type _Key = tuple[Path | None, bool]
type _File = tuple[str, Path, os.stat_result]


@dataclass(frozen=True, slots=True)
class _Slot:
    entry: TreeRegistry
    working: Registry
    builtins: Registry
    signature: _Signature


def plugin_digest(record: PluginRecord) -> str:
    if record.digest is not None:
        return record.digest
    return content_hash(record.root)


def _python_files(directory: Path, *, exclude: Path | None = None) -> Iterator[tuple[Path, os.stat_result]]:
    pending = [directory]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as scanned:
                items = sorted(scanned, key=lambda item: item.name)
        except OSError:
            continue
        folders: list[Path] = []
        for item in items:
            path = Path(item.path)
            try:
                if item.is_dir():
                    if item.name not in SKIP_DIRS and not item.is_symlink() and path != exclude:
                        folders.append(path)
                elif item.name.endswith(".py") and item.is_file():
                    yield path, item.stat()
            except OSError:
                continue
        pending.extend(reversed(folders))


def _project_files(root: Path, parents: Sequence[Path]) -> list[_File]:
    files: list[_File] = []
    for directory in parents:
        harness = directory / HARNESS_DIR
        files.extend((path.relative_to(root).as_posix(), path, stat)
                     for path, stat in _python_files(harness, exclude=harness / PLUGINS_DIR))
    return sorted(files, key=lambda item: item[0])


def _digest_files(files: Sequence[_File]) -> str:
    digest = hashlib.sha256()
    for relative, path, _stat in files:
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _stamp_of(relative: str, stat: os.stat_result) -> tuple[str, int, int, int, int]:
    return relative, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


def _stamp(files: Sequence[_File]) -> _Stamp:
    return tuple(_stamp_of(relative, stat) for relative, _path, stat in files)


def tree_code_digest(tree: Path) -> str:
    root = tree.expanduser().resolve()
    return _digest_files(_project_files(root, _harness_parents(root)))


def _package_stamp(root: Path) -> _Stamp:
    found: list[tuple[str, int, int, int, int]] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in HASH_SKIP_DIRS)
        here = Path(current)
        for name in sorted(files):
            if name.endswith(HASH_SKIP_SUFFIXES) or name == ".DS_Store":
                continue
            path = here / name
            try:
                found.append(_stamp_of(path.relative_to(root).as_posix(), path.stat()))
            except OSError:
                continue
    return tuple(found)


def _signature(plugins: Mapping[str, PluginState], project: str | None) -> _Signature:
    return (tuple(sorted((state.id, state.version, state.digest, str(state.root), state.kind)
                         for state in plugins.values())), project)


class HubRegistries:
    def __init__(self, plugins: PluginHost, *, capacity: int = TREE_CAPACITY) -> None:
        self.plugins = plugins
        self.capacity = capacity
        self._lock = threading.RLock()
        self._load = threading.RLock()
        self._stripes = tuple(threading.RLock() for _ in range(_STRIPES))
        self._slots: OrderedDict[_Key, _Slot] = OrderedDict()
        self._package_hashes: dict[Path, tuple[_Stamp, str]] = {}
        self._project_hashes: OrderedDict[Path, tuple[_Stamp, str]] = OrderedDict()
        self._synced: weakref.WeakKeyDictionary[Registry, tuple[_Stamp, str]] = weakref.WeakKeyDictionary()

    def _stripes_of(self, *keys: _Key) -> list[threading.RLock]:
        return [self._stripes[index] for index in sorted({hash(key) % _STRIPES for key in keys})]

    def for_tree(self, tree: Path | None, *, plugins: bool = True) -> TreeRegistry:
        root = tree.expanduser().resolve() if tree is not None else None
        key = (root, plugins)
        with ExitStack() as stack:
            for stripe in self._stripes_of(key):
                stack.enter_context(stripe)
            parents = _harness_parents(root) if root is not None else []
            scanned = self._project_digest(root, parents) if root is not None else None
            files = _entry_files(root, parents) if root is not None else []
            with self._load:
                states = self._plugin_states(root) if plugins else {}
                working = tree_registry(root, plugins=plugins, isolated=True)
                with using_registry(working):
                    if plugins:
                        _load_plugins(root, self.plugins, prune=True,
                                      digests={plugin_id: state.digest for plugin_id, state in states.items()})
                    if root is not None and scanned is not None:
                        self._refresh_project(working, root, parents, scanned)
                        _sync_files(root, files, PRELOADED_KINDS)
                if scanned is not None:
                    self._synced[working] = scanned
                return self._settle(key, working, states, scanned[1] if scanned is not None else None)

    def _refresh_project(self, working: Registry, root: Path, parents: Sequence[Path],
                         scanned: tuple[_Stamp, str]) -> None:
        stamp, digest = scanned
        recorded = self._synced.get(working)
        if recorded is not None and recorded[1] == digest:
            return
        if recorded is None:
            changed = sorted({item[0] for item in stamp})
        else:
            changed = sorted({item[0] for item in set(recorded[0]) ^ set(stamp)})
        _forget_project_code(root, parents, [root / relative for relative in changed])

    def _settle(self, key: _Key, working: Registry, states: dict[str, PluginState], project: str | None
                ) -> TreeRegistry:
        root, plugins = key
        builtins = builtins_registry()
        signature = _signature(states, project)
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None and slot.signature == signature and slot.builtins is builtins:
                _publish(working, root, slot.entry.registry)
                slot = _Slot(slot.entry, working, builtins, signature)
            else:
                entry = TreeRegistry(tree=root, plugins_enabled=plugins, registry=_publish(working, root),
                                     plugins=states, project_digest=project, generation=next(_GENERATIONS))
                if slot is not None:
                    _retire(slot.entry.registry)
                slot = _Slot(entry, working, builtins, signature)
            self._slots[key] = slot
            self._slots.move_to_end(key)
            while len(self._slots) > max(self.capacity, 1):
                _evicted, dropped = self._slots.popitem(last=False)
                _retire(dropped.entry.registry)
            return slot.entry

    def _project_digest(self, root: Path, parents: Sequence[Path]) -> tuple[_Stamp, str]:
        files = _project_files(root, parents)
        stamp = _stamp(files)
        with self._lock:
            cached = self._project_hashes.get(root)
            if cached is not None and cached[0] == stamp:
                self._project_hashes.move_to_end(root)
                return cached
        digest = _digest_files(files)
        with self._lock:
            self._project_hashes[root] = (stamp, digest)
            self._project_hashes.move_to_end(root)
            while len(self._project_hashes) > max(self.capacity, 1):
                self._project_hashes.popitem(last=False)
        return stamp, digest

    def _package_digest(self, location: Path) -> str:
        stamp = _package_stamp(location)
        cached = self._package_hashes.get(location)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        digest = content_hash(location)
        self._package_hashes[location] = (stamp, digest)
        return digest

    def _plugin_states(self, root: Path | None) -> dict[str, PluginState]:
        states: dict[str, PluginState] = {}
        for record in self.plugins.active(root):
            manifest = record.manifest
            if manifest is None or not manifest.modules:
                continue
            digest = record.digest if record.digest is not None else self._package_digest(record.root.resolve())
            states[manifest.id] = PluginState(id=manifest.id, version=manifest.version, root=record.root,
                                              digest=digest, kind=record.kind)
        return states

    def _drop(self, root: Path) -> None:
        with self._lock:
            for flag in (True, False):
                slot = self._slots.pop((root, flag), None)
                if slot is not None:
                    _retire(slot.entry.registry)
            self._project_hashes.pop(root, None)

    def invalidate(self, tree: Path, path: Path | None = None) -> None:
        root = tree.expanduser().resolve()
        with ExitStack() as stack:
            for stripe in self._stripes_of((root, True), (root, False)):
                stack.enter_context(stripe)
            with self._load:
                self._drop(root)
                if path is not None:
                    target = path.expanduser()
                    forget_file(target if target.is_absolute() else root / target)

    def forget(self, tree: Path) -> None:
        root = tree.expanduser().resolve()
        with ExitStack() as stack:
            for stripe in self._stripes_of((root, True), (root, False)):
                stack.enter_context(stripe)
            with self._load:
                self._drop(root)
                forget_tree(root)

    def requirement_issues(self, plan: Plan, entry: TreeRegistry) -> list[IssueOut]:
        issues: list[IssueOut] = []
        for requirement in plan.requires:
            if requirement.plugin in (BUILTIN_PLUGIN, PROJECT_PLUGIN):
                continue
            state = entry.plugins.get(requirement.plugin)
            if state is None:
                issues.append(IssueOut(
                    severity="error", code="plugin.missing",
                    message=f"the plan needs plugin {requirement.plugin!r} {requirement.version}, which is not "
                            "loaded; trust and enable it in the project or install it"))
            elif state.version != requirement.version:
                issues.append(IssueOut(
                    severity="error", code="plugin.version",
                    message=f"the plan was built with plugin {requirement.plugin!r} {requirement.version} but "
                            f"{state.version} is loaded; plan it again or load that version"))
        return issues

    def trees(self) -> list[TreeRegistry]:
        with self._lock:
            return [slot.entry for slot in self._slots.values()]


__all__ = [
    "HARNESS_DIR",
    "PLUGINS_DIR",
    "PRELOADED_KINDS",
    "TREE_CAPACITY",
    "HubRegistries",
    "PluginState",
    "TreeRegistry",
    "plugin_digest",
    "tree_code_digest",
]
