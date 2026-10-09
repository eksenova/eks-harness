from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import PurePath

from eks_harness.db.common import now

COLOR_PATTERN = re.compile(r"^#[0-9a-f]{6}$")


@dataclass(frozen=True)
class BuiltinTag:
    tag: str
    label: str
    color: str
    description: str


BUILTIN_TAGS: dict[str, BuiltinTag] = {t.tag: t for t in (
    BuiltinTag("web", "Web", "#2f6fdb", "captured in a harness browser"),
    BuiltinTag("chrome", "Chrome", "#e0a100", "captured in Google Chrome"),
    BuiltinTag("chromium", "Chromium", "#4c8bf5", "captured in Chromium"),
    BuiltinTag("edge", "Edge", "#0f9d8a", "captured in Microsoft Edge"),
    BuiltinTag("brave", "Brave", "#e2572a", "captured in Brave"),
    BuiltinTag("mobile", "Mobile", "#7b4fd6", "captured on a simulator or emulator"),
    BuiltinTag("ios", "iOS", "#5f6b7a", "captured on the iOS simulator"),
    BuiltinTag("android", "Android", "#3a9e4f", "captured on the Android emulator"),
)}

_BROWSER_TAGS = {"chrome": "chrome", "chrome-beta": "chrome", "google chrome": "chrome", "google-chrome": "chrome",
                 "google-chrome-stable": "chrome", "chromium": "chromium", "chromium-browser": "chromium",
                 "edge": "edge", "msedge": "edge", "microsoft edge": "edge", "brave": "brave",
                 "brave browser": "brave", "brave-browser": "brave"}


def browser_tag(command: str | None) -> str | None:
    if not command:
        return None
    raw = str(command).strip()
    candidates = [raw.lower()]
    if "/" in raw or "\\" in raw:
        path = PurePath(raw.replace("\\", "/"))
        candidates += [path.stem.lower(), *(p.lower().removesuffix(".app") for p in path.parts)]
    for candidate in candidates:
        if candidate in _BROWSER_TAGS:
            return _BROWSER_TAGS[candidate]
    return None


def builtin_tags_for(lease_kind: str | None, browser_command: str | None = None) -> list[str]:
    if lease_kind == "browser":
        browser = browser_tag(browser_command)
        return ["web", browser] if browser else ["web"]
    if lease_kind in ("ios", "android"):
        return ["mobile", lease_kind]
    return []


def normalize_color(color: str) -> str:
    value = (color or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if not COLOR_PATTERN.match(value):
        raise ValueError(f"invalid color '{color}': use #rrggbb")
    return value


def colors(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT tag, color FROM tag_colors")}


def color_for(conn: sqlite3.Connection, tag: str) -> str | None:
    row = conn.execute("SELECT color FROM tag_colors WHERE tag = ?", (tag,)).fetchone()
    if row:
        return row[0]
    builtin = BUILTIN_TAGS.get(tag)
    return builtin.color if builtin else None


def set_color(conn: sqlite3.Connection, tag: str, color: str, user: str | None = None) -> str:
    value = normalize_color(color)
    conn.execute(
        "INSERT INTO tag_colors (tag, color, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tag) DO UPDATE SET color = excluded.color, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by", (tag, value, now(), user))
    return value


def reset_color(conn: sqlite3.Connection, tag: str) -> bool:
    return conn.execute("DELETE FROM tag_colors WHERE tag = ?", (tag,)).rowcount > 0
