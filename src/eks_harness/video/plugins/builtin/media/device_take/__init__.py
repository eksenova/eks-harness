from __future__ import annotations

import colorsys
import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field

from eks_harness.video.ir.media import PathLike, _MediaBase, register_media_model
from eks_harness.video.plugins.base import MediaRenderer, MediaRenderRequest

if TYPE_CHECKING:
    from eks_harness.video.render.context import RenderContext

log = logging.getLogger("eks_harness.video.device_take")

__all__ = ["DeviceTake", "DeviceTakeRenderer", "beacon_frames", "resolve_cue", "retime", "synced_marks",
           "warp_points"]

BEACON_HUES = [0, 120, 240, 60, 300, 180]
BEACON_SCAN = 64
BEACON_PATCH = (9, 17)
NO_PACING = {"moveMs": 0, "dwellMs": 0, "typeMsPerChar": 0, "holdMs": 0, "screenHoldMs": 0, "mobilePressMs": 0}
OPAQUE = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]


class DeviceTake(_MediaBase):
    kind: Literal["device_take"] = "device_take"
    flow: PathLike
    platform: Literal["ios", "android", "web"] = "ios"
    params: dict[str, str] = Field(default_factory=dict)
    cues: dict[str, float | str] = Field(default_factory=dict)
    pace: str | dict[str, int] | None = None
    phone: bool = False
    tree: PathLike | None = None
    beacons: bool | None = None
    refresh: bool = False


register_media_model(DeviceTake)


def resolve_cue(target: float | str, markers: dict) -> float:
    if isinstance(target, (int, float)):
        return float(target)
    head, _, rest = str(target).partition(":")
    if head == "named":
        name, _, index = rest.partition(":")
        times = (markers.get("named") or {}).get(name, [])
        idx = int(index or 0)
    else:
        times = (markers.get("streams") or {}).get(head, [])
        idx = int(rest or 0)
    if idx >= len(times):
        raise ValueError(f"cue target {target!r}: marker has only {len(times)} entries")
    return float(times[idx])


def warp_points(cues: dict[str, float], targets: dict[str, float]) -> list[tuple[float, float]]:
    points = sorted((targets[name], cues[name]) for name in targets if name in cues)
    for (u0, r0), (u1, r1) in zip(points, points[1:], strict=False):
        if u1 <= u0 or r1 <= r0:
            raise ValueError("cues must happen in the same order as their target times")
    return points


def _record_time(points: list[tuple[float, float]], u: float) -> float:
    if not points:
        return u
    if u <= points[0][0]:
        return points[0][1] - (points[0][0] - u)
    for (u0, r0), (u1, r1) in zip(points, points[1:], strict=False):
        if u <= u1:
            return r0 + (u - u0) * (r1 - r0) / (u1 - u0)
    return points[-1][1] + (u - points[-1][0])


def retime(recording: Path, out: Path, points: list[tuple[float, float]], *, fps: float, frames: int,
           ffmpeg: str = "ffmpeg", erase: list[int] | None = None) -> Path:
    length = frames / fps
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(recording)], capture_output=True, text=True, check=True)
    duration = float(probe.stdout.strip())
    cuts = sorted({0.0, length, *(u for u, _ in points if 0.0 < u < length)})
    spans = [(ua, ub, _record_time(points, ua), _record_time(points, ub))
             for ua, ub in zip(cuts, cuts[1:], strict=False)]
    lead = max(0.0, -min(ra for _, _, ra, _ in spans))
    tail = max(0.0, max(rb for _, _, _, rb in spans) - duration)
    clean = ""
    if erase:
        x, y, w, h = erase
        x0, y0 = max(1, x - 3), max(1, y - 3)
        clean = f"delogo=x={x0}:y={y0}:w={x + w + 3 - x0}:h={y + h + 3 - y0},"
    spans = [(ua, ub, ra, rb, round(ub * fps) - round(ua * fps)) for ua, ub, ra, rb in spans]
    spans = [span for span in spans if span[4] > 0]
    parts = [f"[0:v]{clean}tpad=start_mode=clone:start_duration={lead:.4f}:stop_mode=clone:"
             f"stop_duration={tail + 1:.4f},split={len(spans)}" + "".join(f"[s{i}]" for i in range(len(spans)))]
    for i, (ua, ub, ra, rb, count) in enumerate(spans):
        factor = (ub - ua) / (rb - ra)
        end = max(rb, ra + 2.0 / fps)
        parts.append(f"[s{i}]trim=start={ra + lead:.4f}:end={end + lead:.4f},setpts=(PTS-STARTPTS)*{factor:.6f},"
                     f"fps={fps}:round=up,tpad=stop_mode=clone:stop={count},trim=end_frame={count},"
                     f"setpts=N/({fps}*TB)[p{i}]")
    parts.append("".join(f"[p{i}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=1:a=0,"
                 f"setpts=N/({fps}*TB),scale=trunc(iw/2)*2:trunc(ih/2)*2[v]")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(recording), "-filter_complex", ";".join(parts),
                    "-map", "[v]", "-frames:v", str(frames), "-r", f"{fps}", *OPAQUE, "-an", str(out)], check=True)
    return out


