from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

__version__ = "0.1.0"

HASH_ROOT_ENTRIES = ("pyproject.toml", "hatch_build.py", "README.md", "src", "web")
HASH_EXCLUDED_PREFIXES = (
    "web/node_modules",
    "web/dist",
    "web/.vite",
    "src/eks_harness/web_dist",
)
HASH_EXCLUDED_FILES = ("src/eks_harness/_build.json",)
HASH_EXCLUDED_DIR_NAMES = frozenset({
    "__pycache__",
    "node_modules",
    "tests",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
})
HASH_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
HASH_EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db"})

BUILD_INFO_FILE = "_build.json"


def _excluded(relative: str, is_dir: bool) -> bool:
    parts = relative.split("/")
    if any(part in HASH_EXCLUDED_DIR_NAMES for part in (parts if is_dir else parts[:-1])):
        return True
    if any(relative == prefix or relative.startswith(prefix + "/") for prefix in HASH_EXCLUDED_PREFIXES):
        return True
    if is_dir:
        return False
    if relative in HASH_EXCLUDED_FILES or parts[-1] in HASH_EXCLUDED_NAMES:
        return True
    return relative.endswith(HASH_EXCLUDED_SUFFIXES)


def iter_source_files(root: Path) -> list[str]:
    root = Path(root)
    found: list[str] = []
    for entry in HASH_ROOT_ENTRIES:
        path = root / entry
        if path.is_file():
            if not _excluded(entry, False):
                found.append(entry)
            continue
        if not path.is_dir():
            continue
        for current, dirs, files in os.walk(path):
            current_rel = Path(current).relative_to(root).as_posix()
            dirs[:] = sorted(d for d in dirs if not _excluded(f"{current_rel}/{d}", True))
            for name in files:
                relative = f"{current_rel}/{name}"
                full = Path(current) / name
                if full.is_symlink() or not full.is_file():
                    continue
                if not _excluded(relative, False):
                    found.append(relative)
    return sorted(set(found))


def compute_source_hash(root: Path) -> str:
    root = Path(root)
    digest = hashlib.sha256()
    for relative in iter_source_files(root):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def source_root() -> Path | None:
    candidate = package_dir().parent.parent
    if (candidate / "pyproject.toml").is_file() and (candidate / "hatch_build.py").is_file():
        return candidate
    return None


def build_info() -> dict:
    info: dict = {"version": __version__, "sourceHash": None, "builtAt": None, "editable": False}
    path = package_dir() / BUILD_INFO_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            info.update({k: data.get(k, info.get(k)) for k in ("version", "sourceHash", "builtAt", "gitCommit", "gitDirty")})
    except (OSError, json.JSONDecodeError):
        pass
    root = source_root()
    if root is not None:
        info["editable"] = True
        info["sourceHash"] = compute_source_hash(root)
        info["sourceRoot"] = str(root)
    return info
