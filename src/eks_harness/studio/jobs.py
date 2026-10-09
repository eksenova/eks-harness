"""In-memory + on-disk render job registry.

Job state lives in :class:`JobRegistry`. The registry persists every state
change to ``<state dir>/studio/jobs.json`` so jobs survive a daemon
restart, and exposes a ``subscribers`` async-pubsub for the
``eks-harness://video/render_jobs/<id>`` resource.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .types import JobStatus

__all__ = ["JobRecord", "JobRegistry", "default_jobs_file"]

_LOG = logging.getLogger(__name__)


@dataclass
class JobRecord:
    """Persistable snapshot of one render job."""

    job_id: str
    project_id: str
    project_path: str
    mode: str
    status: JobStatus = "queued"
    progress: float = 0.0
    message: str = ""
    output_path: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    current_step: str | None = None
    # 0-based index of the active step (mirrors the renderer's ``step_start``
    # ``index`` payload). UI consumers display ``step_index + 1`` for human
    # "step N of M" copy. ``None`` until the first ``step_start`` arrives.
    step_index: int | None = None
    total_steps: int = 0
    frame_index: int = 0
    frame_total: int = 0
    eta_s: float | None = None
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_jobs_file() -> Path:
    from .paths import state_dir

    return state_dir() / "jobs.json"


class JobRegistry:
    """Process-wide registry. Persists to disk on every mutation."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._listeners: dict[str, list[asyncio.Queue[JobRecord]]] = {}
        self._lock = asyncio.Lock()
        self._persist_path = persist_path or default_jobs_file()
        self._load()

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:
            _LOG.warning("could not parse %s; starting with an empty registry", self._persist_path)
            return
        valid_fields = {f.name for f in JobRecord.__dataclass_fields__.values()}
        for entry in data.get("jobs", []) or []:
            try:
                filtered = {k: v for k, v in entry.items() if k in valid_fields}
                record = JobRecord(**filtered)
                self._jobs[record.job_id] = record
            except TypeError:
                continue

    def _persist(self) -> None:
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": [r.to_dict() for r in self._jobs.values()]}
        tmp = self._persist_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._persist_path)

    async def create(
        self,
        *,
        project_id: str,
        project_path: Path,
        mode: str,
        job_id: str | None = None,
    ) -> JobRecord:
        async with self._lock:
            effective_id = job_id or uuid.uuid4().hex[:12]
            if effective_id in self._jobs:
                raise ValueError(f"job already exists: {effective_id}")
            record = JobRecord(
                job_id=effective_id,
                project_id=project_id,
                project_path=str(project_path),
                mode=mode,
            )
            self._jobs[effective_id] = record
            self._persist()
        return record

    async def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        message: str | None = None,
        output_path: str | None = None,
        error: str | None = None,
        current_step: str | None = None,
        step_index: int | None = None,
        total_steps: int | None = None,
        frame_index: int | None = None,
        frame_total: int | None = None,
        eta_s: float | None = None,
        error_category: str | None = None,
    ) -> JobRecord | None:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if status is not None:
                record.status = status
            if progress is not None:
                record.progress = max(0.0, min(1.0, progress))
            if message is not None:
                record.message = message
            if output_path is not None:
                record.output_path = output_path
            if error is not None:
                record.error = error
            if current_step is not None:
                record.current_step = current_step
            if step_index is not None:
                record.step_index = step_index
            if total_steps is not None:
                record.total_steps = total_steps
            if frame_index is not None:
                record.frame_index = frame_index
            if frame_total is not None:
                record.frame_total = frame_total
            if eta_s is not None:
                record.eta_s = eta_s
            if error_category is not None:
                record.error_category = error_category
            record.updated_at = time.time()
            self._persist()
            snapshot = JobRecord(**record.to_dict())

        for queue in list(self._listeners.get(job_id, [])):
            queue.put_nowait(snapshot)
        return snapshot

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def all(self) -> list[JobRecord]:
        return list(self._jobs.values())

    async def watch(self, job_id: str) -> AsyncIterator[JobRecord]:
        queue: asyncio.Queue[JobRecord] = asyncio.Queue()
        self._listeners.setdefault(job_id, []).append(queue)
        try:
            current = self.get(job_id)
            if current is not None:
                yield current
            while True:
                item = await queue.get()
                yield item
                if item.status in {"succeeded", "failed", "cancelled"}:
                    break
        finally:
            listeners = self._listeners.get(job_id)
            if listeners and queue in listeners:
                listeners.remove(queue)


_REGISTRY: JobRegistry | None = None


def get_registry() -> JobRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = JobRegistry()
    return _REGISTRY


def set_registry(registry: JobRegistry | None) -> None:
    """Replace the process-wide registry. Used by tests."""

    global _REGISTRY
    _REGISTRY = registry
