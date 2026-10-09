"""``eks-harness://video/render_jobs/{job_id}`` - live status JSON for a render job."""

from __future__ import annotations

import json

from ._spec import ResourceSpec

from ..jobs import JobRegistry, get_registry

__all__ = ["RESOURCES", "read_render_job"]


def read_render_job(job_id: str, *, registry: JobRegistry | None = None) -> str:
    record = (registry or get_registry()).get(job_id)
    if record is None:
        raise KeyError(f"unknown render job {job_id}")
    return json.dumps(record.to_dict(), indent=2, sort_keys=True)


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/render_jobs/{job_id}', name='video_render_job', title='Render job',
                 description='Live status JSON for a render job; subscribers are notified as the renderer reports progress.',
                 mime_type='application/json', read=read_render_job, needs_roots=False, needs_registry=True),
]
