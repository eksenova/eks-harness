"""Run Blender headless for a ``BlenderScene`` request.

Finds the Blender executable and hands the frames to :mod:`eks_harness.video.farm`:
local workers (``workers``, ``worker_env``) or, when a farm is configured,
local plus SSH workers that pull frames one at a time. With the frame cache
on (the default) frames are kept per segment with a scene fingerprint each,
so a frame whose scene did not change is not rendered again; the PNG
sequence is then encoded to ProRes.
"""

from __future__ import annotations

import functools
import glob
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video import farm
from eks_harness.video.render.composite import ALPHA_ENCODE
from eks_harness.video.render.subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from eks_harness.video.plugins.base import MediaRenderRequest
    from eks_harness.video.render.context import RenderContext

_LOG = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
BOOTSTRAP = HERE / "bootstrap.py"
RUNTIME_DIR = Path(__file__).resolve().parents[5] / "blender"
RUNTIME = RUNTIME_DIR / "__init__.py"
FINGERPRINT = HERE / "frame_fingerprint.py"
OPAQUE_ENCODE = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
INSTALL_HINT = (
    "BlenderScene needs Blender 4.2 or newer. Install it from https://www.blender.org/download/ and put "
    "`blender` on PATH, set EKS_HARNESS_BLENDER=/path/to/blender, or pass BlenderScene(blender=...)."
)
_SEARCH = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "~/Applications/Blender.app/Contents/MacOS/Blender",
    "~/opt/blender-*/blender",
    "/opt/blender*/blender",
    "C:/Program Files/Blender Foundation/Blender */blender.exe",
]


def runtime_files() -> list[Path]:
    """The Blender-side files; changing them invalidates cached renders."""

    return [BOOTSTRAP, RUNTIME, FINGERPRINT]


def find_blender(explicit: str | Path | None = None) -> str:
    for candidate in (explicit, os.environ.get("EKS_HARNESS_BLENDER")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
            raise RuntimeError(f"Blender executable {candidate!s} does not exist. {INSTALL_HINT}")
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    for pattern in _SEARCH:
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    raise RuntimeError(INSTALL_HINT)


@functools.lru_cache(maxsize=8)
def blender_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not run {binary} --version: {exc}. {INSTALL_HINT}") from exc
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[0] if lines else "unknown"


class BlenderCapability:
    """Node capability probe: the Blender executable and version this machine can render with."""

    name = "blender"

    def probe(self) -> dict[str, str] | None:
        try:
            binary = find_blender(None)
            return {"path": binary, "version": blender_version(binary)}
        except RuntimeError:
            return None


def _absolute(path: str | Path | None, base: Path) -> str | None:
    if path is None:
        return None
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def root_of(media, ctx: RenderContext) -> Path:
    return Path(media.root).expanduser().resolve() if media.root else Path(ctx.workspace).resolve()


def worker_count(media_workers: int | None, frames: int) -> int:
    wanted = media_workers or int(os.environ.get("EKS_HARNESS_BLENDER_WORKERS", "1") or 1)
    return max(1, min(wanted, frames))


def frames_dir_for(request: MediaRenderRequest, ctx: RenderContext) -> Path:
    """Where the PNG frames live: per segment and output format when the frame cache is on, so edits that leave a
    frame's scene unchanged reuse its image; otherwise private to this render."""

    media = request.media
    if not media.frame_cache:
        return request.work_dir / "frames"
    width, height = media.resolution or request.resolution
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.segment_id)
    fps = f"{request.fps:g}".replace(".", "_")
    return Path(ctx.cache_dir) / "media" / "blender" / "frames" / f"{safe}-{width}x{height}-{fps}fps"


