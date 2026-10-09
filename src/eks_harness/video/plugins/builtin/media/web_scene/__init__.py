from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from eks_harness.video.ir.media import PathLike, _MediaBase, register_media_model
from eks_harness.video.plugins.base import MediaRenderer, MediaRenderRequest

from . import runner

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = ["SceneEvent", "WebScene", "WebSceneRenderer"]


class SceneEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: float
    name: str | None = None
    verb: str = "emit"
    data: Any = None
    prop: str | None = None
    value: Any = None
    ease: float | None = None
    source: str | None = None


class WebScene(_MediaBase):
    kind: Literal["web_scene"] = "web_scene"
    entry: PathLike
    root: PathLike | None = None
    props: dict[str, Any] = Field(default_factory=dict)
    events: list[SceneEvent] = Field(default_factory=list)
    marker_events: dict[str, str] = Field(default_factory=dict)
    media: dict[str, Any] = Field(default_factory=dict)
    inputs: list[PathLike] = Field(default_factory=list)
    resolution: tuple[int, int] | None = None
    device_scale_factor: float = 1.0
    transparent: bool = True
    ready: str | None = None
    timeout_ms: int = Field(default=30000, gt=0)
    browser_args: list[str] = Field(default_factory=list)


register_media_model(WebScene)


class WebSceneRenderer(MediaRenderer):
    name: ClassVar[str] = "web_scene"
    model: ClassVar[type[WebScene]] = WebScene
    version: ClassVar[str] = "1"

    def __init__(self, context: Any = None) -> None:
        self.context = context

    def check_available(self, ctx: RenderContext) -> None:
        runner.require_playwright()
        runner.runtime_script()

    def cache_inputs(self, media: WebScene, ctx: RenderContext) -> list[Path]:  # type: ignore[override]
        base = runner.root_of(media, ctx)
        extra = [Path(p) if Path(p).is_absolute() else base / p for p in media.inputs]
        return [base, *extra, runner.runtime_script()]

    def cache_salt(self, media: WebScene, ctx: RenderContext) -> str:  # type: ignore[override]
        return runner.browser_version()

    def render(self, request: MediaRenderRequest, ctx: RenderContext) -> Path:
        return runner.render(request, ctx)
