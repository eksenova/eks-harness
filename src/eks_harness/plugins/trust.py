from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache",
                       ".mypy_cache", ".harness-cache"})
SKIP_SUFFIXES = (".pyc", ".pyo")


def content_hash(root: Path) -> str:
    digest = hashlib.sha256()
    root = root.resolve()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES) or name == ".DS_Store":
                continue
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode("utf-8") + b"\0")
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


class TrustStore:
    def __init__(self, file: Path) -> None:
        self.file = file
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.file)

    def entries(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def get(self, plugin_id: str, root: Path) -> dict[str, Any] | None:
        entry = self.entries().get(_key(plugin_id, root))
        return entry if isinstance(entry, dict) else None

    def approve(self, plugin_id: str, root: Path, digest: str, *, by: str = "") -> dict[str, Any]:
        with self._lock:
            data = self._read()
            entry = {"plugin": plugin_id, "root": str(root.resolve()), "hash": digest,
                     "approvedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "by": by}
            data[_key(plugin_id, root)] = entry
            self._write(data)
            return entry

    def revoke(self, plugin_id: str, root: Path | None = None) -> int:
        with self._lock:
            data = self._read()
            keys = [k for k, v in data.items() if isinstance(v, dict) and v.get("plugin") == plugin_id
                    and (root is None or k == _key(plugin_id, root))]
            for key in keys:
                del data[key]
            if keys:
                self._write(data)
            return len(keys)


def _key(plugin_id: str, root: Path) -> str:
    return f"{plugin_id}@{root.resolve()}"
