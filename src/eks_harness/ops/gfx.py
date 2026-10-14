from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, cast

import numpy as np
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ExecContext,
    ExecResult,
    ExecutorSpec,
    ResolveContext,
    builtin_provenance,
)
from eks_harness.ir.ops.scene import GFX_RENDER_OP
from eks_harness.media.files import ffmpeg_version, ffprobe, ingest, scratch
from eks_harness.media.gfx.compile import compile_scene
from eks_harness.media.gfx.job import (
    PROBE_FLAG,
    PROGRESS_PREFIX,
    GfxResult,
    skia_version,
)

GFX_EXECUTOR = "eks.gfx"
GFX_EXECUTOR_VERSION = "1"
CHILD_MODULE = "eks_harness.media.gfx.child"
OUTPUT_NAME = "gfx.mov"
PROBE_TIMEOUT = 60.0
POLL_SECONDS = 0.25
_PROBES: dict[str, dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()


def runtime_version() -> str:
    return f"skia-python {skia_version()}; ffmpeg {ffmpeg_version()}"


def probe_gpu(python: str = sys.executable) -> dict[str, Any]:
    with _PROBE_LOCK:
        known = _PROBES.get(python)
        if known is not None:
            return known
        try:
            done = subprocess.run([python, "-m", CHILD_MODULE, PROBE_FLAG], capture_output=True, text=True,
                                  timeout=PROBE_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as error:
            raise NodeFailure("resolution_unavailable",
                              f"the GPU probe did not answer in {PROBE_TIMEOUT:.0f}s") from error
        try:
            found = cast(dict[str, Any], json.loads(done.stdout.strip().splitlines()[-1]))
        except (IndexError, ValueError) as error:
            raise NodeFailure("resolution_unavailable",
                              f"the GPU probe failed ({done.returncode}): {done.stderr[-800:]}") from error
        if not found.get("ok"):
            raise NodeFailure("resolution_unavailable",
                              f"gfx.render on the GPU needs an OpenGL context: {found.get('message')}; "
                              "render with cpu=True or run the node on a machine with a GPU")
        _PROBES[python] = found
        return found


def _video_size(path: Path) -> tuple[int, int]:
    data = ffprobe(path)
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
            return int(stream["width"]), int(stream["height"])
    raise NodeFailure("content_unavailable", f"{path.name} has no video stream for a gfx video layer")


class GfxExecutor:
    def __init__(self, *, python: str = sys.executable) -> None:
        self.python = python

    def spec(self) -> ExecutorSpec:
        return ExecutorSpec(GFX_EXECUTOR, GFX_EXECUTOR_VERSION, ops=(GFX_RENDER_OP,), run=self.run,
                            resolver=self.resolve, locations=frozenset({"node"}), runtime_version=runtime_version,
                            fonts=True, provenance=builtin_provenance(),
                            description="Renders gfx scenes with Skia: CPU raster for byte-identical goldens, or "
                                        "OpenGL (CGL on macOS, EGL on Linux) to a PNG-in-MOV clip with alpha")

    def _resolution(self, cpu: bool) -> dict[str, JsonValue]:
        if cpu:
            return {"renderer": "cpu", "skia": skia_version()}
        found = probe_gpu(self.python)
        gpu = cast(Mapping[str, Any], found["gpu"])
        return {"renderer": "gpu", "skia": skia_version(),
                "gpu": {"adapter": str(gpu["adapter"]), "driver": str(gpu["driver"]), "vendor": str(gpu["vendor"])}}

    def resolve(self, context: ResolveContext) -> Mapping[str, JsonValue]:
        return self._resolution(context.data.get("cpu") is True)

    def run(self, call: ExecContext) -> ExecResult:
        scene = call.data.get("scene")
        if not isinstance(scene, Mapping):
            raise NodeFailure("args_invalid", f"{GFX_RENDER_OP} {call.node.id} has no scene")
        cpu = call.data.get("cpu") is True
        resolution = self._resolution(cpu)
        context = call.time()
        with scratch(call) as work:
            compiled = compile_scene(cast(Mapping[str, Any], scene), context, cpu=cpu,
                                     media=lambda value: call.media(value).path, probe=_video_size, workdir=work)
            job = compiled.job
            np.savez(job.tracks, allow_pickle=False, **compiled.tracks)
            job_path = work / "job.json"
            job_path.write_text(job.model_dump_json(by_alias=True), encoding="utf-8")
            result = self._child(call, job_path, work, job.frames)
            if not result.ok:
                raise NodeFailure(result.code or "executor_error", result.message or "the gfx renderer failed",
                                  detail={"renderer": "cpu" if cpu else "gpu"})
            if result.gpu is not None and isinstance(resolution.get("gpu"), Mapping):
                predicted = cast(Mapping[str, Any], resolution["gpu"])
                if (result.gpu.get("adapter"), result.gpu.get("driver")) != (predicted["adapter"],
                                                                               predicted["driver"]):
                    raise NodeFailure(
                        "resolution_unavailable",
                        f"the renderer ran on {result.gpu.get('adapter')} ({result.gpu.get('driver')}), not on the "
                        f"resolved {predicted['adapter']} ({predicted['driver']})",
                        detail={"resolved": cast(JsonValue, dict(predicted)),
                                "used": cast(JsonValue, dict(result.gpu))})
            value = ingest(call, Path(job.output), kind="video", name=OUTPUT_NAME,
                           caption=f"gfx render {call.node.id}",
                           meta={"gfx": {"frames": job.frames, "fps": job.fps, "size": list(job.size),
                                         "renderer": result.renderer}})
        width, height = job.size
        return ExecResult(value=value, resolution=resolution,
                          detail={"frames": job.frames, "fps": job.fps, "size": [width, height],
                                  "renderer": result.renderer, "skia": result.skia})

    def _child(self, call: ExecContext, job_path: Path, work: Path, frames: int) -> GfxResult:
        log = work / "child.log"
        with log.open("wb") as errors:
            process = subprocess.Popen([self.python, "-m", CHILD_MODULE, str(job_path)], cwd=work,
                                       stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL)
            reader = threading.Thread(target=self._progress, args=(call, process.stdout, frames), daemon=True)
            reader.start()
            try:
                while True:
                    try:
                        process.wait(timeout=POLL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        call.check_cancelled()
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                reader.join(timeout=5.0)
                if process.stdout is not None:
                    process.stdout.close()
        result_path = Path(json.loads(job_path.read_text(encoding="utf-8"))["result"])
        if not result_path.exists():
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise NodeFailure("executor_error", f"the gfx renderer exited with {process.returncode} and wrote no "
                              f"result: {tail}")
        return GfxResult.model_validate_json(result_path.read_text(encoding="utf-8"))

    def _progress(self, call: ExecContext, stream: IO[bytes] | None, frames: int) -> None:
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith(PROGRESS_PREFIX):
                continue
            done = line.removeprefix(PROGRESS_PREFIX).split("/")[0]
            if done.isdigit():
                call.progress(min(int(done) / max(frames, 1), 1.0), f"gfx frame {done}/{frames}")


__all__ = ["GFX_EXECUTOR", "GFX_EXECUTOR_VERSION", "GfxExecutor", "probe_gpu", "runtime_version"]
