from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "harness-plugin.toml"
API_VERSION = "1"
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")
CONTRIBUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")

CONTRIBUTION_TYPES: dict[str, str] = {
    "driver": "controls a target (browser, simulator, emulator, device, desktop app)",
    "driver_extension": "a JS module loaded into a driver worker that adds script helpers (login, app navigation)",
    "flow_helpers": "a Python class mixed into flow apps for a platform (app menus, login screens)",
    "platform": "a kind of target and its device pool",
    "device_pool": "a pool of devices or browsers for a platform",
    "app_profile": "how to build, install, launch and identify an app",
    "backend": "a service stack an app talks to",
    "backend_policy": "picks a backend for a tree (local, staging, ...)",
    "seeder": "fills a backend with a scenario",
    "credentials": "personas and secrets for logins",
    "login": "logs a driver session in as a persona",
    "fake": "fake inputs a driver can arm (camera, NFC, documents, location)",
    "capture": "a capture kind (screenshot, recording, dump)",
    "annotator": "draws annotations into captures",
    "viewer": "an artifact viewer for the UI",
    "effect": "a video effect",
    "transition": "a video transition",
    "easing": "an easing curve",
    "marker_source": "extracts markers (beats, words, scenes) from media",
    "captioner": "speech to text for captions",
    "media_provider": "resolves media references",
    "media_renderer": "renders a media kind (Blender, web scene, device take)",
    "scene_engine": "runs scenes on a clock",
    "score_track": "a score track type",
    "encoder": "a video encoder",
    "node_capability": "a capability a node can offer",
    "node_job": "a job kind nodes can run (frame ranges, scene previews, builds)",
    "mcp_tools": "MCP tools",
    "mcp_resources": "MCP resources",
    "cli": "CLI commands",
    "api": "HTTP routes mounted under /api/plugins/<plugin>",
    "ui": "a UI module for a slot",
    "skill": "a Claude Code skill",
    "hook": "a Claude Code hook",
    "agent": "a Claude Code agent",
    "template": "a plugin scaffold",
}

ENTRY_TYPES = frozenset(CONTRIBUTION_TYPES) - {"ui", "skill", "hook", "agent", "template", "viewer", "driver_extension"}
PATH_TYPES = frozenset({"skill", "hook", "agent", "template", "driver_extension"})
UI_SLOTS = frozenset({
    "nav.section", "route", "project.tab", "session.panel", "artifact.viewer", "device.controls",
    "backend.inspector", "node.inspector", "score.track", "score.inspector", "settings.page", "command",
})
SETTING_TYPES = frozenset({"str", "int", "float", "bool", "path", "secret", "list[str]", "json"})


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str
    default: Any = None
    description: str = ""
    choices: tuple[str, ...] | None = None

    def coerce(self, value: Any) -> Any:
        if value is None:
            return None
        if self.type in ("str", "path", "secret"):
            text = str(value)
            if self.choices and text not in self.choices:
                raise ManifestError(f"{self.key}: '{text}' is not one of {', '.join(self.choices)}")
            return text
        if self.type == "int":
            if isinstance(value, bool):
                raise ManifestError(f"{self.key}: expected an integer")
            return int(value)
        if self.type == "float":
            return float(value)
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ManifestError(f"{self.key}: expected a boolean")
        if self.type == "list[str]":
            if not isinstance(value, list | tuple):
                raise ManifestError(f"{self.key}: expected a list")
            return [str(item) for item in value]
        return value


@dataclass(frozen=True)
class Contribution:
    plugin_id: str
    type: str
    id: str
    entry: str | None = None
    path: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.plugin_id}:{self.type}:{self.id}"

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"plugin": self.plugin_id, "type": self.type, "id": self.id}
        if self.entry:
            out["entry"] = self.entry
        if self.path:
            out["path"] = self.path
        out.update({k: v for k, v in self.data.items() if k not in out})
        return out


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    api: str
    root: Path
    description: str = ""
    enabled_by_default: bool = True
    python_requires: tuple[str, ...] = ()
    node_requires: tuple[str, ...] = ()
    bin_requires: tuple[str, ...] = ()
    python_paths: tuple[str, ...] = (".", "src")
    contributions: tuple[Contribution, ...] = ()
    settings: tuple[SettingSpec, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def file(self) -> Path:
        return self.root / MANIFEST_NAME

    def of_type(self, kind: str) -> list[Contribution]:
        return [c for c in self.contributions if c.type == kind]

    def setting(self, key: str) -> SettingSpec | None:
        return next((s for s in self.settings if s.key == key), None)

    def defaults(self) -> dict[str, Any]:
        return {s.key: s.default for s in self.settings}

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for c in self.contributions:
            counts[c.type] = counts.get(c.type, 0) + 1
        return {"id": self.id, "name": self.name, "version": self.version, "api": self.api,
                "description": self.description, "enabledByDefault": self.enabled_by_default,
                "contributes": counts}


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where} must be a list of strings")
    return tuple(value)


