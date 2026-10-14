from __future__ import annotations

import importlib
import math
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.hashing import derived_seed
from eks_harness.ir.ops.edit import Clip, RenderSettings, Timeline, Track
from eks_harness.ir.time import TICKS_PER_SECOND, TimeContext, TimeError, TimeSet, Word
from eks_harness.ir.types import MediaInfo
from eks_harness.media.animate import Timebase, color_rgba, floats
from eks_harness.media.audio.limiter import limit
from eks_harness.media.audio.loudness import true_peak_db
from eks_harness.media.edit.overlays import (
    BlurItem,
    BoxesFor,
    OverlayPlanner,
    apply_emphasis,
    blur_frame,
)
from eks_harness.media.edit.sources import ClipSource
from eks_harness.media.edit.timing import (
    TIMING_EFFECTS,
    ClipTiming,
    frame_of,
    neighbours,
    plan_clips,
)
from eks_harness.media.edit.transitions import TransitionFrame, blend
from eks_harness.media.files import BITEXACT_OUTPUT, ffmpeg, probe, tool
from eks_harness.media.overlays.video import render_overlay_video

skia: Any = importlib.import_module("skia")
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "192k"
CODEC_PASSES = 4
CODEC_TOLERANCE_DB = 0.05
BLENDS: Mapping[str, Any] = {
    "normal": skia.BlendMode.kSrcOver, "add": skia.BlendMode.kPlus, "multiply": skia.BlendMode.kMultiply,
    "screen": skia.BlendMode.kScreen, "overlay": skia.BlendMode.kOverlay,
}
ENCODERS: Mapping[str, str] = {
    "libx264": "libx264", "libsvtav1": "libsvtav1", "videotoolbox": "h264_videotoolbox", "nvenc": "h264_nvenc",
    "qsv": "h264_qsv", "amf": "h264_amf",
}
X264_PRESETS = {"preview": "ultrafast", "draft": "veryfast", "final": "medium"}
X264_CRF = {"preview": 30, "draft": 24, "final": 18}
SVT_PRESETS = {"preview": "12", "draft": "8", "final": "6"}
SVT_CRF = {"preview": 45, "draft": 38, "final": 30}
CLOCK_STREAMS = frozenset({"beat", "downbeat", "bar"})
MediaPath = Callable[[MediaInfo], Path]
EffectOpener = Callable[..., Any]


@dataclass
class OverlayLayer:
    track: int
    source: ClipSource


@dataclass
class RenderJob:
    timeline: Timeline
    context: TimeContext
    media: MediaPath
    media_value: Callable[[JsonValue], Path]
    out: Path
    scratch: Path
    encoder: str
    boxes_for: BoxesFor | None = None
    progress: Callable[[float, str], None] = lambda fraction, message: None
    cancelled: Callable[[], bool] = lambda: False


@dataclass
class _ClipState:
    timing: ClipTiming
    source: ClipSource
    effects: list[tuple[str, Any]]


def _info(value: Any) -> MediaInfo:
    if isinstance(value, MediaInfo):
        return value
    return MediaInfo.model_validate(value)


def _tracks(timeline: Timeline) -> list[tuple[int, Track]]:
    return sorted(enumerate(timeline.tracks), key=lambda item: (item[1].z, item[0]))


def _transition(timing: ClipTiming, timings: Sequence[ClipTiming], fps: Fraction,
                context: TimeContext) -> list[tuple[int, int, Any, ClipTiming | None, ClipTiming | None]]:
    windows: list[tuple[int, int, Any, ClipTiming | None, ClipTiming | None]] = []
    previous, following = neighbours(timings, timing)
    clip = timing.clip
    incoming = clip.transition_in
    if incoming is not None and incoming.kind != "cut":
        if previous is not None and previous.clip.transition_out is not None \
                and previous.clip.transition_out.kind != "cut":
            raise NodeFailure("args_invalid", f"clips {previous.clip.id} and {clip.id} both set a transition between "
                              "them; keep transition_out on the first or transition_in on the second")
        length = frame_of(_duration(incoming, context), fps)
        windows.append((timing.first, timing.first + length, incoming, previous, timing))
    outgoing = clip.transition_out
    if outgoing is not None and outgoing.kind != "cut":
        length = frame_of(_duration(outgoing, context), fps)
        if following is not None:
            windows.append((following.first, following.first + length, outgoing, timing, following))
        else:
            windows.append((timing.end - length, timing.end, outgoing, timing, None))
    return windows


