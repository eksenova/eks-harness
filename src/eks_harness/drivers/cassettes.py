from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from eks_harness.engine.resources import CassetteRequest

CASSETTE_FILE = "cassette.json"
META_FILE = "meta.json"


class FileCassetteHook:
    def __init__(self, root: Path) -> None:
        self.root = root

    def folder(self, request: CassetteRequest) -> Path:
        return self.root / request.key.removeprefix("sha256:")[:40]

    def recorded(self, request: CassetteRequest) -> bool:
        return (self.folder(request) / CASSETTE_FILE).is_file()

    def available(self, request: CassetteRequest) -> bool:
        return request.mode != "replay" or self.recorded(request)

    def opened(self, request: CassetteRequest) -> Mapping[str, JsonValue]:
        folder = self.folder(request)
        folder.mkdir(parents=True, exist_ok=True)
        mode = request.mode
        if mode == "auto":
            mode = "replay" if self.recorded(request) else "record"
        return {"dir": str(folder), "mode": mode, "key": request.key, "name": request.name}

    def closed(self, request: CassetteRequest, recorded: Mapping[str, JsonValue]) -> str | None:
        folder = self.folder(request)
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"name": request.name, "key": request.key, "node": request.node_id, "run": request.run.id,
                "plan": request.run.plan_hash, "backend": request.backend.model_dump(mode="json")
                if request.backend is not None else None, "recorded": dict(recorded), "closedAt": time.time()}
        temporary = folder / f"{META_FILE}.tmp"
        temporary.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(temporary, folder / META_FILE)
        return None


__all__ = ["CASSETTE_FILE", "FileCassetteHook"]
