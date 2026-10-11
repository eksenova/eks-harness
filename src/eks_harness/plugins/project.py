from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARNESS_DIR = ".harness"
PROJECT_FILE = "project.toml"
LOCAL_FILE = "project.local.toml"
MAX_EXPANSION_DEPTH = 8
_PLACEHOLDER = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>(?:[^{}]|\{[^{}]*\})*))?\}")


class ProjectConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    tree: Path | None = None
    project_id: str | None = None
    name: str | None = None
    tags: tuple[str, ...] = ()
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    trust: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    git: tuple[str, ...] = ()
    variables: dict[str, str] = field(default_factory=dict)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def harness_dir(self) -> Path | None:
        return self.tree / HARNESS_DIR if self.tree else None

    @property
    def plugins_dir(self) -> Path | None:
        return self.tree / HARNESS_DIR / "plugins" if self.tree else None

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        return dict(self.settings.get(plugin_id) or {})

    def wants(self, plugin_id: str, enabled_by_default: bool) -> bool:
        if plugin_id in self.disable:
            return False
        return enabled_by_default or plugin_id in self.enable


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProjectConfigError(f"{where} must be a list of strings")
    return tuple(value)


def git_toplevel(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def main_tree(tree: Path) -> Path:
    try:
        out = subprocess.run(["git", "-C", str(tree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return tree
    common = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    if common is None or common.name != ".git":
        return tree
    return common.parent


def expand_placeholders(text: str, values: dict[str, str], depth: int = 0) -> str:
    if depth > MAX_EXPANSION_DEPTH:
        raise ProjectConfigError(f"'{text}': placeholders nest too deeply (a variable refers to itself?)")

    def replace(match: re.Match[str]) -> str:
        name, default = match["name"], match["default"]
        value = values.get(name) if name in values else os.environ.get(name)
        if value:
            return expand_placeholders(value, values, depth + 1)
        if default is not None:
            return expand_placeholders(default, values, depth + 1)
        return match.group(0)

    return _PLACEHOLDER.sub(replace, text)


def resolve_plugin_path(text: str, tree: Path, variables: dict[str, str] | None = None) -> Path:
    values = {**(variables or {}), "tree": str(tree)}
    if "${" in text:
        values["main_tree"] = str(main_tree(tree))
    path = Path(expand_placeholders(text, values)).expanduser()
    if path.is_absolute():
        return path.resolve()
    from_tree = (tree / path).resolve()
    legacy = (tree / HARNESS_DIR / path).resolve()
    return legacy if not from_tree.exists() and legacy.exists() else from_tree


def find_tree(start: Path) -> Path | None:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / HARNESS_DIR / PROJECT_FILE).is_file():
            return candidate
    return git_toplevel(start)


def load_project_config(tree: Path | None) -> ProjectConfig:
    if tree is None:
        return ProjectConfig()
    tree = tree.resolve()
    file = tree / HARNESS_DIR / PROJECT_FILE
    if not file.is_file():
        return ProjectConfig(tree=tree)
    data = _read_toml(file)
    local = tree / HARNESS_DIR / LOCAL_FILE
    shared = _variables(data, file)
    variables = {**shared, **{k: os.environ[k] for k in shared if os.environ.get(k)},
                 **(_variables(_read_toml(local), local) if local.is_file() else {})}
    project = data.get("project") or {}
    plugins = data.get("plugins") or {}
    if not isinstance(project, dict) or not isinstance(plugins, dict):
        raise ProjectConfigError(f"{file}: [project] and [plugins] must be tables")
    settings = plugins.get("settings") or {}
    if not isinstance(settings, dict) or not all(isinstance(v, dict) for v in settings.values()):
        raise ProjectConfigError(f"{file}: [plugins.settings.<id>] must be tables")
    return ProjectConfig(
        tree=tree,
        project_id=project.get("id"),
        name=project.get("name"),
        tags=_strings(project.get("tags"), "project.tags"),
        enable=_strings(plugins.get("enable"), "plugins.enable"),
        disable=_strings(plugins.get("disable"), "plugins.disable"),
        trust=_strings(plugins.get("trust"), "plugins.trust"),
        paths=tuple(resolve_plugin_path(p, tree, variables)
                    for p in _strings(plugins.get("paths"), "plugins.paths")),
        git=_strings(plugins.get("git"), "plugins.git"),
        variables=variables,
        settings={k: dict(v) for k, v in settings.items()},
        raw=data,
    )


def _read_toml(file: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ProjectConfigError(f"{file}: {error}") from error


def _variables(data: dict[str, Any], file: Path) -> dict[str, str]:
    table = data.get("vars") or {}
    if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
        raise ProjectConfigError(f"{file}: [vars] must map names to strings")
    return dict(table)
