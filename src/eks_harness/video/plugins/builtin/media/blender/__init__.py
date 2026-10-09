"""``BlenderScene``: render a Blender scene in sync with the video timeline.

Optional and opt-in: a project enables it with ``plugins=[PluginRequirement(name="blender")]``.
Only then is Blender (4.2 or newer, no Python packages) looked up, and a
render fails up front with an install hint when it is missing. The
scene is a ``.blend`` file, a bpy script, or both. The engine runs Blender
headless, configures the scene to the project's fps, resolution and the
segment's source window, hands the script the project's markers converted to
scene frames (``import eks_harness.blender``), renders the frames across one or
more worker processes and encodes them to an alpha-capable ProRes 4444 clip
that composites on any track like footage. Renders are cached by the contents
of the blend file, the script, ``inputs``, the parameters, the timing, the
markers and the Blender version, and interrupted renders resume.

    from eks_harness.video.plugins.builtin.media.blender import BlenderScene

    Project(..., plugins=[PluginRequirement(name="blender")], tracks=[Track(name="fx", z=1, segments=[
        Segment(id="phone", start=Seconds(t=0), out=Seconds(t=8),
                media=BlenderScene(script="scenes/phone.py", params={"color": "gold"}))])])
"""

from __future__ import annotations

import os

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field, model_validator

from eks_harness.video.ir.media import PathLike, _MediaBase, register_media_model
from eks_harness.video.plugins.base import MediaRenderer, MediaRenderRequest

from . import runner

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["BlenderRenderer", "BlenderScene"]


class BlenderScene(_MediaBase):
    """A Blender scene rendered for a segment's source window, frame-locked to the project."""

    kind: Literal["blender_scene"] = "blender_scene"
    root: PathLike | None = None
    """Folder the scene's files live in: relative ``blend``/``script``/``inputs`` resolve against it and farm
    workers mirror it. Default: the render workspace."""

    blend: PathLike | None = None
    """``.blend`` file to open. Optional when ``script`` builds the scene from scratch."""

    script: PathLike | None = None
    """bpy script run after the blend file loads and the scene is configured. It can
    ``import eks_harness.blender`` to read the timing, ``params`` and markers as scene frames."""

    params: dict[str, Any] = Field(default_factory=dict)
    """JSON-serialisable values handed to the script as ``eks_harness.blender.params``."""

    media: dict[str, Any] = Field(default_factory=dict)
    """Other media rendered first for this segment's source window and handed to the script as file paths
    (``eks_harness.blender.media[name]``): a file path, or any media kind (dict form), e.g. a live app capture for a
    phone screen. Movies get a ``.framemd5`` sidecar so the frame cache tracks exactly which frame is shown."""

    inputs: list[PathLike] = Field(default_factory=list)
    """Extra files or directories the script reads; their contents are part of the cache key."""

    scene: str | None = None
    """Scene to render (default: the active scene)."""

    camera: str | None = None
    """Camera object to render through (default: the scene camera)."""

    engine: Literal["CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"] | None = None
    """Render engine. ``None`` keeps whatever the blend file or script set (Cycles for a new scene)."""

    samples: int | None = Field(default=None, gt=0)
    """Render samples (Cycles samples or EEVEE TAA samples); ``None`` keeps the scene's value."""

    device: Literal["AUTO", "CPU", "OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"] = "AUTO"
    """Cycles compute device. ``AUTO`` picks the best GPU backend available, else CPU."""

    transparent: bool = True
    """Render with a transparent film so the clip composites over lower tracks."""

    resolution: tuple[int, int] | None = None
    """Override the render size (default: the project resolution). The clip is scaled to the project size."""

    frame_start: int = 1
    """Scene frame that corresponds to source time 0. Segment ``in``/``out`` select the window."""

    workers: int | None = Field(default=None, gt=0)
    """Blender processes rendering in parallel (default: ``EKS_HARNESS_BLENDER_WORKERS`` or 1)."""

    worker_env: list[dict[str, str]] = Field(default_factory=list)
    """Per-worker environment, cycled over the workers (e.g. ``{"CUDA_VISIBLE_DEVICES": "1"}``)."""

    blender: PathLike | None = None
    """Blender executable (default: ``EKS_HARNESS_BLENDER``, ``blender`` on PATH, then the usual install paths)."""

    farm: bool = True
    """Use the configured render farm (``eks_harness.video.farm``) when one exists; ``False`` renders locally only."""

    frame_cache: bool = True
    """Keep frames per segment with a scene fingerprint each and skip frames whose scene did not change."""

    sync_exclude: list[str] = Field(default_factory=lambda: ["__pycache__", ".git", "/cache", "/renders"])

    events: list[dict[str, Any]] = Field(default_factory=list)
    """Score inputs on the source clock (``{"time", "name", "verb", "data", "prop", "value"}``) the script reads with
    ``eks_harness.blender.inputs()``."""

    emits: bool = False
    """The script calls ``eks_harness.blender.emit()``: run an emit pass and write ``<output>.events.json``."""
    """rsync exclude patterns when the workspace is mirrored to remote farm workers."""

    @model_validator(mode="after")
    def _needs_scene(self) -> BlenderScene:
        if self.blend is None and self.script is None:
            raise ValueError("BlenderScene needs a blend file, a script, or both")
        return self


register_media_model(BlenderScene)


class BlenderRenderer(MediaRenderer):
    name: ClassVar[str] = "blender"
    model: ClassVar[type[BlenderScene]] = BlenderScene
    version: ClassVar[str] = "2"
    opt_in: ClassVar[bool] = True

    def __init__(self, context: Any = None) -> None:
        settings = dict(getattr(context, "settings", None) or {})
        for key, env in (("executable", "EKS_HARNESS_BLENDER"), ("workers", "EKS_HARNESS_BLENDER_WORKERS"),
                         ("cycles_device", "EKS_HARNESS_CYCLES_DEVICE")):
            value = settings.get(key)
            if value not in (None, "") and not (key == "workers" and int(value) <= 1):
                os.environ.setdefault(env, str(value))

    def check_available(self, ctx: RenderContext) -> None:
        runner.blender_version(runner.find_blender(None))

    def cache_inputs(self, media: BlenderScene, ctx: RenderContext) -> list[Path]:  # type: ignore[override]
        base = runner.root_of(media, ctx)
        files = [media.blend, media.script, *media.inputs]
        return [p if Path(p).is_absolute() else base / p for p in files if p is not None] + runner.runtime_files()

    def cache_salt(self, media: BlenderScene, ctx: RenderContext) -> str:  # type: ignore[override]
        return runner.blender_version(runner.find_blender(media.blender))

    def render(self, request: MediaRenderRequest, ctx: RenderContext) -> Path:
        return runner.render(request, ctx)
