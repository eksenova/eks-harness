from __future__ import annotations

import functools
import importlib.metadata
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ExecContext, ExecResult, ExecutorSpec, ResolveContext
from eks_harness.ir.ops.edit import SOFTWARE_ENCODERS, RenderSettings, Timeline
from eks_harness.ir.types import MediaInfo
from eks_harness.media.edit.compose import ENCODERS, RenderJob, render
from eks_harness.media.files import ffmpeg_version, ingest, scratch, tool
from eks_harness.media.overlays import boxmap
from eks_harness.media.overlays.boxmap import BoxMap, RecordingBoxes
from eks_harness.ops.overlay import artifact_meta

EDIT_EXECUTOR = "eks.edit"
EDIT_EXECUTOR_VERSION = "1"
HARDWARE_ORDER = ("videotoolbox", "nvenc", "qsv", "amf")


@functools.cache
def available_encoders() -> frozenset[str]:
    done = subprocess.run([tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60,
                          check=False)
    names: set[str] = set()
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "V":
            names.add(parts[1])
    return frozenset(names)


def choose_encoder(settings: RenderSettings) -> str:
    found = available_encoders()
    if settings.encoder is not None:
        if ENCODERS[settings.encoder] not in found:
            raise NodeFailure("resolution_unavailable",
                              f"the render pins encoder {settings.encoder} ({ENCODERS[settings.encoder]}), which this "
                              "machine's ffmpeg does not have; render it where that encoder exists or pin libx264")
        return settings.encoder
    if settings.quality == "final":
        return "libx264"
    for name in HARDWARE_ORDER:
        if ENCODERS[name] in found:
            return name
    return "libx264"


def _inference(timeline: Timeline) -> dict[str, JsonValue] | None:
    from eks_harness.media.edit.effects import inference_record

    for track in timeline.tracks:
        for clip in track.clips:
            for effect in clip.effects:
                record = inference_record(effect)
                if record is not None:
                    return dict(record)
    return None


def _resolution(timeline: Timeline) -> dict[str, JsonValue]:
    encoder = choose_encoder(timeline.render)
    record: dict[str, JsonValue] = {"encoder": encoder, "encoder_version": ffmpeg_version()}
    inference = _inference(timeline)
    if inference is not None:
        record["inference"] = inference
    return record


def _timeline(args: Any) -> Timeline:
    found = getattr(args, "timeline", None)
    if not isinstance(found, Timeline):
        raise NodeFailure("args_invalid", "edit.render needs a timeline")
    return found


def _info(value: Any) -> MediaInfo:
    return value if isinstance(value, MediaInfo) else MediaInfo.model_validate(value)


def clip_boxes(call: ExecContext, info: MediaInfo) -> BoxMap | RecordingBoxes | None:
    if info.artifact is not None:
        meta = artifact_meta(call, info.artifact)
        sidecar = meta.get("boxes")
        if not isinstance(sidecar, str):
            return None
        path = call.artifacts.file(sidecar)
        return boxmap.load(path) if path is not None else None
    found = boxmap.sidecar_path(call.media(info.model_dump(mode="json", by_alias=True, exclude_none=True)).path)
    return boxmap.load(found) if found.is_file() else None


class EditRenderExecutor:
    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        return _resolution(_timeline(context.args))

    def run(self, call: ExecContext) -> ExecResult:
        from eks_harness.media.edit.effects import open_effect

        timeline = _timeline(call.args)
        resolution = _resolution(timeline)
        encoder = str(resolution["encoder"])

        def media(info: MediaInfo) -> Path:
            return call.media(info.model_dump(mode="json", by_alias=True, exclude_none=True)).path

        def media_value(value: JsonValue) -> Path:
            return call.media(value).path

        def boxes_for(info: MediaInfo) -> BoxMap | RecordingBoxes | None:
            return clip_boxes(call, info)

        with scratch(call) as directory:
            out = directory / f"{call.node.id}.mp4"
            job = RenderJob(timeline=timeline, context=call.time(), media=media, media_value=media_value, out=out,
                            scratch=directory, encoder=encoder, progress=call.progress,
                            cancelled=call.cancel.is_set, boxes_for=boxes_for)
            detail = render(job, open_effect)
            value = ingest(call, out, kind="video", name=f"{call.node.id}.mp4",
                           meta={"encoder": encoder, "software": encoder in SOFTWARE_ENCODERS})
        return ExecResult(value=value, resolution=resolution, detail={**detail, "encoder": encoder})

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(EDIT_EXECUTOR, EDIT_EXECUTOR_VERSION, ops=("edit.render",), run=self.run,
                            resolver=self.resolve, locations=frozenset({"node"}), fonts=True,
                            runtime_version=runtime_version,
                            description="Composites an edit timeline frame by frame and encodes it")


def runtime_version() -> str:
    return f"ffmpeg {ffmpeg_version()}; skia {importlib.metadata.version('skia-python')}; " \
           f"numpy {importlib.metadata.version('numpy')}; scipy {importlib.metadata.version('scipy')}"


__all__ = ["EDIT_EXECUTOR", "EditRenderExecutor", "available_encoders", "choose_encoder"]
