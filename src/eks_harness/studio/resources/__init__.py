"""Video studio resources as plain read functions with their URI templates.

``RESOURCES`` lists every resource (``eks-harness://video/...``) with its
metadata and reader so an MCP server or the HTTP API can expose them.
"""

from __future__ import annotations

from . import (
    audio_lib,
    easings,
    effects,
    encoding_presets,
    image_lib,
    plugins,
    preview_frames,
    projects,
    render_jobs,
    templates,
    video_lib,
)
import re
from typing import Any

from ._spec import ResourceSpec

__all__ = ["RESOURCES", "ResourceSpec", "match", "read"]

RESOURCES: list[ResourceSpec] = [
    *templates.RESOURCES,
    *projects.RESOURCES,
    *effects.RESOURCES,
    *easings.RESOURCES,
    *audio_lib.RESOURCES,
    *video_lib.RESOURCES,
    *image_lib.RESOURCES,
    *plugins.RESOURCES,
    *encoding_presets.RESOURCES,
    *render_jobs.RESOURCES,
    *preview_frames.RESOURCES,
]


def _pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", template)
    regex = "".join(
        (f"(?P<{p[1:-1]}>.+)" if p == "{path}" else f"(?P<{p[1:-1]}>[^/]+)") if p.startswith("{") else re.escape(p)
        for p in parts
    )
    return re.compile(regex + r"\Z")


_PATTERNS = sorted(((spec, _pattern(spec.uri)) for spec in RESOURCES), key=lambda item: -len(item[0].uri))


def match(uri: str) -> tuple[ResourceSpec, dict[str, str]]:
    for spec, pattern in _PATTERNS:
        found = pattern.match(uri)
        if found:
            return spec, found.groupdict()
    raise KeyError(f"no resource matches {uri}")


def read(uri: str, *, roots: Any = None, registry: Any = None) -> tuple[ResourceSpec, Any]:
    spec, params = match(uri)
    kwargs: dict[str, Any] = dict(params)
    if spec.needs_roots and roots is not None:
        kwargs["roots"] = roots
    if spec.needs_registry and registry is not None:
        kwargs["registry"] = registry
    return spec, spec.read(**kwargs)