def build_config(request: MediaRenderRequest, base: Path, frames_dir: Path) -> dict:
    media = request.media
    width, height = media.resolution or request.resolution
    first = media.frame_start + round(request.source_start * request.fps)
    return {
        "fps": request.fps,
        "width": int(width),
        "height": int(height),
        "frame_zero": media.frame_start,
        "first_frame": first,
        "frame_count": request.frame_count,
        "source_start": request.source_start,
        "source_end": request.source_end,
        "project_start": request.project_start,
        "speed": request.speed,
        "params": media.params,
        "markers": request.markers,
        "transparent": media.transparent,
        "engine": media.engine,
        "samples": media.samples,
        "device": media.device,
        "scene": media.scene,
        "camera": media.camera,
        "script": _absolute(media.script, base),
        "frames_dir": str(frames_dir),
        "frame_cache": media.frame_cache,
        "segment_id": request.segment_id,
        "workspace": str(base),
        "runtime_dir": str(RUNTIME_DIR),
        "inputs": list(media.events),
    }


def emit_pass(request: MediaRenderRequest, ctx: RenderContext, binary: str, config: dict) -> Path:
    events_file = request.output.with_suffix(".events.json")
    raw = request.work_dir / f"{request.segment_id.replace('/', '_')}-emitted.json"
    blend = _absolute(request.media.blend, root_of(request.media, ctx))
    argv = [binary, "--factory-startup", "-b", *([blend] if blend else []), "--python-exit-code", "1",
            "--python", str(BOOTSTRAP), "--", json.dumps({**config, "emit_only": True, "events_out": str(raw)})]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not raw.exists():
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-30:])
        raise RuntimeError(f"Blender emit pass for {request.segment_id} failed ({result.returncode}):\n{tail}")
    emitted = json.loads(raw.read_text(encoding="utf-8")).get("emitted") or []
    events_file.write_text(json.dumps({"segment": request.segment_id, "fps": request.fps,
                                       "sourceStart": request.source_start,
                                       "emitted": [{**e, "segment": request.segment_id} for e in emitted]},
                                      indent=2), encoding="utf-8")
    raw.unlink(missing_ok=True)
    return events_file


class FrameLedger:
    """``fingerprints.json`` next to the frames: scene frame -> [platform tag, fingerprint]."""

    def __init__(self, frames_dir: Path, frames: list[int], enabled: bool) -> None:
        self.path = frames_dir / "fingerprints.json"
        try:
            known = {int(k): v for k, v in json.loads(self.path.read_text()).items()}
        except (FileNotFoundError, ValueError):
            known = {}
        self.known = known
        self.reusable = {f: known[f] for f in frames if enabled and isinstance(known.get(f), list)
                         and (frames_dir / frame_name(f)).exists()}
        self.rendered = 0
        self.skipped = 0

    def platform_of(self, frame: int) -> str | None:
        entry = self.reusable.get(frame)
        return entry[0] if entry else None

    def record(self, line: str, tag: str) -> None:
        kind, frame, fp, line_tag = [*line.split(), "-", "-", "-"][:4]
        if kind == "EHX_SKIP":
            self.skipped += 1
        else:
            self.rendered += 1
        if fp != "-":
            self.known[int(frame)] = [line_tag if line_tag != "-" else tag, fp]

    def save(self) -> None:
        self.path.write_text(json.dumps({str(k): v for k, v in sorted(self.known.items())}))


def nested_media(name: str, spec, request: MediaRenderRequest, ctx: RenderContext) -> Path:
    from eks_harness.video.render.materialize import render_nested

    path = render_nested(spec, request, ctx, name).resolve()
    sidecar = Path(f"{path}.framemd5")
    if path.suffix.lower() in {".mov", ".mp4", ".m4v", ".webm", ".mkv"} and not sidecar.exists():
        run_ffmpeg(["-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", str(sidecar)],
                   binary=ctx.options.ffmpeg_binary)
    return path


def frame_name(frame: int) -> str:
    return f"f_{frame:06d}.png"


def _farm_for(media, base: Path, frames: int) -> farm.FarmConfig:
    if media.farm:
        return farm.load(base, local_workers=worker_count(media.workers, frames), local_slots=media.worker_env or None)
    envs = media.worker_env or [{}]
    count = worker_count(media.workers, frames)
    return farm.FarmConfig(hosts=[farm.Host(name="local", slots=[farm.Slot(env=dict(envs[i % len(envs)]))
                                                                 for i in range(count)])])


