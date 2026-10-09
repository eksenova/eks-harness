from __future__ import annotations

import functools
import json
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from eks_harness.video.render.composite import ALPHA_ENCODE
from eks_harness.video.render.subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from eks_harness.video.plugins.base import MediaRenderRequest
    from eks_harness.video.render.context import RenderContext

OPAQUE_ENCODE = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
INSTALL_HINT = "WebScene needs Playwright: install eks-harness[html], then run `playwright install chromium`."
PACKAGE_RUNTIME = Path(__file__).resolve().parents[5] / "assets" / "scene-runtime.js"
SOURCE_RUNTIME = Path(__file__).resolve().parents[7] / "sdk" / "scene" / "dist" / "scene-runtime.js"
PRIVATE = "/__ehx/"


class WebSceneError(RuntimeError):
    pass


def require_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise WebSceneError(INSTALL_HINT) from error
    return sync_playwright


def runtime_script() -> Path:
    for candidate in (PACKAGE_RUNTIME, SOURCE_RUNTIME):
        if candidate.is_file():
            return candidate
    raise WebSceneError("the scene runtime (scene-runtime.js) is missing; build sdk/scene (pnpm --filter "
                        "@eks-harness/scene build) or reinstall eks-harness with its web parts")


@functools.cache
def browser_version() -> str:
    try:
        sync_playwright = require_playwright()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            version = browser.version
            browser.close()
            return version
    except Exception:
        return "unknown"


def root_of(media: Any, ctx: RenderContext) -> Path:
    if media.root:
        return Path(media.root).expanduser().resolve()
    entry = Path(media.entry)
    workspace = Path(ctx.workspace).resolve()
    return (entry if entry.is_absolute() else workspace / entry).parent.resolve()


def _entry_path(media: Any, ctx: RenderContext, base: Path) -> str:
    entry = Path(media.entry)
    if not entry.is_absolute():
        entry = (Path(media.root).expanduser() / entry) if media.root else Path(ctx.workspace) / entry
    entry = entry.resolve()
    if not entry.is_file():
        raise WebSceneError(f"WebScene entry {entry} does not exist")
    if not entry.is_relative_to(base):
        raise WebSceneError(f"WebScene entry {entry} is outside its root {base}")
    return entry.relative_to(base).as_posix()


class _Handler(SimpleHTTPRequestHandler):
    private_root: Path = Path(".")

    def translate_path(self, path: str) -> str:
        parsed = unquote(urlparse(path).path)
        if parsed.startswith(PRIVATE):
            target = (self.private_root / parsed[len(PRIVATE):]).resolve()
            if not target.is_relative_to(self.private_root.resolve()):
                return str(self.private_root / "__denied__")
            return str(target)
        return super().translate_path(path)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


@contextmanager
def serve(root: Path, private: Path) -> Iterator[str]:
    handler = type("SceneHandler", (_Handler,), {"private_root": private})
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def source_time(project_time: float, request: MediaRenderRequest) -> float:
    speed = request.speed or 1.0
    return request.source_start + (project_time - request.project_start) * speed


def scene_inputs(request: MediaRenderRequest) -> list[dict[str, Any]]:
    media = request.media
    inputs: list[dict[str, Any]] = [event.model_dump(exclude_none=True) for event in media.events]
    streams = (request.markers or {}).get("streams") or {}
    named = (request.markers or {}).get("named") or {}
    for stream, event_name in media.marker_events.items():
        times = streams.get(stream) or named.get(stream) or []
        for index, t in enumerate(sorted(float(x) for x in times)):
            local = source_time(t, request)
            if request.source_start - 1e-9 <= local <= request.source_end + 1e-9:
                inputs.append({"time": local, "verb": "emit", "name": event_name,
                               "data": {"index": index, "stream": stream}, "source": "markers"})
    return sorted(inputs, key=lambda item: float(item["time"]))