def hue_color(hue: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(hue / 360, 1.0, 1.0)
    return f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def beacon_frames(recording: Path, count: int, ffmpeg: str = "ffmpeg") -> tuple[list[int | None], list[int] | None]:
    import numpy as np
    from scipy import ndimage

    size = BEACON_SCAN
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(recording), "-vf", f"crop={size}:{size}:0:0",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, size, size, 3).astype(np.int16)
    a, b = BEACON_PATCH
    codes: list[int | None] = []
    box: list[int] | None = None
    for frame in frames:
        patch = frame[a:b, a:b].reshape(-1, 3)
        r, g, bl = patch.mean(axis=0)
        if max(r, g, bl) - min(r, g, bl) < 90 or (patch.max(axis=0) - patch.min(axis=0)).max() > 60:
            codes.append(None)
            continue
        hue = colorsys.rgb_to_hsv(r / 255, g / 255, bl / 255)[0] * 360
        code = min(range(len(BEACON_HUES)), key=lambda k: min(abs(hue - BEACON_HUES[k]),
                                                                360 - abs(hue - BEACON_HUES[k])))
        codes.append(code)
        if box is None:
            near = (np.abs(frame - np.array([r, g, bl])).max(axis=2) < 40)
            labels, _ = ndimage.label(near)
            ys, xs = np.nonzero(labels == labels[(a + b) // 2, (a + b) // 2])
            box = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
    found: list[int | None] = [None] * count
    nxt, previous = 0, None
    for index, code in enumerate(codes):
        if code is None or code == previous:
            previous = code if code is not None else previous
            continue
        previous = code
        skip = next((j for j in range(len(BEACON_HUES)) if (nxt + j) % len(BEACON_HUES) == code), None)
        if skip is None or nxt + skip >= count:
            continue
        found[nxt + skip] = index
        nxt += skip + 1
    return found, box


def synced_marks(order: list[str], wall: dict[str, float], frames: list[int | None], fps: float) -> dict[str, float]:
    seen = {name: frame / fps for name, frame in zip(order, frames, strict=True) if frame is not None}
    if not seen:
        return dict(wall)
    out = {}
    for name in order:
        if name in seen:
            out[name] = seen[name]
            continue
        anchor = min(seen, key=lambda other: abs(wall[other] - wall[name]))
        out[name] = seen[anchor] + wall[name] - wall[anchor]
    return out


def _fps(recording: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", str(recording)], capture_output=True, text=True, check=True).stdout.strip()
    num, _, den = out.partition("/")
    return float(num) / float(den or 1)


def _started_at(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("startedAt")
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def capture(media: DeviceTake, work: Path, ffmpeg: str = "ffmpeg") -> tuple[Path, dict[str, float], list[int] | None]:
    from eks_harness.cli.client import client_from_args
    from eks_harness.config import load as load_config
    from eks_harness.flows.runner import FlowRequest, load_module, prepare_app
    from eks_harness.paths import resolve_paths
    from eks_harness.plugins import find_tree

    flow_path = Path(media.flow).resolve()
    tree = Path(media.tree).resolve() if media.tree else find_tree(flow_path.parent)
    paths = resolve_paths()
    client = client_from_args(type("Args", (), {"url": None, "api_key": None})())
    prepared = prepare_app(FlowRequest(flow=flow_path, platform=media.platform, params=dict(media.params),
                                       cwd=tree, out=work, release=False),
                           client=client, paths=paths, config=load_config(paths))
    app = prepared.app
    module = load_module(flow_path, f"ehx_take_{flow_path.stem.replace('-', '_')}")
    if hasattr(module, "prepare"):
        module.prepare(app)
    beacons = media.beacons if media.beacons is not None else media.platform != "web"
    if beacons:
        app.call("annotate.beacon", None)
    wall: dict[str, float] = {}
    order: list[str] = []
    kwargs: dict[str, Any] = {"from_here": True, "hold": 0, "pace": media.pace or NO_PACING}
    kwargs.update({"phone": media.phone, "pointer": False} if media.platform == "web" else {"no_pointer": True})
    started = app.start_recording("device-take", **kwargs)
    t0 = _started_at(started) or time.time()

    def cue(name: str) -> None:
        if name in wall:
            raise ValueError(f"cue {name!r} was marked twice")
        wall[name] = time.time() - t0
        order.append(name)
        if beacons:
            app.call("annotate.beacon", hue_color(BEACON_HUES[(len(order) - 1) % len(BEACON_HUES)]))

    try:
        module.flow(app, cue)
    finally:
        if beacons:
            app.sleep(0.2)
        art = app.stop_recording("device-take", trim=False)
        if beacons:
            app.call("annotate.beacon", None)
    if art is None:
        raise RuntimeError("the take returned no recording")
    app.artifacts.append(art)
    app.collect(fetch=True, sheet=False)
    if not art.local or not Path(art.local).exists():
        raise RuntimeError(f"could not download the recording {art.id}")
    recording = Path(art.local)
    marks, box, frames = dict(wall), None, []
    if beacons and order:
        frames, box = beacon_frames(recording, len(order), ffmpeg)
        marks = synced_marks(order, wall, frames, _fps(recording))
        missing = [name for name, frame in zip(order, frames, strict=True) if frame is None]
        if missing:
            log.warning("device take: no sync beacon for cue(s) %s; their times come from the wall clock",
                        ", ".join(missing))
    (work / "cues.json").write_text(json.dumps({
        "cues": marks, "wall": wall, "beacon_frames": dict(zip(order, frames, strict=False)), "beacon_box": box,
        "artifact": art.id, "url": art.url}, indent=1))
    return recording, marks, box


class DeviceTakeRenderer(MediaRenderer):
    name: ClassVar[str] = "device-take"
    model: ClassVar[type[DeviceTake]] = DeviceTake
    version: ClassVar[str] = "2"
    opt_in: ClassVar[bool] = True

    def __init__(self, context: Any = None) -> None:
        self.context = context

    def cache_inputs(self, media: DeviceTake, ctx: RenderContext) -> list[Path]:  # type: ignore[override]
        return [Path(media.flow)]

    def cache_salt(self, media: DeviceTake, ctx: RenderContext) -> str:  # type: ignore[override]
        return str(time.time()) if media.refresh else ""

    def render(self, request: MediaRenderRequest, ctx: RenderContext) -> Path:
        media: DeviceTake = request.media  # type: ignore[assignment]
        recording, marks, box = capture(media, request.work_dir, ctx.options.ffmpeg_binary)
        speed = request.speed or 1.0
        targets = {name: (resolve_cue(t, request.markers) - request.project_start) * speed
                   for name, t in media.cues.items()}
        missing = sorted(set(targets) - set(marks))
        if missing:
            raise RuntimeError(f"the flow never called cue() for: {', '.join(missing)}")
        return retime(recording, request.output, warp_points(marks, targets), fps=request.fps,
                      frames=request.frame_count, ffmpeg=ctx.options.ffmpeg_binary, erase=box)
