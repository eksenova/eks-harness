"""``eks-harness://video/preview_frames/{job_id}/{frame}`` - latest preview JPEG.

The render driver writes preview frames to
``<project>/cache/preview_frames/<job_id>/<NNNN>.jpg``. The resource returns
the bytes of the requested frame; ``frame=latest`` returns the highest-numbered
frame available (or the most recently modified if numeric ordering fails).
"""

from __future__ import annotations

from pathlib import Path

from ._spec import ResourceSpec

from ..jobs import JobRegistry, get_registry

__all__ = ["RESOURCES", "read_preview_frame", "resolve_frame"]


def read_preview_frame(job_id: str, frame: str, *, registry: JobRegistry | None = None) -> bytes:
    record = (registry or get_registry()).get(job_id)
    if record is None:
        raise KeyError(f"unknown render job {job_id}")
    frame_dir = Path(record.project_path) / "cache" / "preview_frames" / job_id
    path = resolve_frame(frame_dir, frame)
    if path is None:
        raise FileNotFoundError(f"no preview frame {frame!r} for job {job_id}")
    return path.read_bytes()


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/preview_frames/{job_id}/{frame}', name='video_preview_frame', title='Preview frame',
                 description='Latest JPEG preview frame of a render job; frame=latest for the newest.',
                 mime_type='image/jpeg', read=read_preview_frame, needs_roots=False, needs_registry=True),
]


def resolve_frame(frame_dir: Path, frame: str) -> Path | None:
    if not frame_dir.exists():
        return None
    if frame == "latest":
        candidates = sorted(frame_dir.glob("*.jpg"))
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda p: int(p.stem))
        except ValueError:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    if not frame.endswith(".jpg"):
        frame = f"{frame}.jpg"
    candidate = frame_dir / frame
    return candidate if candidate.exists() else None
