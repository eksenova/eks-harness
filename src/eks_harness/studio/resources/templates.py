"""``eks-harness://video/templates/{name}`` - bundled starter projects.

Templates ship inside the ``eks_harness.studio/templates/`` package directory so
they install with the wheel.
"""

from __future__ import annotations

from importlib import resources

from ._spec import ResourceSpec

__all__ = ["RESOURCES", "TEMPLATE_NAMES", "read_template"]

TEMPLATE_NAMES = (
    "beat_flash_montage",
    "captioned_talking_head",
    "punch_in_zoom",
)


def read_template(name: str) -> str:
    if name not in TEMPLATE_NAMES:
        raise KeyError(f"unknown template {name!r}; available: {', '.join(TEMPLATE_NAMES)}")
    package = resources.files("eks_harness.studio.templates")
    return (package / f"{name}.py.txt").read_text(encoding="utf-8")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/templates/{name}', name='video_templates', title='Video project templates',
                 description="Bundled video project templates. Read with a name like 'beat_flash_montage'.",
                 mime_type='text/x-python', read=read_template, needs_roots=False, needs_registry=False),
]
