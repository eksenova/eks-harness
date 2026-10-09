from __future__ import annotations

import hashlib
import importlib
import inspect
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.config import Config
from eks_harness.paths import Paths
from eks_harness.plugins.discovery import (
    BUILTIN_ROOT,
    Candidate,
    builtin_candidates,
    git_candidates,
    package_candidates,
    path_candidates,
    repo_candidates,
)
from eks_harness.plugins.manifest import Contribution, Manifest, ManifestError
from eks_harness.plugins.project import ProjectConfig, load_project_config
from eks_harness.plugins.trust import TrustStore, content_hash

_LOG = logging.getLogger(__name__)

STATES = ("active", "pending", "changed", "disabled", "error", "shadowed")


class PluginError(RuntimeError):
    pass


@dataclass
class PluginRecord:
    candidate: Candidate
    state: str
    reason: str = ""
    digest: str | None = None
    approved_hash: str | None = None

    @property
    def manifest(self) -> Manifest | None:
        return self.candidate.manifest

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else self.candidate.origin

    @property
    def kind(self) -> str:
        return self.candidate.kind

    @property
    def root(self) -> Path:
        return self.candidate.root

    @property
    def active(self) -> bool:
        return self.state == "active"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "origin": self.candidate.origin,
                               "root": str(self.root), "state": self.state}
        if self.reason:
            out["reason"] = self.reason
        if self.candidate.tree:
            out["tree"] = str(self.candidate.tree)
        if self.digest:
            out["hash"] = self.digest
        if self.manifest:
            out.update(self.manifest.summary())
            out["contributions"] = [c.as_dict() for c in self.manifest.contributions]
            out["settings"] = [{"key": s.key, "type": s.type, "default": s.default, "description": s.description,
                                "choices": list(s.choices) if s.choices else None} for s in self.manifest.settings]
        if self.candidate.error:
            out["error"] = self.candidate.error
        return out


@dataclass
class PluginContext:
    plugin_id: str
    manifest: Manifest
    settings: dict[str, Any]
    paths: Paths
    config: Config
    host: PluginHost
    tree: Path | None = None
    project: ProjectConfig = field(default_factory=ProjectConfig)

    @property
    def root(self) -> Path:
        return self.manifest.root

    @property
    def data_dir(self) -> Path:
        path = self.paths.data_dir / "plugins" / self.plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_dir(self) -> Path:
        path = self.paths.cache_dir / "plugins" / "data" / self.plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def log(self) -> logging.Logger:
        return logging.getLogger(f"eks_harness.plugin.{self.plugin_id}")

    def resolve(self, relative: str) -> Path:
        return (self.manifest.root / relative).resolve()


