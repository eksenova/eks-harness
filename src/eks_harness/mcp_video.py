from __future__ import annotations

import base64
import inspect
import json
import logging
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

log = logging.getLogger("eks_harness.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
LONG = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

ProjectArg = Annotated[str, Field(description="Path to a video project (project.py or project.json, or its folder)")]


class _Reporter:
    def __init__(self, ctx: Context | None) -> None:
        self.ctx = ctx
        self.session = self

    async def info(self, message: str) -> None:
        if self.ctx is not None:
            await _maybe(getattr(self.ctx, "info", None), message)

    async def warning(self, message: str) -> None:
        if self.ctx is not None:
            await _maybe(getattr(self.ctx, "warning", None), message)

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        if self.ctx is not None:
            await _maybe(getattr(self.ctx, "report_progress", None), progress, total, message)

    async def send_resource_updated(self, **kwargs: Any) -> None:
        session = getattr(self.ctx, "session", None) if self.ctx is not None else None
        await _maybe(getattr(session, "send_resource_updated", None), **kwargs)


async def _maybe(fn: Any, *args: Any, **kwargs: Any) -> None:
    if fn is None:
        return
    try:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            await result
    except Exception as error:
        log.debug("progress report failed: %s", error)


def _guard(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (FileNotFoundError, KeyError, ValueError) as error:
        raise ToolError(str(error)) from None


def register_video_tools(server: MCPServer) -> bool:
    try:
        from eks_harness.studio import tools as studio
        from eks_harness.studio.resources import RESOURCES
    except ImportError as error:
        log.info("video tools unavailable (install eks-harness[video]): %s", error)
        return False
    descriptions = {name: text for name, (_, text) in studio.TOOLS.items()}

    @server.tool(name="video_validate", description=descriptions["validate_project"], annotations=READ_ONLY)
    def video_validate(project: ProjectArg) -> dict[str, Any]:
        return _guard(studio.validate_project, project)

    @server.tool(name="video_inspect", description=descriptions["inspect_project"], annotations=READ_ONLY)
    def video_inspect(project: ProjectArg) -> dict[str, Any]:
        return _guard(studio.inspect_project, project)

    @server.tool(name="video_markers", description=descriptions["extract_markers"], annotations=READ_ONLY)
    def video_markers(project: ProjectArg) -> dict[str, Any]:
        return _guard(studio.extract_markers, project)

    @server.tool(name="video_effects", description=descriptions["list_effects"], annotations=READ_ONLY)
    def video_effects() -> dict[str, Any]:
        return studio.list_effects()

    @server.tool(name="video_add_segment", description=descriptions["add_segment"], annotations=WRITE)
    def video_add_segment(project: ProjectArg, track: Annotated[str, Field(description="Track name")],
                          segment: Annotated[dict[str, Any], Field(description="Segment IR object")],
                          create_track: bool = False) -> dict[str, Any]:
        return _guard(studio.add_segment, project, track, segment, create_track=create_track)

    @server.tool(name="video_add_effect", description=descriptions["add_effect"], annotations=WRITE)
    def video_add_effect(project: ProjectArg, segment_id: str,
                         effect: Annotated[dict[str, Any], Field(description="Effect IR object")]) -> dict[str, Any]:
        return _guard(studio.add_effect, project, segment_id, effect)

    @server.tool(name="video_set_property", description=descriptions["set_property"], annotations=WRITE)
    def video_set_property(project: ProjectArg,
                           path: Annotated[str, Field(description="Dotted/indexed path, e.g. tracks[0].segments[1].in")],
                           value: Annotated[Any, Field(description="New JSON value")]) -> dict[str, Any]:
        return _guard(studio.set_property, project, path, value)

    @server.tool(name="video_render", description=descriptions["render_project"], annotations=LONG)
    async def video_render(project: ProjectArg, ctx: Context,
                           mode: Literal["preview", "final"] = "preview", encoder: str | None = None,
                           bitrate_kbps: int | None = None, crf: int | None = None, width: int | None = None,
                           height: int | None = None, range_start: float | None = None,
                           range_end: float | None = None) -> dict[str, Any]:
        try:
            return await studio.render_project(project, mode, encoder=encoder, bitrate_kbps=bitrate_kbps, crf=crf,
                                               width=width, height=height, range_start=range_start,
                                               range_end=range_end, reporter=_Reporter(ctx))
        except (FileNotFoundError, ValueError) as error:
            raise ToolError(str(error)) from None

    @server.tool(name="video_cancel", description=descriptions["cancel_job"], annotations=WRITE)
    async def video_cancel(job_id: str) -> dict[str, Any]:
        try:
            return await studio.cancel_job(job_id)
        except KeyError as error:
            raise ToolError(str(error)) from None

    for spec in RESOURCES:
        _register_resource(server, spec)
    return True


def _register_resource(server: MCPServer, spec: Any) -> None:
    from eks_harness.studio.resources import read

    names = re.findall(r"\{([a-z_]+)\}", spec.uri)

    def reader(**params: str) -> Any:
        uri = spec.uri
        for key, value in params.items():
            uri = uri.replace("{" + key + "}", value)
        _, value = read(uri)
        if isinstance(value, bytes):
            return value if spec.mime_type.startswith(("image/", "audio/", "video/")) else base64.b64encode(value)
        if isinstance(value, dict | list):
            return json.dumps(value, ensure_ascii=False, default=str)
        return value

    reader.__name__ = spec.name.replace("-", "_").replace(".", "_")
    reader.__signature__ = inspect.Signature(
        [inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, annotation=str) for n in names])
    reader.__annotations__ = {n: str for n in names}
    try:
        server.resource(spec.uri, name=spec.name, title=spec.title, description=spec.description,
                        mime_type=spec.mime_type)(reader)
    except Exception as error:
        log.warning("resource %s not registered: %s", spec.uri, error)