def _duration(transition: Any, context: TimeContext) -> int:
    try:
        return context.resolve(transition.duration)
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"transition {transition.kind}: {error}") from error


def _beats(transition: Any, context: TimeContext, fps: Fraction) -> list[float]:
    stream = getattr(transition, "stream", None)
    if stream is None:
        return []
    try:
        if stream in CLOCK_STREAMS:
            ticks = context.resolve_set(TimeSet(stream=stream))
        else:
            ticks = list(context.markers.get(stream, ()))
    except TimeError as error:
        raise NodeFailure("time_unresolved", f"transition {transition.kind} stream {stream}: {error}") from error
    return [value / TICKS_PER_SECOND for value in ticks]


class Compositor:
    def __init__(self, job: RenderJob, effect_opener: EffectOpener) -> None:
        self.job = job
        self.timeline = job.timeline
        self.fps = Fraction(job.timeline.fps)
        self.width, self.height = job.timeline.size
        self.open_effect = effect_opener
        self.infos = {clip.id: _info(clip.media) for track in job.timeline.tracks for clip in track.clips}
        self.timings, self.total = plan_clips(job.timeline, job.context, self.infos, None)
        self.states: dict[str, _ClipState] = {}
        self.windows: list[tuple[int, int, Any, ClipTiming | None, ClipTiming | None, int]] = []
        for timing in self.timings:
            for start, end, transition, before, after in _transition(timing, self.timings, self.fps, job.context):
                self.windows.append((start, end, transition, before, after, timing.track_index))
        self.beats = {id(window[2]): _beats(window[2], job.context, self.fps) for window in self.windows}
        self.background = color_rgba(job.timeline.background)
        self.track_bases = {index: Timebase(context=job.context, origin=0, length=None,
                                            seed=derived_seed(job.timeline.seed, f"track:{track.name}"),
                                            path=f"track:{track.name}")
                            for index, track in enumerate(job.timeline.tracks)}
        self.clip_values = {timing.clip.id: self._clip_values(timing) for timing in self.timings}
        self.overlays: list[OverlayLayer] = []
        self.blurs: list[BlurItem] = []
        self._plan_overlays()
        self.track_opacity = {index: floats(track.opacity, self.track_bases[index],
                                            np.arange(self.total, dtype=np.float64) / float(self.fps))
                              for index, track in enumerate(job.timeline.tracks)}

    def _plan_overlays(self) -> None:
        tracks = [(index, track) for index, track in enumerate(self.timeline.tracks) if track.overlays]
        if not tracks:
            return
        planner = OverlayPlanner(context=self.job.context, fps=self.fps, size=(self.width, self.height),
                                 total=self.total, timings=self.timings, infos=self.infos,
                                 z_of={index: track.z for index, track in enumerate(self.timeline.tracks)},
                                 values_for=lambda timing: self.clip_values[timing.clip.id],
                                 boxes_for=self.job.boxes_for or (lambda info: None), media=self.job.media_value)
        for index, track in tracks:
            planned = planner.plan(index, list(track.overlays))
            apply_emphasis(planned.emphasis)
            self.blurs.extend(planned.blurs)
            if not planned.items:
                continue
            out = self.job.scratch / f"overlays-{index}.mov"
            render_overlay_video(planned.items, size=(self.width, self.height), fps=self.fps, frames=self.total,
                                 style=None, out=out, threads=self.timeline.render.threads)
            info = probe(out, f"file:{out.name}", kind="video")
            self.overlays.append(OverlayLayer(track=index, source=ClipSource(
                out, info, fit="fill", frame=(self.width, self.height), clip=f"overlays of track {track.name}")))

    def _clip_values(self, timing: ClipTiming) -> dict[str, NDArray[np.float64]]:
        local = np.arange(timing.count, dtype=np.float64) / float(self.fps)
        clip = timing.clip
        return {name: floats(getattr(clip, name), timing.base, local)
                for name in ("opacity", "x", "y", "scale", "rotation")}

    def _state(self, timing: ClipTiming) -> _ClipState:
        found = self.states.get(timing.clip.id)
        if found is not None:
            return found
        clip = timing.clip
        info = self.infos[clip.id]
        source = ClipSource(self.job.media(info), info, fit=clip.fit, frame=(self.width, self.height), clip=clip.id)
        words: dict[str, Sequence[Word]] = {name: list(items) for name, items in self.job.context.words.items()}
        effects: list[tuple[str, Any]] = []
        for index, effect in enumerate(clip.effects):
            if effect.kind in TIMING_EFFECTS:
                continue
            seed = derived_seed(self.timeline.seed, f"{clip.id}/effect:{index}")
            base = Timebase(context=timing.base.context, origin=timing.base.origin, length=timing.base.length,
                            seed=seed, path=f"{clip.id}/effect:{index}")
            runtime = self.open_effect(effect, base=base, fps=self.fps, frames=range(timing.first, timing.end),
                                       size=source.box, media=self.job.media_value, words=words)
            effects.append((effect.kind, runtime))
        state = _ClipState(timing=timing, source=source, effects=effects)
        self.states[clip.id] = state
        return state

    def _retire(self, frame: int) -> None:
        for clip_id, state in list(self.states.items()):
            last = max([state.timing.end, *(end for _start, end, _t, before, after, _track in self.windows
                                             if state.timing in (before, after))])
            if frame >= last:
                self._close_state(state)
                del self.states[clip_id]

    def _close_state(self, state: _ClipState) -> None:
        state.source.close()
        for _kind, runtime in state.effects:
            runtime.close()

    def _image(self, timing: ClipTiming, frame: int) -> NDArray[np.uint8]:
        from eks_harness.media.edit.effects import EffectFrame

        state = self._state(timing)
        at = min(max(frame, timing.first), timing.end - 1)
        image = state.source.frame(float(timing.source[at - timing.first]))
        if state.effects:
            moment = EffectFrame(index=at, seconds=at / float(self.fps), local=(at - timing.first) / float(self.fps))
            image = np.ascontiguousarray(image)
            for _kind, runtime in state.effects:
                image = runtime.apply(image, moment)
        return np.ascontiguousarray(image)

    def _draw_clip(self, canvas: Any, timing: ClipTiming, frame: int) -> None:
        image = self._image(timing, frame)
        values = self.clip_values[timing.clip.id]
        local = min(max(frame - timing.first, 0), timing.count - 1)
        opacity = float(np.clip(values["opacity"][local], 0.0, 1.0))
        if opacity <= 0.0:
            return
        picture = skia.Image.fromarray(image, colorType=skia.ColorType.kRGBA_8888_ColorType,
                                       alphaType=skia.AlphaType.kUnpremul_AlphaType)
        height, width = image.shape[:2]
        canvas.save()
        canvas.translate(self.width / 2.0 + float(values["x"][local]), self.height / 2.0 + float(values["y"][local]))
        canvas.rotate(float(values["rotation"][local]))
        scale = float(values["scale"][local])
        canvas.scale(scale, scale)
        paint = skia.Paint(Alphaf=opacity)
        canvas.drawImage(picture, -width / 2.0, -height / 2.0, skia.SamplingOptions(skia.FilterMode.kLinear), paint)
        canvas.restore()

    def _layer(self, timing: ClipTiming | None, frame: int) -> NDArray[np.float64]:
        if timing is None:
            return np.zeros((self.height, self.width, 4), dtype=np.float64)
        surface = skia.Surface(self.width, self.height)
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorTRANSPARENT)
        self._draw_clip(canvas, timing, frame)
        pixels = surface.makeImageSnapshot().toarray(colorType=skia.ColorType.kRGBA_8888_ColorType,
                                                     alphaType=skia.AlphaType.kPremul_AlphaType)
        return pixels.astype(np.float64) / 255.0

    def _draw_track(self, canvas: Any, index: int, track: Track, frame: int) -> None:
        windows = [window for window in self.windows if window[5] == index and window[0] <= frame < window[1]]
        handled: set[str] = set()
        for start, end, transition, before, after, _track in windows:
            progress = (frame - start + 0.5) / max(end - start, 1)
            owner = after if after is not None else before
            seed = derived_seed(self.timeline.seed, f"{owner.clip.id if owner is not None else track.name}/transition")
            moment = TransitionFrame(progress=progress, index=frame, seconds=frame / float(self.fps), seed=seed,
                                     beats=self.beats.get(id(transition), []))
            mixed = blend(transition, self._layer(before, frame), self._layer(after, frame), moment)
            data = np.ascontiguousarray(np.clip(np.rint(mixed * 255.0), 0, 255).astype(np.uint8))
            picture = skia.Image.fromarray(data, colorType=skia.ColorType.kRGBA_8888_ColorType,
                                           alphaType=skia.AlphaType.kPremul_AlphaType)
            canvas.drawImage(picture, 0, 0)
            for timing in (before, after):
                if timing is not None:
                    handled.add(timing.clip.id)
        for timing in self.timings:
            if timing.track_index == index and timing.covers(frame) and timing.clip.id not in handled:
                self._draw_clip(canvas, timing, frame)

    def _finish_track(self, surface: Any, index: int, frame: int) -> None:
        seconds = frame / float(self.fps)
        canvas = surface.getCanvas()
        blurs = [item for item in self.blurs if item.track == index and item.start <= seconds < item.end]
        if blurs:
            pixels = surface.makeImageSnapshot().toarray(colorType=skia.ColorType.kRGBA_8888_ColorType,
                                                         alphaType=skia.AlphaType.kUnpremul_AlphaType)
            blurred = np.ascontiguousarray(blur_frame(pixels, blurs, seconds))
            picture = skia.Image.fromarray(blurred, colorType=skia.ColorType.kRGBA_8888_ColorType,
                                           alphaType=skia.AlphaType.kUnpremul_AlphaType)
            canvas.drawImage(picture, 0, 0, skia.SamplingOptions(), skia.Paint(BlendMode=skia.BlendMode.kSrc))
        for layer in self.overlays:
            if layer.track == index:
                image = layer.source.frame(seconds)
                picture = skia.Image.fromarray(np.ascontiguousarray(image),
                                               colorType=skia.ColorType.kRGBA_8888_ColorType,
                                               alphaType=skia.AlphaType.kUnpremul_AlphaType)
                canvas.drawImage(picture, 0, 0)

    def frame(self, frame: int) -> bytes:
        surface = skia.Surface(self.width, self.height)
        canvas = surface.getCanvas()
        red, green, blue, alpha = (float(value) for value in self.background)
        canvas.clear(skia.Color4f(red, green, blue, alpha))
        for index, track in _tracks(self.timeline):
            opacity = float(np.clip(self.track_opacity[index][frame], 0.0, 1.0))
            if opacity <= 0.0:
                self._finish_track(surface, index, frame)
                continue
            paint = skia.Paint(Alphaf=opacity, BlendMode=BLENDS[track.blend])
            canvas.saveLayer(None, paint)
            self._draw_track(canvas, index, track, frame)
            canvas.restore()
            self._finish_track(surface, index, frame)
        self._retire(frame + 1)
        pixels = surface.makeImageSnapshot().toarray(colorType=skia.ColorType.kRGBA_8888_ColorType,
                                                     alphaType=skia.AlphaType.kUnpremul_AlphaType)
        return pixels.tobytes()

    def close(self) -> None:
        for state in self.states.values():
            self._close_state(state)
        self.states.clear()
        for layer in self.overlays:
            layer.source.close()