def render(request: MediaRenderRequest, ctx: RenderContext) -> Path:
    media = request.media
    binary = find_blender(media.blender)
    base = root_of(media, ctx)
    frames_dir = frames_dir_for(request, ctx)
    frames_dir.mkdir(parents=True, exist_ok=True)
    config = build_config(request, base, frames_dir)
    config["media"] = {name: str(nested_media(name, spec, request, ctx)) for name, spec in media.media.items()}
    first = config["first_frame"]
    frames = list(range(first, first + request.frame_count))
    cache_on = media.frame_cache and os.environ.get("EKS_HARNESS_BLENDER_FRAME_CACHE", "1") != "0"
    ledger = FrameLedger(frames_dir, frames, cache_on)
    if media.frame_cache:
        config["fingerprints"] = {str(f): v for f, v in ledger.reusable.items()}
    todo = frames if media.frame_cache else [f for f in frames if not (frames_dir / frame_name(f)).exists()]

    blend = _absolute(media.blend, base)
    outside = [Path(p) for p in (blend, config["script"], *(_absolute(i, base) for i in media.inputs))
               if p and base not in Path(p).resolve().parents]
    for path in config["media"].values():
        outside += [Path(path)] + ([Path(path + ".framemd5")] if Path(path + ".framemd5").exists() else [])

    def command(worker: farm.WorkerContext) -> list[str]:
        payload = json.dumps(worker.remap(config))
        return [worker.tool("blender", binary), "--factory-startup", "-b", *([worker.path(blend)] if blend else []),
                "--python-exit-code", "1", "--python", worker.path(BOOTSTRAP), "--", payload, "-"]

    def progress(done: int, total: int) -> None:
        if done == total or done % max(1, total // 10) == 0:
            _LOG.info("blender %s: %d/%d frames", request.segment_id, done, total)

    if todo:
        job = farm.FarmJob(name=f"blender:{request.segment_id}", items=todo, command=command,
                           out_dir=frames_dir, sync=[base, HERE, RUNTIME_DIR, *outside], exclude=media.sync_exclude,
                           out_pattern="f_*.png", affinity=ledger.platform_of, on_line=ledger.record,
                           on_progress=progress, requires=["blender"])
        try:
            for stat in farm.run(job, _farm_for(media, base, len(todo))):
                rate = f"{stat.seconds_per_item:.1f}s/frame" if stat.seconds_per_item else "-"
                _LOG.info("blender %s: %-14s %4d frames %s %s", request.segment_id, stat.name, stat.done, rate,
                          stat.device)
        finally:
            ledger.save()
        if ledger.skipped:
            _LOG.info("blender %s: %d unchanged frames reused", request.segment_id, ledger.skipped)

    missing = [f for f in frames if not (frames_dir / frame_name(f)).exists()]
    if missing:
        raise RuntimeError(f"Blender produced no image for {len(missing)} of {len(frames)} frames "
                           f"(first missing: scene frame {missing[0]})")

    out_w, out_h = request.resolution
    scale = [] if (config["width"], config["height"]) == (out_w, out_h) else \
        ["-vf", f"scale={out_w}:{out_h}:flags=lanczos"]
    tmp = request.output.with_suffix(".part.mov")
    run_ffmpeg(["-y", "-v", "error", "-framerate", f"{request.fps}", "-start_number", str(first),
                "-i", str(frames_dir / "f_%06d.png"), "-frames:v", str(len(frames)), *scale,
                *(ALPHA_ENCODE if media.transparent else OPAQUE_ENCODE), "-an", str(tmp)],
               binary=ctx.options.ffmpeg_binary)
    tmp.replace(request.output)
    if media.emits:
        emit_pass(request, ctx, binary, config)
    if not media.frame_cache and not os.environ.get("EKS_HARNESS_BLENDER_KEEP_FRAMES"):
        shutil.rmtree(frames_dir, ignore_errors=True)
    return request.output


__all__ = ["INSTALL_HINT", "BlenderCapability", "FrameLedger", "blender_version", "build_config", "find_blender", "frame_name",
           "frames_dir_for", "render", "runtime_files", "worker_count"]
