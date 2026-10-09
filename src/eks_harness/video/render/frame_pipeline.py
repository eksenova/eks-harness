"""Frame pipeline runtime.

A frame pipeline runs as three coupled processes:

1. ffmpeg subprocess **A** decodes the source media (with the leading
   ``graph_prefix`` filter chain folded in) and writes ``rawvideo`` to its
   stdout.
2. PyAV reads that rawvideo stream, hands each decoded frame to the
   :class:`FrameProcessor` chain, and writes the processed frames into the
   stdin of ffmpeg subprocess **B**.
3. ffmpeg subprocess **B** encodes the processed frames into the segment
   output file.

Cancellation is cooperative through a ``threading.Event`` reachable from the
render context. Progress fires on the pluggy ``progress`` hook every
``progress_interval`` frames.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import av
import numpy as np

from eks_harness.video.plugins.base import FrameProcessor
from eks_harness.video.plugins.manager import get_plugin_manager

from .progress import (
    NoopProgressReporter,
    ProgressEvent,
    ProgressReporter,
    emit_log,
)

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

__all__ = [
    "FramePipeline",
    "FramePipelineResult",
    "FramePipelineSpec",
]

_LOG = logging.getLogger(__name__)


class _PipeReader:
    # PyAV 17's PyIO bridge calls fseek whenever the file-like exposes a
    # `seek` attribute. On Windows that raises OSError(EINVAL) on a pipe
    # before PyAV can fall back to sequential reads. Wrap the decoder's
    # stdout so only `read` is exposed and PyAV treats it as non-seekable.
    __slots__ = ("_stream",)

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


@dataclass
class FramePipelineSpec:
    """Inputs / outputs of a frame-pipeline run.

    ``input_path`` is the source media file. ``graph_prefix_filter`` is the
    serialized filter string for ffmpeg subprocess A (decode side). It can be
    empty when no graph-compilable effects precede the frame-pipeline tail.
    ``output_path`` is the encoded segment file written by subprocess B.
    """

    input_path: Path
    output_path: Path
    width: int
    height: int
    fps: float
    duration: float
    pix_fmt: str = "bgr24"
    graph_prefix_filter: str = ""
    encoder_args: list[str] = field(default_factory=list)
    input_args: list[str] = field(default_factory=list)
    progress_interval: int = 10
    ffmpeg_binary: str = "ffmpeg"
    is_image: bool = False
    frame_start: int = 0
    frame_count: int | None = None


@dataclass
class FramePipelineResult:
    frames_written: int
    duration_s: float
    output_path: Path


class FramePipeline:
    """Per-segment frame pipeline executor."""

    def __init__(
        self,
        spec: FramePipelineSpec,
        processors: Iterable[FrameProcessor],
        ctx: RenderContext | None = None,
    ) -> None:
        self.spec = spec
        self.processors = list(processors)
        self.ctx = ctx
        self._reporter: ProgressReporter = self._resolve_reporter()
        self._segment_id: str | None = (
            str(ctx.extra.get("segment_id")) if ctx is not None and ctx.extra.get("segment_id") else None
        )
        self._frame_total_hint: int | None = (
            int(ctx.extra.get("segment_frame_total"))  # type: ignore[arg-type]
            if ctx is not None and ctx.extra.get("segment_frame_total")
            else None
        )

    def run(self) -> FramePipelineResult:
        cancel = self._cancel_event()
        decoder = self._spawn_decoder()
        encoder = self._spawn_encoder()
        # Drain both ffmpeg children's stderr on background threads. Without
        # this the OS pipe buffer (~64 KiB on Windows) fills as ffmpeg writes
        # warnings/stats, the child blocks on its next stderr write, and the
        # whole pipeline deadlocks - most visibly stalling between the last
        # decoded frame and the subsequent ``mux_audio`` step.
        decoder_stderr = _StderrPump(decoder)
        encoder_stderr = _StderrPump(encoder)

        frames_written = 0
        started = time.monotonic()
        in_container: Any = None
        out_container: Any = None
        out_stream: Any = None
        if self.spec.frame_count is not None:
            frame_total = max(1, self.spec.frame_count)
        else:
            frame_total = self._frame_total_hint or max(
                1, int(self.spec.duration * self.spec.fps)
            )
        emit_log(
            self._reporter,
            level="info",
            message=(
                f"frame_pipeline: {self.spec.width}x{self.spec.height} "
                f"@ {self.spec.fps:g} fps, {frame_total} frames"
            ),
            segment=self._segment_id,
        )

        last_emit_t = started
        last_emit_frame = 0
        emit_interval_s = 0.5
        emit_interval_frames = max(1, self.spec.progress_interval * 3)

        try:
            assert decoder.stdout is not None
            assert encoder.stdin is not None

            in_container = av.open(
                _PipeReader(decoder.stdout),
                mode="r",
                format="rawvideo",
                options={
                    "pixel_format": self.spec.pix_fmt,
                    "video_size": f"{self.spec.width}x{self.spec.height}",
                    "framerate": f"{self.spec.fps}",
                },
            )
            out_container = av.open(encoder.stdin, mode="w", format="rawvideo")
            out_stream = out_container.add_stream("rawvideo", rate=round(self.spec.fps))
            out_stream.width = self.spec.width
            out_stream.height = self.spec.height
            out_stream.pix_fmt = self.spec.pix_fmt

            frame_offset = self.spec.frame_start
            frame_limit = self.spec.frame_count
            frame_idx = 0
            window_done = False
            for packet in in_container.demux():
                if window_done:
                    break
                if packet.dts is None and packet.size == 0:
                    continue
                for frame in packet.decode():
                    if frame_limit is not None and frame_idx >= frame_limit:
                        window_done = True
                        break
                    if cancel is not None and cancel.is_set():
                        raise _CancelledError()
                    global_idx = frame_offset + frame_idx
                    t = global_idx / self.spec.fps
                    processed = self._apply_processors(frame, t, global_idx)
                    for out_packet in out_stream.encode(processed):
                        out_container.mux(out_packet)
                    frame_idx += 1
                    now = time.monotonic()
                    if (
                        frame_idx - last_emit_frame >= emit_interval_frames
                        or now - last_emit_t >= emit_interval_s
                    ):
                        self._emit_frame_event(
                            frame_idx=frame_idx,
                            frame_total=frame_total,
                            elapsed_since_emit=now - last_emit_t,
                            frames_since_emit=frame_idx - last_emit_frame,
                        )
                        last_emit_t = now
                        last_emit_frame = frame_idx
                    if frame_idx % self.spec.progress_interval == 0:
                        self._fire_progress(frame_idx)
            for out_packet in out_stream.encode(None):
                out_container.mux(out_packet)
            frames_written = frame_idx
            # Final frame event so consumers see 100% on this segment.
            self._emit_frame_event(
                frame_idx=frames_written,
                frame_total=max(frame_total, frames_written),
                elapsed_since_emit=max(time.monotonic() - last_emit_t, 1e-6),
                frames_since_emit=max(frames_written - last_emit_frame, 1),
            )
        finally:
            self._teardown(
                in_container,
                out_container,
                decoder,
                encoder,
                decoder_stderr=decoder_stderr,
                encoder_stderr=encoder_stderr,
            )

        return FramePipelineResult(
            frames_written=frames_written,
            duration_s=time.monotonic() - started,
            output_path=self.spec.output_path,
        )

    def _resolve_reporter(self) -> ProgressReporter:
        if self.ctx is None:
            return NoopProgressReporter()
        reporter = self.ctx.extra.get("progress_reporter")
        if isinstance(reporter, ProgressReporter) or hasattr(reporter, "emit"):
            return reporter  # type: ignore[return-value]
        return NoopProgressReporter()

    def _emit_frame_event(
        self,
        *,
        frame_idx: int,
        frame_total: int,
        elapsed_since_emit: float,
        frames_since_emit: int,
    ) -> None:
        fps_observed = (
            frames_since_emit / elapsed_since_emit if elapsed_since_emit > 0 else 0.0
        )
        remaining = max(frame_total - frame_idx, 0)
        eta_s = remaining / fps_observed if fps_observed > 0 else None
        payload: dict[str, Any] = {
            "step": "render_segments",
            "frame_index": frame_idx,
            "frame_total": frame_total,
            "fps_observed": round(fps_observed, 2),
        }
        if eta_s is not None:
            payload["eta_s"] = round(eta_s, 2)
        if self._segment_id is not None:
            payload["segment"] = self._segment_id
        self._reporter.emit(ProgressEvent("frame", payload=payload))

    def _apply_processors(
        self, frame: Any, t: float, frame_idx: int
    ) -> Any:
        if not self.processors:
            return frame
        array = frame.to_ndarray(format=self.spec.pix_fmt)
        for processor in self.processors:
            result = processor.process(array, t, frame_idx)
            array = result if isinstance(result, np.ndarray) else np.asarray(result)
        out = av.VideoFrame.from_ndarray(array, format=self.spec.pix_fmt)
        out.pts = frame.pts
        out.time_base = frame.time_base
        return out

    def _spawn_decoder(self) -> subprocess.Popen[bytes]:
        windowed = self.spec.frame_count is not None and self.spec.frame_start > 0
        if windowed and not self.spec.is_image:
            args = self._windowed_video_decoder_args()
        elif self.spec.frame_count is not None and self.spec.is_image:
            args = self._image_decoder_args(self.spec.frame_count)
        else:
            args = self._full_decoder_args()
        _LOG.debug("frame pipeline decoder: %s", " ".join(args))
        return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _full_decoder_args(self) -> list[str]:
        from . import hwaccel

        args = [
            self.spec.ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "error",
            *hwaccel.decode_args(),
            *self.spec.input_args,
            "-i", str(self.spec.input_path),
            "-t", f"{self.spec.duration:.6f}",
            "-r", f"{self.spec.fps}",
        ]
        if self.spec.graph_prefix_filter:
            args.extend(["-vf", self.spec.graph_prefix_filter])
        args.extend([
            "-pix_fmt", self.spec.pix_fmt,
            "-f", "rawvideo",
            "-",
        ])
        return args

    def _image_decoder_args(self, frame_count: int) -> list[str]:
        # A looped still is constant across the segment, so a chunk worker
        # only needs to emit its own ``frame_count`` frames; their content is
        # identical to the matching frames of the serial decode. The base
        # ``scale`` graph prefix is time-invariant, so no window offset is
        # required for correctness.
        chunk_duration = frame_count / self.spec.fps
        args = [
            self.spec.ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "error",
            *self.spec.input_args,
            "-i", str(self.spec.input_path),
            "-t", f"{chunk_duration:.6f}",
            "-r", f"{self.spec.fps}",
        ]
        if self.spec.graph_prefix_filter:
            args.extend(["-vf", self.spec.graph_prefix_filter])
        args.extend([
            "-frames:v", str(frame_count),
            "-pix_fmt", self.spec.pix_fmt,
            "-f", "rawvideo",
            "-",
        ])
        return args

    def _windowed_video_decoder_args(self) -> list[str]:
        from . import hwaccel

        assert self.spec.frame_count is not None
        start = self.spec.frame_start
        end = start + self.spec.frame_count - 1
        # Normalise the source to the segment's CFR grid *inside the filter
        # graph* (``fps=``) before counting frames, so ``select`` numbers
        # frames on the same grid the serial decode walks even for VFR
        # sources. Then select the contiguous window and reset timestamps so
        # PyAV reads a clean 0-based rawvideo stream. Re-decoding the leading
        # frames is cheap relative to the Python effect chain.
        normalise = f"fps={self.spec.fps}"
        select = f"select='between(n\\,{start}\\,{end})',setpts=N/FRAME_RATE/TB"
        prefix = self.spec.graph_prefix_filter
        parts = [normalise]
        if prefix:
            parts.append(prefix)
        parts.append(select)
        vf = ",".join(parts)
        args = [
            self.spec.ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "error",
            *hwaccel.decode_args(),
            *self.spec.input_args,
            "-i", str(self.spec.input_path),
            "-vf", vf,
            "-frames:v", str(self.spec.frame_count),
            "-pix_fmt", self.spec.pix_fmt,
            "-f", "rawvideo",
            "-",
        ]
        return args

    def _spawn_encoder(self) -> subprocess.Popen[bytes]:
        args = [
            self.spec.ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", self.spec.pix_fmt,
            "-s", f"{self.spec.width}x{self.spec.height}",
            "-r", f"{self.spec.fps}",
            "-i", "-",
        ]
        if self.spec.encoder_args:
            args.extend(self.spec.encoder_args)
        else:
            from . import hwaccel

            args.extend([*hwaccel.encode_args(), "-pix_fmt", "yuv420p"])
        args.extend([
            "-an",
            "-t", f"{self.spec.duration:.6f}",
            str(self.spec.output_path),
        ])
        _LOG.debug("frame pipeline encoder: %s", " ".join(args))
        return subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def _cancel_event(self) -> threading.Event | None:
        if self.ctx is None:
            return None
        cancel = self.ctx.extra.get("cancel")
        if isinstance(cancel, threading.Event):
            return cancel
        return None

    def _fire_progress(self, frame_idx: int) -> None:
        try:
            get_plugin_manager().hook.progress(
                event={
                    "stage": "frame_pipeline",
                    "frames": frame_idx,
                    "output": str(self.spec.output_path),
                }
            )
        except Exception:
            _LOG.debug("progress hook raised", exc_info=True)

    def _teardown(
        self,
        in_container: Any,
        out_container: Any,
        decoder: subprocess.Popen[bytes],
        encoder: subprocess.Popen[bytes],
        *,
        decoder_stderr: "_StderrPump | None" = None,
        encoder_stderr: "_StderrPump | None" = None,
    ) -> None:
        for processor in self.processors:
            try:
                processor.close()
            except Exception:
                _LOG.debug("processor.close() raised", exc_info=True)

        if out_container is not None:
            try:
                out_container.close()
            except Exception:
                _LOG.debug("output container close raised", exc_info=True)
        if in_container is not None:
            try:
                in_container.close()
            except Exception:
                _LOG.debug("input container close raised", exc_info=True)

        pumps = {
            "decoder": decoder_stderr,
            "encoder": encoder_stderr,
        }
        for proc, name in ((decoder, "decoder"), (encoder, "encoder")):
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            if proc.stdout is not None:
                with contextlib.suppress(Exception):
                    proc.stdout.close()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            pump = pumps.get(name)
            stderr_bytes = pump.join() if pump is not None else b""
            if proc.returncode not in (0, None):
                _LOG.warning(
                    "ffmpeg %s exited with %s: %s",
                    name,
                    proc.returncode,
                    stderr_bytes.decode("utf-8", errors="replace")[-400:],
                )


class _CancelledError(RuntimeError):
    pass


class _StderrPump:
    """Background drainer for a child process's ``stderr`` pipe.

    Reads ``proc.stderr`` on a daemon thread so the OS pipe buffer never
    fills while ffmpeg is producing diagnostic output during a long
    encode/decode. Without this drain a 64 KiB-bound write on Windows
    blocks the child, which in turn stalls the rawvideo bridge and looks
    to upstream consumers like the renderer is hanging at ``mux_audio``
    (the step that follows ``render_segments``).
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._chunks: list[bytes] = []
        self._thread: threading.Thread | None = None
        if proc.stderr is None:
            return
        self._thread = threading.Thread(
            target=self._drain, name="frame-pipeline-stderr", daemon=True
        )
        self._thread.start()

    def _drain(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self._chunks.append(chunk)
        except Exception:
            _LOG.debug("stderr pump read failed", exc_info=True)

    def join(self, timeout: float = 5.0) -> bytes:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return b"".join(self._chunks)
