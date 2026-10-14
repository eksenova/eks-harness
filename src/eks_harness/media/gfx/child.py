from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import IO, Any

import numpy as np

from eks_harness.media.files import BITEXACT_OUTPUT
from eks_harness.media.gfx.draw import Renderer, RenderError
from eks_harness.media.gfx.gpu import GlContext, GpuUnavailable, open_gl
from eks_harness.media.gfx.job import (
    PROBE_FLAG,
    PROGRESS_PREFIX,
    GfxJob,
    GfxResult,
    skia_version,
)
from eks_harness.media.gfx.video import VideoDecodeError

PROGRESS_EVERY = 10


class _Target:
    def __init__(self, job: GfxJob) -> None:
        self.skia: Any = importlib.import_module("skia")
        width, height = job.size
        info = self.skia.ImageInfo.MakeN32Premul(width, height)
        self.gl: GlContext | None = None
        self.context: Any = None
        if job.cpu:
            self.surface = self.skia.Surface.MakeRaster(info)
        else:
            self.gl = open_gl()
            self.context = self.skia.GrDirectContext.MakeGL()
            if self.context is None:
                raise GpuUnavailable(f"Skia cannot create a GL context on {self.gl.adapter} ({self.gl.driver})")
            self.surface = self.skia.Surface.MakeRenderTarget(self.context, self.skia.Budgeted.kNo, info)
        if self.surface is None:
            raise GpuUnavailable(f"Skia cannot allocate a {width}x{height} surface")
        self.read_info = self.skia.ImageInfo.Make(width, height, self.skia.ColorType.kRGBA_8888_ColorType,
                                                  self.skia.AlphaType.kUnpremul_AlphaType)
        self.buffer = np.empty((height, width, 4), dtype=np.uint8)

    def pixels(self) -> bytes:
        if self.context is not None:
            self.context.flushAndSubmit()
        if not self.surface.readPixels(self.read_info, self.buffer, self.buffer.shape[1] * 4, 0, 0):
            raise RenderError("executor_error", "Skia could not read the rendered frame back")
        return self.buffer.tobytes()


def _encoder(job: GfxJob) -> subprocess.Popen[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RenderError("resolution_unavailable", "ffmpeg is not installed on this machine; install ffmpeg")
    width, height = job.size
    threads = str(job.encode_threads)
    argv = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-threads", threads,
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-framerate", job.fps, "-i", "-",
            *BITEXACT_OUTPUT, "-threads", threads, "-c:v", "png", "-pix_fmt", "rgba", "-f", "mov", job.output]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _finish(encoder: subprocess.Popen[bytes]) -> None:
    stdin: IO[bytes] | None = encoder.stdin
    if stdin is not None:
        try:
            stdin.close()
        except BrokenPipeError:
            pass
    stderr = encoder.stderr.read().decode("utf-8", "replace") if encoder.stderr is not None else ""
    if encoder.wait() != 0:
        raise RenderError("executor_error", f"ffmpeg could not encode the gfx render: {stderr[-1500:]}")


def render(job: GfxJob) -> GfxResult:
    with np.load(job.tracks) as archive:
        tracks = {name: archive[name] for name in archive.files}
    target = _Target(job)
    renderer = Renderer(job, tracks)
    encoder = _encoder(job)
    try:
        canvas = target.surface.getCanvas()
        stdin = encoder.stdin
        if stdin is None:
            raise RenderError("executor_error", "the ffmpeg encoder has no input pipe")
        for frame in range(job.frames):
            renderer.draw_frame(canvas, frame)
            try:
                stdin.write(target.pixels())
            except BrokenPipeError:
                break
            if (frame + 1) % PROGRESS_EVERY == 0 or frame + 1 == job.frames:
                print(f"{PROGRESS_PREFIX}{frame + 1}/{job.frames}", flush=True)
    finally:
        renderer.close()
        _finish(encoder)
    return GfxResult(ok=True, frames=job.frames, renderer="cpu" if job.cpu else "gpu",
                     gpu=target.gl.identity() if target.gl is not None else None, skia=skia_version())


def probe() -> dict[str, Any]:
    try:
        context = open_gl()
    except GpuUnavailable as error:
        return {"ok": False, "message": str(error)}
    return {"ok": True, "gpu": context.identity(), "skia": skia_version()}


def main(argv: list[str]) -> int:
    if argv == [PROBE_FLAG]:
        print(json.dumps(probe()), flush=True)
        return 0
    if len(argv) != 1:
        print("usage: python -m eks_harness.media.gfx.child JOB.json | --probe", file=sys.stderr)
        return 2
    job = GfxJob.model_validate_json(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        result = render(job)
    except GpuUnavailable as error:
        result = GfxResult(ok=False, code="resolution_unavailable", message=str(error))
    except RenderError as error:
        result = GfxResult(ok=False, code=error.code, message=error.message)
    except VideoDecodeError as error:
        result = GfxResult(ok=False, code="content_unavailable", message=str(error))
    except Exception as error:  # noqa: BLE001
        tail = traceback.format_exc()[-1500:]
        result = GfxResult(ok=False, code="executor_error", message=f"{type(error).__name__}: {error}\n{tail}")
    Path(job.result).write_text(result.model_dump_json(by_alias=True), encoding="utf-8")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
