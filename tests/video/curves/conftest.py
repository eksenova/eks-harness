from __future__ import annotations

from eks_harness.video.compile.markers import MarkerSet
from eks_harness.video.ir.project import Project


def make_project(duration: float = 1.0, fps: float = 30.0, random_seed: int | None = None) -> Project:
    kwargs: dict[str, object] = {
        "fps": fps,
        "resolution": (64, 64),
        "duration": duration,
    }
    if random_seed is not None:
        kwargs["random_seed"] = random_seed
    return Project(**kwargs)


def empty_markers() -> MarkerSet:
    return MarkerSet()