class PluginHost:
    def __init__(self, paths: Paths, config: Config, *, builtin_root: Path = BUILTIN_ROOT,
                 include_packages: bool = True) -> None:
        self.paths = paths
        self.config = config
        self.builtin_root = builtin_root
        self.include_packages = include_packages
        self.trust_store = TrustStore(paths.config_dir / "plugins-trust.json")
        self._lock = threading.RLock()
        self._global: list[Candidate] | None = None
        self._trees: dict[Path, tuple[float, ProjectConfig, list[Candidate]]] = {}
        self._hashes: dict[Path, tuple[float, str]] = {}
        self._instances: dict[tuple[str, Path | None], Any] = {}
        self._env_paths: set[str] = set()

    def refresh(self) -> None:
        with self._lock:
            self._global = None
            self._trees.clear()
            self._hashes.clear()
            self._instances.clear()

    def _global_candidates(self) -> list[Candidate]:
        with self._lock:
            if self._global is None:
                found = builtin_candidates(self.builtin_root)
                if self.include_packages:
                    found += package_candidates()
                found += path_candidates([Path(p) for p in self.config["plugins.paths"] or []])
                found += git_candidates(self.paths.cache_dir, list(self.config["plugins.git"] or []))
                self._global = found
            return list(self._global)

    def project(self, tree: Path | None) -> ProjectConfig:
        if tree is None:
            return ProjectConfig()
        return self._tree(tree)[1]

    def _tree(self, tree: Path) -> tuple[float, ProjectConfig, list[Candidate]]:
        tree = tree.resolve()
        file = tree / ".harness" / "project.toml"
        plugins_dir = tree / ".harness" / "plugins"
        stamp = max(_mtime(file), _mtime(plugins_dir))
        with self._lock:
            cached = self._trees.get(tree)
            if cached and cached[0] == stamp:
                return cached
            project = load_project_config(tree)
            found = repo_candidates(tree, project.paths)
            found += git_candidates(self.paths.cache_dir, list(project.git), tree=tree)
            entry = (stamp, project, found)
            self._trees[tree] = entry
            return entry

    def _digest(self, root: Path) -> str:
        stamp = _tree_mtime(root)
        cached = self._hashes.get(root)
        if cached and cached[0] == stamp:
            return cached[1]
        digest = content_hash(root)
        self._hashes[root] = (stamp, digest)
        return digest

    def records(self, tree: Path | None = None) -> list[PluginRecord]:
        candidates = self._global_candidates()
        project = ProjectConfig()
        if tree is not None:
            _, project, local = self._tree(tree)
            candidates = candidates + local
        enabled = set(self.config["plugins.enabled"] or [])
        disabled = set(self.config["plugins.disabled"] or [])
        records: list[PluginRecord] = []
        by_id: dict[str, PluginRecord] = {}
        for candidate in candidates:
            record = self._judge(candidate, project, enabled, disabled)
            records.append(record)
            if record.manifest is None:
                continue
            previous = by_id.get(record.id)
            if previous is not None and previous.state != "shadowed":
                previous.state = "shadowed"
                previous.reason = f"overridden by {candidate.origin}"
            by_id[record.id] = record
        return records

    def _judge(self, candidate: Candidate, project: ProjectConfig, enabled: set[str],
               disabled: set[str]) -> PluginRecord:
        manifest = candidate.manifest
        if manifest is None:
            return PluginRecord(candidate, "error", candidate.error or "invalid plugin")
        digest = None
        approved = None
        if not candidate.trusted_by_kind:
            digest = self._digest(candidate.root)
            pre_trusted = candidate.kind == "repo" and candidate.tree is not None and any(
                _same_path(candidate.root, (candidate.tree / ".harness" / "plugins" / name))
                or _same_path(candidate.root, (candidate.tree / ".harness" / name))
                or manifest.id == name
                for name in project.trust)
            if not pre_trusted:
                entry = self.trust_store.get(manifest.id, candidate.root)
                approved = entry.get("hash") if entry else None
                if approved is None:
                    return PluginRecord(candidate, "pending", "waiting for approval", digest)
                if approved != digest:
                    return PluginRecord(candidate, "changed", "changed since it was approved", digest, approved)
        wanted = manifest.enabled_by_default or manifest.id in enabled or manifest.id in project.enable
        if manifest.id in disabled or manifest.id in project.disable or not wanted:
            return PluginRecord(candidate, "disabled", "disabled" if wanted else "off by default", digest, approved)
        missing = [b for b in manifest.bin_requires if shutil.which(b) is None]
        if missing:
            return PluginRecord(candidate, "error", f"missing executables: {', '.join(missing)}", digest, approved)
        return PluginRecord(candidate, "active", "", digest, approved)

    def get(self, plugin_id: str, tree: Path | None = None) -> PluginRecord | None:
        winner = None
        for record in self.records(tree):
            if record.id == plugin_id and record.state != "shadowed":
                winner = record
        return winner

    def active(self, tree: Path | None = None) -> list[PluginRecord]:
        return [r for r in self.records(tree) if r.active]

    def contributions(self, kind: str, tree: Path | None = None) -> list[Contribution]:
        out: list[Contribution] = []
        for record in self.active(tree):
            out.extend(record.manifest.of_type(kind))
        return out

    def contribution(self, kind: str, contribution_id: str, tree: Path | None = None) -> Contribution:
        matches = [c for c in self.contributions(kind, tree) if c.id == contribution_id
                   or f"{c.plugin_id}:{c.id}" == contribution_id]
        if not matches:
            raise PluginError(f"no active plugin contributes {kind} '{contribution_id}'")
        return matches[-1]

    def settings(self, plugin_id: str, tree: Path | None = None) -> dict[str, Any]:
        record = self.get(plugin_id, tree)
        if record is None or record.manifest is None:
            raise PluginError(f"unknown plugin '{plugin_id}'")
        merged = record.manifest.defaults()
        user = (self.config["plugins.settings"] or {}).get(plugin_id) or {}
        merged.update(user)
        merged.update(self.project(tree).plugin_settings(plugin_id))
        out: dict[str, Any] = {}
        for key, value in merged.items():
            spec = record.manifest.setting(key)
            try:
                out[key] = spec.coerce(value) if spec else value
            except (ManifestError, ValueError, TypeError) as error:
                raise PluginError(f"{plugin_id}: setting {key}: {error}") from error
        return out

    def context(self, plugin_id: str, tree: Path | None = None) -> PluginContext:
        record = self.get(plugin_id, tree)
        if record is None or record.manifest is None:
            raise PluginError(f"unknown plugin '{plugin_id}'")
        return PluginContext(plugin_id=plugin_id, manifest=record.manifest, settings=self.settings(plugin_id, tree),
                             paths=self.paths, config=self.config, host=self, tree=tree,
                             project=self.project(tree))

    def trust(self, plugin_id: str, tree: Path | None = None, *, by: str = "") -> dict[str, Any]:
        record = self._pending_or_any(plugin_id, tree)
        if record.candidate.trusted_by_kind:
            return {"plugin": plugin_id, "trusted": True, "reason": f"{record.kind} plugins are trusted"}
        digest = self._digest(record.root)
        entry = self.trust_store.approve(plugin_id, record.root, digest, by=by)
        self._instances.clear()
        return entry

    def untrust(self, plugin_id: str, tree: Path | None = None) -> int:
        record = self._pending_or_any(plugin_id, tree)
        self._instances.clear()
        return self.trust_store.revoke(plugin_id, record.root)

    def _pending_or_any(self, plugin_id: str, tree: Path | None) -> PluginRecord:
        matches = [r for r in self.records(tree) if r.id == plugin_id]
        if not matches:
            raise PluginError(f"unknown plugin '{plugin_id}'")
        return next((r for r in reversed(matches) if r.state != "shadowed"), matches[-1])

    def import_entry(self, contribution: Contribution, tree: Path | None = None) -> Any:
        record = self.get(contribution.plugin_id, tree)
        if record is None or not record.active or record.manifest is None:
            raise PluginError(f"plugin '{contribution.plugin_id}' is not active")
        if not contribution.entry:
            raise PluginError(f"{contribution.key} has no entry")
        self._prepare_imports(record)
        module_name, _, attr = contribution.entry.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            raise PluginError(f"{contribution.key}: cannot import {module_name}: {error}") from error
        target: Any = module
        for part in attr.split("."):
            try:
                target = getattr(target, part)
            except AttributeError as error:
                raise PluginError(f"{contribution.key}: {module_name} has no {attr}") from error
        return target

    def load(self, contribution: Contribution, tree: Path | None = None) -> Any:
        key = (contribution.key, tree.resolve() if tree else None)
        with self._lock:
            if key in self._instances:
                return self._instances[key]
        target = self.import_entry(contribution, tree)
        instance = target
        if inspect.isclass(target):
            context = self.context(contribution.plugin_id, tree)
            instance = _construct(target, context, contribution)
        with self._lock:
            self._instances[key] = instance
        return instance

    def load_all(self, kind: str, tree: Path | None = None) -> list[tuple[Contribution, Any]]:
        out = []
        for contribution in self.contributions(kind, tree):
            try:
                out.append((contribution, self.load(contribution, tree)))
            except PluginError as error:
                _LOG.warning("%s", error)
        return out

    def _prepare_imports(self, record: PluginRecord) -> None:
        manifest = record.manifest
        if record.kind in ("builtin", "package"):
            return
        for relative in manifest.python_paths:
            path = (manifest.root / relative).resolve()
            if path.is_dir() and str(path) not in sys.path:
                sys.path.insert(0, str(path))
        if manifest.python_requires:
            env = self.ensure_requirements(manifest)
            if str(env) not in self._env_paths:
                sys.path.append(str(env))
                self._env_paths.add(str(env))

    def requirements_dir(self, manifest: Manifest) -> Path:
        key = hashlib.sha256("\n".join(sorted(manifest.python_requires)).encode()).hexdigest()[:16]
        return self.paths.cache_dir / "plugins" / "envs" / f"py{sys.version_info.major}{sys.version_info.minor}-{key}"

    def ensure_requirements(self, manifest: Manifest) -> Path:
        target = self.requirements_dir(manifest)
        marker = target / ".installed"
        if marker.exists():
            return target
        uv = shutil.which("uv")
        if uv is None:
            raise PluginError(f"{manifest.id} needs {', '.join(manifest.python_requires)} and uv is not on PATH")
        target.mkdir(parents=True, exist_ok=True)
        out = subprocess.run([uv, "pip", "install", "--quiet", "--python", sys.executable, "--target", str(target),
                              *manifest.python_requires], capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            raise PluginError(f"{manifest.id}: installing requirements failed: {(out.stderr or out.stdout).strip()}")
        marker.write_text("\n".join(manifest.python_requires) + "\n", encoding="utf-8")
        return target


def _construct(cls: type, context: PluginContext, contribution: Contribution) -> Any:
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return cls()
    names = list(params)
    kwargs: dict[str, Any] = {}
    if "context" in params:
        kwargs["context"] = context
    if "contribution" in params:
        kwargs["contribution"] = contribution
    if kwargs:
        return cls(**kwargs)
    required = [p for p in params.values() if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if required and names:
        return cls(context)
    return cls()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tree_mtime(root: Path) -> float:
    newest = _mtime(root)
    for path in root.rglob("*"):
        if any(part in ("node_modules", ".git", "__pycache__", ".venv") for part in path.parts):
            continue
        newest = max(newest, _mtime(path))
    return newest


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def iter_records(records: Iterable[PluginRecord], state: str | None = None) -> list[PluginRecord]:
    return [r for r in records if state is None or r.state == state]