def textures(request: MediaRenderRequest, ctx: RenderContext, private: Path) -> dict[str, dict[str, Any]]:
    from eks_harness.video.render.materialize import render_nested

    out: dict[str, dict[str, Any]] = {}
    for name, spec in request.media.media.items():
        path = render_nested(spec, request, ctx, name).resolve()
        folder = private / "textures" / name
        folder.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            shutil.copyfile(path, folder / f"000000{path.suffix.lower()}")
            out[name] = {"url": f"{PRIVATE}textures/{name}/000000{path.suffix.lower()}", "frames": 1}
            continue
        run_ffmpeg(["-y", "-v", "error", "-i", str(path), "-vsync", "0", "-start_number", "0",
                    str(folder / "%06d.png")], binary=ctx.options.ffmpeg_binary)
        count = len(list(folder.glob("*.png")))
        out[name] = {"url": f"{PRIVATE}textures/{name}/{{frame}}.png", "frames": count, "fps": request.fps,
                     "offset": request.source_start}
    return out


def beat_grid(request: MediaRenderRequest) -> tuple[list[float], list[float]]:
    streams = (request.markers or {}).get("streams") or {}
    beats = sorted(source_time(float(t), request) for t in streams.get("beat") or [])
    downbeats = sorted(source_time(float(t), request) for t in streams.get("downbeat") or [])
    return beats, downbeats


def render(request: MediaRenderRequest, ctx: RenderContext) -> Path:
    sync_playwright = require_playwright()
    media = request.media
    base = root_of(media, ctx)
    entry = _entry_path(media, ctx, base)
    width, height = media.resolution or request.resolution
    first = round(request.source_start * request.fps)
    frames = range(first, first + request.frame_count)
    work = Path(tempfile.mkdtemp(prefix="web-scene-", dir=request.work_dir))
    private = work / "private"
    shots = work / "frames"
    private.mkdir()
    shots.mkdir()
    beats, downbeats = beat_grid(request)
    config = {"mode": "render", "fps": request.fps, "track": request.segment_id, "start": request.source_start,
              "duration": request.source_end - request.source_start, "props": media.props,
              "inputs": scene_inputs(request), "textures": textures(request, ctx, private), "beats": beats,
              "downbeats": downbeats}
    emitted: list[dict[str, Any]] = []
    try:
        with serve(base, private) as origin, sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--disable-gpu-vsync", "--force-color-profile=srgb",
                                                       *media.browser_args])
            page = browser.new_page(viewport={"width": int(width), "height": int(height)},
                                    device_scale_factor=media.device_scale_factor)
            page.add_init_script(script=f"window.__EHX_SCENE_CONFIG__ = {json.dumps(config)};")
            page.add_init_script(path=str(runtime_script()))
            page.set_default_timeout(media.timeout_ms)
            page.goto(f"{origin}/{entry}", wait_until="load")
            page.wait_for_function("() => !!window.__ehx")
            if media.ready:
                page.wait_for_selector(media.ready)
            for index, frame in enumerate(frames):
                result = page.evaluate("(f) => window.__ehx.renderFrame(f)", frame)
                for item in result.get("emitted") or []:
                    emitted.append({**item, "segment": request.segment_id})
                page.screenshot(path=str(shots / f"f_{index:06d}.png"), omit_background=media.transparent,
                                type="png")
            browser.close()
        out_w, out_h = request.resolution
        scale = [] if (int(width), int(height)) == (out_w, out_h) else ["-vf", f"scale={out_w}:{out_h}:flags=lanczos"]
        tmp = request.output.with_suffix(".part.mov")
        run_ffmpeg(["-y", "-v", "error", "-framerate", f"{request.fps}", "-start_number", "0",
                    "-i", str(shots / "f_%06d.png"), "-frames:v", str(len(frames)), *scale,
                    *(ALPHA_ENCODE if media.transparent else OPAQUE_ENCODE), "-an", str(tmp)],
                   binary=ctx.options.ffmpeg_binary)
        tmp.replace(request.output)
        events_file = request.output.with_suffix(".events.json")
        events_file.write_text(json.dumps({"segment": request.segment_id, "fps": request.fps,
                                           "sourceStart": request.source_start, "emitted": emitted},
                                          indent=2), encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return request.output


def emitted_events(output: Path) -> list[dict[str, Any]]:
    file = output.with_suffix(".events.json")
    if not file.is_file():
        return []
    return list(json.loads(file.read_text(encoding="utf-8")).get("emitted") or [])
