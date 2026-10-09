from __future__ import annotations

import os
import posixpath
import re
import threading
import unicodedata
from pathlib import Path, PurePosixPath

from eks_harness.paths import Paths

PROJECT_LEVEL_SLUG = "_project"
THUMB_NAME = ".thumb.jpg"
POSTER_NAME = ".poster.jpg"
SITE_DIR = "site"
RESERVED_NAMES = frozenset({SITE_DIR, THUMB_NAME, POSTER_NAME})
MAX_FILENAME = 180
MAX_SITE_PATH = 1024
TREE_LOCK = threading.RLock()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_WINDOWS_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                              *(f"lpt{i}" for i in range(1, 10))})


class UnsafePath(ValueError):
    pass


def sanitize_filename(name: str | None, fallback: str = "file") -> str:
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _CONTROL.sub("", text)
    text = _UNSAFE.sub("_", text).strip().lstrip(".").strip()
    if not text:
        text = fallback
    stem, dot, ext = text.rpartition(".")
    if not dot:
        stem, ext = text, ""
    if stem.lower() in _WINDOWS_DEVICES:
        stem = f"{stem}_"
    if len(text) > MAX_FILENAME:
        ext = ext[:16]
        stem = stem[:MAX_FILENAME - len(ext) - 1]
    text = f"{stem}.{ext}" if dot and ext else stem
    if text in RESERVED_NAMES:
        text = f"{text}_"
    return text


def normalize_site_path(raw: str) -> str:
    text = unicodedata.normalize("NFC", str(raw or "")).replace("\\", "/")
    if _CONTROL.search(text):
        raise UnsafePath(f"control characters in path {raw!r}")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise UnsafePath(f"absolute path {raw!r}")
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePath(f"path escapes the site root: {raw!r}")
        if part.split(".", 1)[0].lower() in _WINDOWS_DEVICES:
            raise UnsafePath(f"unsupported path segment {part!r} in {raw!r}")
        parts.append(part)
    if not parts:
        raise UnsafePath(f"empty path {raw!r}")
    normalized = "/".join(parts)
    if len(normalized) > MAX_SITE_PATH:
        raise UnsafePath(f"path too long: {raw[:80]!r}")
    return normalized


def request_site_path(raw: str) -> str | None:
    text = (raw or "").replace("\\", "/")
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def session_dir_name(slug: str | None) -> str:
    return slug or PROJECT_LEVEL_SLUG


def artifact_rel_dir(project_id: str, session_slug: str | None, artifact_id: str) -> str:
    owner, _, name = project_id.partition("/")
    return str(PurePosixPath(owner, name, session_dir_name(session_slug), artifact_id))


def artifact_rel_path(project_id: str, session_slug: str | None, artifact_id: str, filename: str) -> str:
    return f"{artifact_rel_dir(project_id, session_slug, artifact_id)}/{filename}"


def resolve_in(root: Path, relative: str) -> Path:
    base = root.resolve()
    candidate = (base / relative).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise UnsafePath(f"{relative!r} is outside {base}")
    return candidate


def file_path(paths: Paths, rel_path: str) -> Path:
    return resolve_in(paths.store_dir, rel_path)


def artifact_dir(paths: Paths, rel_path: str) -> Path:
    return file_path(paths, posixpath.dirname(rel_path))


def thumb_path(paths: Paths, rel_path: str) -> Path:
    return artifact_dir(paths, rel_path) / THUMB_NAME


def poster_path(paths: Paths, rel_path: str) -> Path:
    return artifact_dir(paths, rel_path) / POSTER_NAME


def site_root(paths: Paths, rel_path: str) -> Path:
    return artifact_dir(paths, rel_path) / SITE_DIR


def site_file(paths: Paths, rel_path: str, site_path: str) -> Path | None:
    root = site_root(paths, rel_path)
    try:
        candidate = resolve_in(root, site_path) if site_path else root
    except UnsafePath:
        return None
    return candidate


def remove_empty_parents(path: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = path.resolve()
    with TREE_LOCK:
        while current != stop and current.is_relative_to(stop):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent


def place_dir(source: Path, target: Path) -> None:
    with TREE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total
