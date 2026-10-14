from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MANIFEST_SCHEMA = "eks/seed-snapshot@1"
KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SNAPSHOT_PREFIX = "seed-"
SNAPSHOT_NAME_DIGITS = 16


class ReplayEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str
    path: str
    kind: str
    key: str | None = None
    phase: str
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    resolved: dict[str, JsonValue] = Field(default_factory=dict)
    parent: JsonValue = None


class Divergence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)
    digest: bool = False
    previous: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: str = Field(default=MANIFEST_SCHEMA, alias="schema")
    key: str
    seed: str
    seed_hash: str = Field(alias="seedHash")
    definition: str
    migrations: str | None = None
    snapshot: str | None = None
    digest: str | None = None
    outputs_digest: str = Field(alias="outputsDigest")
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    personas: dict[str, JsonValue] = Field(default_factory=dict)
    replay: list[ReplayEntry] = Field(default_factory=list)
    bases: list[str] = Field(default_factory=list)
    created: str
    build_ms: int = Field(default=0, alias="buildMs")
    resources: int = 0
    runs: list[str] = Field(default_factory=list)
    deterministic: bool | None = None
    divergence: Divergence | None = None

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def snapshot_name(key: str) -> str:
    return SNAPSHOT_PREFIX + key.removeprefix("sha256:")[:SNAPSHOT_NAME_DIGITS]


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def _path(self, definition: str, key: str) -> Path:
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"a seed snapshot key is sha256:<64 hex>, got {key!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", definition):
            raise ValueError(f"invalid backend definition name {definition!r}")
        return self.root / definition / f"{key.removeprefix('sha256:')}.json"

    def get(self, definition: str, key: str) -> Manifest | None:
        path = self._path(definition, key)
        try:
            return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None

    def put(self, manifest: Manifest) -> Path:
        path = self._path(manifest.definition, manifest.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with self._lock:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest.dump(), handle, ensure_ascii=False, indent=2, sort_keys=True)
            temp.replace(path)
        return path

    def drop(self, definition: str, key: str) -> bool:
        path = self._path(definition, key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def all(self, definition: str | None = None) -> Iterator[Manifest]:
        if not self.root.is_dir():
            return
        folders = [self.root / definition] if definition else sorted(p for p in self.root.iterdir() if p.is_dir())
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    yield Manifest.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue

    def find(self, key: str) -> Manifest | None:
        wanted = key if key.startswith("sha256:") else None
        for manifest in self.all():
            if manifest.key == wanted or manifest.key.removeprefix("sha256:").startswith(key.removeprefix("sha256:")):
                return manifest
        return None

    def stale(self, definition: str, seed: str, keep: int, current: str) -> list[Manifest]:
        same = sorted((m for m in self.all(definition) if m.seed == seed and m.key != current),
                      key=lambda m: m.created, reverse=True)
        return same[max(0, keep - 1):]


__all__ = ["MANIFEST_SCHEMA", "Divergence", "Manifest", "ReplayEntry", "SnapshotStore", "snapshot_name"]
