from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class ProjectSettingError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectSetting:
    key: str
    default: Any
    type: str
    label: str
    description: str
    group: str


GROUPS: dict[str, str] = {
    "capture": "Capture",
    "apps": "Apps under test",
    "devices": "Devices",
}

_SETTINGS: list[ProjectSetting] = [
    ProjectSetting("capture.hideSelectors", [], "list[str]", "Hidden selectors",
                   "CSS selectors hidden in every web screenshot and recording of this project, for volatile widgets "
                   "such as dev tool overlays. Never hide [aria-live] regions.", "capture"),
    ProjectSetting("apps.iosBundleIds", [], "list[str]", "iOS app bundle ids",
                   "Apps terminated and removed from a simulator when a lease of this project is released or taken "
                   "over, with the simulator keychain reset.", "apps"),
    ProjectSetting("apps.androidPackages", [], "list[str]", "Android app packages",
                   "Apps removed from an emulator when a lease of this project is released or taken over.", "apps"),
    ProjectSetting("devices.strayProcessPatterns", [], "list[str]", "Stray process patterns",
                   "Command line fragments of this project's app tooling (dev servers, drivers). A matching process "
                   "that no lease owns counts as stray, and the device sweep keeps retired simulators while one runs.",
                   "devices"),
]

SETTINGS: dict[str, ProjectSetting] = {setting.key: setting for setting in _SETTINGS}
DEFAULTS: dict[str, Any] = {setting.key: setting.default for setting in _SETTINGS}


def coerce(key: str, value: Any) -> Any:
    setting = SETTINGS.get(key)
    if setting is None:
        raise ProjectSettingError(f"unknown project setting {key}; known: {', '.join(SETTINGS)}")
    if setting.type == "list[str]":
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ProjectSettingError(f"{key} must be a JSON list") from error
            else:
                value = [part for part in (line.strip() for line in text.replace(",", "\n").splitlines()) if part]
        if not isinstance(value, list) or not all(isinstance(item, (str, int, float)) for item in value):
            raise ProjectSettingError(f"{key} must be a list of strings")
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    raise ProjectSettingError(f"{key} has an unsupported type {setting.type}")


def parse(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key in SETTINGS:
            try:
                clean[key] = coerce(key, value)
            except ProjectSettingError:
                continue
    return clean


def merge(stored: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    result = dict(stored)
    for key, value in changes.items():
        if value is None:
            if key not in SETTINGS:
                raise ProjectSettingError(f"unknown project setting {key}; known: {', '.join(SETTINGS)}")
            result.pop(key, None)
        else:
            result[key] = coerce(key, value)
    return result


def effective(stored: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in SETTINGS}}


def dump(stored: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in stored.items() if k in SETTINGS}, ensure_ascii=False, sort_keys=True)


def for_project(conn: sqlite3.Connection, project_id: str | None) -> dict[str, Any]:
    if not project_id:
        return dict(DEFAULTS)
    row = conn.execute("SELECT settings FROM projects WHERE id = ?", (project_id,)).fetchone()
    return effective(parse(row[0] if row else None))


def union(conn: sqlite3.Connection, key: str, project_ids: Iterable[str] | None = None) -> list[str]:
    if project_ids is None:
        rows = conn.execute("SELECT settings FROM projects").fetchall()
    else:
        ids = list(dict.fromkeys(p for p in project_ids if p))
        if not ids:
            return []
        rows = conn.execute(f"SELECT settings FROM projects WHERE id IN ({', '.join('?' for _ in ids)})",
                            ids).fetchall()
    values: list[str] = []
    for row in rows:
        values.extend(effective(parse(row[0])).get(key) or [])
    return list(dict.fromkeys(values))


def describe(stored: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": s.key, "label": s.label, "description": s.description, "type": s.type,
             "group": s.group, "group_title": GROUPS.get(s.group, s.group), "default": s.default,
             "value": stored.get(s.key, s.default), "is_set": s.key in stored} for s in _SETTINGS]
