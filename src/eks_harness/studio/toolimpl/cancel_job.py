"""``cancel_job`` tool - terminate a running render."""

from __future__ import annotations

import logging
import sys
from typing import Any


from ..jobs import JobRegistry, get_registry
from ..types import Reporter
from .render_project import get_subprocess

__all__ = ["DESCRIPTION", "cancel"]

_LOG = logging.getLogger(__name__)


DESCRIPTION = (
    "Cancel a render job by ID; sends SIGTERM (or CTRL_BREAK on Windows) to the renderer subprocess."
)


async def cancel(job_id: str, *, registry: JobRegistry | None = None,
                 reporter: Reporter | None = None) -> dict[str, Any]:
    job_registry = registry or get_registry()
    record = job_registry.get(job_id)
    if record is None:
        raise KeyError(f"unknown job {job_id}")
    if record.status not in {"queued", "running"}:
        return {"job_id": job_id, "status": record.status, "cancelled": False}

    proc = get_subprocess(job_id)
    if proc is not None and proc.returncode is None:
        try:
            if sys.platform.startswith("win"):
                import signal as _signal

                proc.send_signal(_signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    await job_registry.update(job_id, status="cancelled", message="cancellation requested")
    if reporter is not None:
        await reporter.info(f"requested cancellation for job {job_id}")
    return {"job_id": job_id, "status": "cancelled", "cancelled": True}