def _decode_audio(path: Path, scratch: Path, name: str, start: float = 0.0,
                  length: float | None = None) -> NDArray[np.float64]:
    target = scratch / f"{name}.f32"
    inputs = ["-ss", f"{start:.6f}"] if start > 0 else []
    inputs += ["-i", str(path)]
    outputs = ["-vn", "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_RATE), "-f", "f32le", "-c:a", "pcm_f32le"]
    if length is not None:
        outputs += ["-t", f"{length:.6f}"]
    ffmpeg(inputs, [*outputs, str(target)])
    return np.fromfile(target, dtype="<f4").astype(np.float64).reshape(-1, AUDIO_CHANNELS)


def _has_audio(path: Path) -> bool:
    done = subprocess.run([tool("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries",
                           "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                          timeout=60, check=False)
    return bool(done.stdout.strip())


def mix_audio(job: RenderJob, compositor: Compositor) -> Path | None:
    seconds = compositor.total / float(compositor.fps)
    samples = math.ceil(seconds * AUDIO_RATE)
    bus = np.zeros((samples, AUDIO_CHANNELS), dtype=np.float64)
    used = False
    if job.timeline.audio is not None:
        info = _info(job.timeline.audio)
        decoded = _decode_audio(job.media(info), job.scratch, "timeline-audio")
        count = min(samples, decoded.shape[0])
        bus[:count] += decoded[:count]
        used = True
    for timing in compositor.timings:
        clip: Clip = timing.clip
        if not clip.audio:
            continue
        info = compositor.infos[clip.id]
        path = job.media(info)
        if not _has_audio(path):
            raise NodeFailure("args_invalid", f"clip {clip.id} plays its own audio, but {info.uri} has none")
        first_source = float(timing.source[0])
        last_source = float(timing.source[-1]) + 1.0 / float(compositor.fps)
        decoded = _decode_audio(path, job.scratch, f"clip-{timing.track_index}-{timing.clip_index}",
                                first_source, last_source - first_source + 0.1)
        start = round(timing.first / float(compositor.fps) * AUDIO_RATE)
        count = min(round(timing.count / float(compositor.fps) * AUDIO_RATE), samples - start)
        if count <= 0:
            continue
        sample_times = (np.arange(count, dtype=np.float64) / AUDIO_RATE) * float(compositor.fps)
        frame_positions = np.interp(sample_times, np.arange(timing.count, dtype=np.float64),
                                    timing.source - first_source)
        index = frame_positions * AUDIO_RATE
        low = np.clip(np.floor(index).astype(np.int64), 0, decoded.shape[0] - 1)
        high = np.clip(low + 1, 0, decoded.shape[0] - 1)
        frac = (index - np.floor(index))[:, None]
        piece = decoded[low] * (1.0 - frac) + decoded[high] * frac
        gain_db = floats(clip.gain, timing.base, np.arange(timing.count, dtype=np.float64) / float(compositor.fps))
        gains = np.interp(sample_times, np.arange(timing.count, dtype=np.float64), gain_db)
        bus[start:start + count] += piece * np.power(10.0, gains / 20.0)[:, None]
        used = True
    if not used:
        return None
    return encode_audio(bus, job.scratch)


def _aac(bus: NDArray[np.float64], scratch: Path) -> tuple[Path, NDArray[np.float64]]:
    raw = scratch / "edit-audio.f32"
    np.clip(bus, -1.0, 1.0).astype("<f4").tofile(raw)
    target = scratch / "edit-audio.m4a"
    ffmpeg(["-f", "f32le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-i", str(raw)],
           ["-c:a", "aac", "-b:a", AUDIO_BITRATE, str(target)])
    decoded = scratch / "edit-audio-check.f32"
    ffmpeg(["-i", str(target)], ["-f", "f32le", "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_RATE), str(decoded)])
    return target, np.fromfile(decoded, dtype="<f4").astype(np.float64).reshape(-1, AUDIO_CHANNELS)


def encode_audio(bus: NDArray[np.float64], scratch: Path) -> Path:
    ceiling = true_peak_db(bus)
    current = bus
    limit_db = ceiling
    target, decoded = _aac(current, scratch)
    for _attempt in range(CODEC_PASSES):
        overshoot = true_peak_db(decoded) - ceiling
        if overshoot <= CODEC_TOLERANCE_DB:
            break
        limit_db -= overshoot
        current = limit(bus, AUDIO_RATE, limit_db)
        target, decoded = _aac(current, scratch)
    return target


def encoder_args(settings: RenderSettings, encoder: str) -> list[str]:
    codec = ENCODERS[encoder]
    args = ["-c:v", codec, "-threads", str(settings.threads), "-pix_fmt", settings.pix_fmt,
            "-color_primaries", settings.color_primaries, "-color_trc", settings.color_trc,
            "-colorspace", settings.colorspace]
    if codec == "libx264":
        args += ["-preset", X264_PRESETS[settings.quality], "-x264-params", f"threads={settings.threads}"]
    elif codec == "libsvtav1":
        args += ["-preset", SVT_PRESETS[settings.quality],
                 "-svtav1-params", f"lp={settings.threads}"]
    if settings.bitrate_kbps is not None:
        args += ["-b:v", f"{settings.bitrate_kbps}k"]
    elif codec == "libx264":
        args += ["-crf", str(settings.crf if settings.crf is not None else X264_CRF[settings.quality])]
    elif codec == "libsvtav1":
        args += ["-crf", str(settings.crf if settings.crf is not None else SVT_CRF[settings.quality])]
    elif settings.crf is not None:
        args += ["-q:v", str(settings.crf)]
    return args


def render(job: RenderJob, effect_opener: EffectOpener) -> dict[str, JsonValue]:
    compositor = Compositor(job, effect_opener)
    try:
        audio = mix_audio(job, compositor)
        video_input = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{compositor.width}x{compositor.height}",
                       "-r", str(compositor.fps), "-i", "pipe:0"]
        inputs = [*video_input, *(["-i", str(audio)] if audio is not None else [])]
        mapping = ["-map", "0:v:0", *(["-map", "1:a:0", "-c:a", "copy"] if audio is not None else [])]
        seconds = compositor.total / float(compositor.fps)
        argv = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *inputs, *BITEXACT_OUTPUT,
                *encoder_args(job.timeline.render, job.encoder), *mapping, "-t", f"{seconds:.6f}",
                "-movflags", "+faststart", str(job.out)]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert process.stdin is not None
        try:
            for frame in range(compositor.total):
                if job.cancelled():
                    raise NodeFailure("cancelled", "the render was cancelled")
                process.stdin.write(compositor.frame(frame))
                if frame % 10 == 0:
                    job.progress(frame / compositor.total, f"frame {frame} of {compositor.total}")
            process.stdin.close()
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code != 0:
            stderr = cast(bytes, process.stderr.read() if process.stderr is not None else b"")
            raise NodeFailure("executor_error", f"the encoder failed ({code}): {stderr.decode()[-1200:]}")
        return {"frames": compositor.total, "seconds": seconds, "audio": audio is not None}
    finally:
        compositor.close()


__all__ = ["AUDIO_RATE", "ENCODERS", "Compositor", "OverlayLayer", "RenderJob", "encoder_args", "mix_audio",
           "render"]