def _contribution(plugin_id: str, kind: str, index: int, item: Any) -> Contribution:
    where = f"contributes.{kind}[{index}]"
    if not isinstance(item, dict):
        raise ManifestError(f"{where} must be a table")
    data = dict(item)
    entry = data.pop("entry", None)
    path = data.pop("path", None)
    cid = data.pop("id", None)
    if kind == "ui":
        slot = data.get("slot")
        if slot not in UI_SLOTS:
            raise ManifestError(f"{where}.slot must be one of {', '.join(sorted(UI_SLOTS))}")
        if not isinstance(data.get("module"), str):
            raise ManifestError(f"{where}.module is required (a built ES module path)")
        cid = cid or f"{slot}:{Path(data['module']).stem}"
    elif kind == "viewer":
        if not isinstance(data.get("module"), str):
            raise ManifestError(f"{where}.module is required")
        cid = cid or Path(data["module"]).stem
    elif kind in PATH_TYPES:
        if not isinstance(path, str) or not path:
            raise ManifestError(f"{where}.path is required")
        cid = cid or Path(path).name
    if kind in ENTRY_TYPES:
        if not isinstance(entry, str) or ":" not in entry:
            raise ManifestError(f"{where}.entry must be 'module:attribute'")
    if not isinstance(cid, str) or not CONTRIBUTION_ID.fullmatch(cid):
        raise ManifestError(f"{where}.id is missing or invalid")
    return Contribution(plugin_id=plugin_id, type=kind, id=cid, entry=entry, path=path, data=data)


def _settings(raw: Any) -> tuple[SettingSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ManifestError("[config] must be a table of settings")
    specs = []
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            spec = {"type": type(spec).__name__ if not isinstance(spec, list) else "list[str]", "default": spec}
            spec["type"] = {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(spec["type"], spec["type"])
        kind = spec.get("type", "str")
        if kind not in SETTING_TYPES:
            raise ManifestError(f"config.{key}.type must be one of {', '.join(sorted(SETTING_TYPES))}")
        choices = spec.get("choices")
        specs.append(SettingSpec(key=key, type=kind, default=spec.get("default"),
                                 description=str(spec.get("description", "")),
                                 choices=tuple(choices) if choices else None))
    return tuple(specs)


def parse_manifest(data: dict[str, Any], root: Path) -> Manifest:
    plugin = data.get("plugin")
    if not isinstance(plugin, dict):
        raise ManifestError("[plugin] table is required")
    pid = plugin.get("id")
    if not isinstance(pid, str) or not PLUGIN_ID.fullmatch(pid):
        raise ManifestError("plugin.id must be lower case, dot separated (for example 'acme.backend')")
    api = str(plugin.get("api", API_VERSION))
    if api.split(".", 1)[0] != API_VERSION:
        raise ManifestError(f"plugin.api {api} is not supported (this harness speaks {API_VERSION})")
    requires = data.get("requires") or {}
    if not isinstance(requires, dict):
        raise ManifestError("[requires] must be a table")
    contributes = data.get("contributes") or {}
    if not isinstance(contributes, dict):
        raise ManifestError("[contributes] must be a table")
    items: list[Contribution] = []
    for kind, entries in contributes.items():
        if kind not in CONTRIBUTION_TYPES:
            raise ManifestError(f"unknown contribution type '{kind}'")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            raise ManifestError(f"contributes.{kind} must be an array of tables")
        for index, item in enumerate(entries):
            items.append(_contribution(pid, kind, index, item))
    seen: set[tuple[str, str]] = set()
    for item in items:
        if (item.type, item.id) in seen:
            raise ManifestError(f"duplicate {item.type} contribution '{item.id}'")
        seen.add((item.type, item.id))
    python_paths = _str_list(plugin.get("python_path"), "plugin.python_path") or (".", "src")
    return Manifest(
        id=pid,
        name=str(plugin.get("name") or pid),
        version=str(plugin.get("version", "0.0.0")),
        api=api,
        root=root,
        description=str(plugin.get("description", "")),
        enabled_by_default=bool(plugin.get("enabled_by_default", True)),
        python_requires=_str_list(requires.get("python"), "requires.python"),
        node_requires=_str_list(requires.get("nodes"), "requires.nodes"),
        bin_requires=_str_list(requires.get("bin"), "requires.bin"),
        python_paths=python_paths,
        contributions=tuple(items),
        settings=_settings(data.get("config")),
        raw=data,
    )


def load_manifest(path: Path) -> Manifest:
    file = path / MANIFEST_NAME if path.is_dir() else path
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"{file} does not exist") from error
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"{file}: {error}") from error
    try:
        return parse_manifest(data, file.parent.resolve())
    except ManifestError as error:
        raise ManifestError(f"{file}: {error}") from error
