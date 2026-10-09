from __future__ import annotations

import json
import mimetypes
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import get_ctx, require_admin
from eks_harness.api.errors import bad_request, not_found
from eks_harness.auth import middleware as auth_middleware
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.plugins import find_tree
from eks_harness.score.live import LiveRegistry, pump_websocket

router = APIRouter(tags=["scores"])
SERVICE = "score-live"
SCORE_SUFFIXES = (".py", ".json")


def live(ctx: AppContext) -> LiveRegistry:
    if not ctx.has_service(SERVICE):
        ctx.register_service(SERVICE, LiveRegistry())
    return ctx.service(SERVICE)


def _file(path: str) -> Path:
    file = Path(path).expanduser()
    if not file.is_file() or file.suffix not in SCORE_SUFFIXES:
        raise bad_request(f"No score file {path} (score.py or score.json).", error="no_score")
    return file.resolve()


def _load(path: str):
    from eks_harness.score.loader import load_any

    file = _file(path)
    try:
        return load_any(file), file.parent
    except Exception as error:
        raise bad_request(f"{file.name}: {error}", error="bad_score") from error


def score_files(tree: Path) -> list[dict[str, Any]]:
    from eks_harness.plugins import load_project_config

    project = load_project_config(tree)
    patterns = list((project.section("scores").get("paths") or [])) or [".harness/scores/*"]
    found: dict[str, dict[str, Any]] = {}
    for pattern in patterns:
        for path in sorted(tree.glob(pattern)):
            if path.is_dir():
                candidates = [p for p in path.iterdir() if p.suffix in SCORE_SUFFIXES and p.stem.startswith("score")]
            else:
                candidates = [path] if path.suffix in SCORE_SUFFIXES else []
            for item in candidates:
                found[str(item)] = {"path": str(item), "name": item.parent.name if item.stem == "score" else item.stem,
                                    "relative": str(item.relative_to(tree)), "modified": item.stat().st_mtime}
    return sorted(found.values(), key=lambda i: -i["modified"])


@router.get("/api/scores")
def list_scores(request: Request, tree: str = Query(...), _: Principal = Depends(require_admin)) -> dict:
    root = find_tree(Path(tree).expanduser()) or Path(tree).expanduser()
    if not root.is_dir():
        raise bad_request(f"No directory {tree}.", error="no_tree")
    return {"tree": str(root), "items": score_files(root)}


@router.get("/api/scores/plan")
async def score_plan(request: Request, path: str = Query(...), analyze: bool = Query(True),
                     _: Principal = Depends(require_admin)) -> dict:
    from eks_harness.score import plan
    from eks_harness.score.analysis import AnalysisError, beat_analyzer

    score, base = _load(path)
    try:
        result = await run_in_threadpool(plan, score, base=base, analyzer=beat_analyzer if analyze else None)
    except AnalysisError as error:
        raise bad_request(str(error), error="analysis_failed") from error
    return {"score": json.loads(score.dumps()), "plan": result.as_dict(), "base": str(base)}


@router.post("/api/scores/render")
def render(request: Request, body: dict = Body(...), principal: Principal = Depends(require_admin)) -> dict:
    from eks_harness.score.analysis import beat_analyzer
    from eks_harness.score.render import render_score
    from eks_harness.score.takes import device_take_runner

    ctx = get_ctx(request)
    score, base = _load(str(body.get("path") or ""))
    output = Path(body["out"]).expanduser() if body.get("out") else base / "renders" / f"{score.name}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    resource = f"score:{score.name}"

    def progress(stage: str, detail: dict) -> None:
        ctx.events.publish("score.render.progress", resource=resource, persist=False,
                           detail={"stage": stage, **detail})

    def work() -> None:
        try:
            result = render_score(score, base, output, analyzer=beat_analyzer, take_runner=device_take_runner,
                                  progress=progress, mode=str(body.get("mode") or "final"))
            ctx.events.publish("score.render.done", resource=resource, actor=principal.username,
                               detail={"output": str(result.output), "iterations": result.iterations,
                                       "warnings": result.warnings[:20]})
        except Exception as error:
            ctx.events.publish("score.render.failed", resource=resource, actor=principal.username,
                               detail={"error": str(error)[:2000]})

    threading.Thread(target=work, name=f"score-render-{score.name}", daemon=True).start()
    return {"started": True, "resource": resource, "output": str(output)}


def _device_sessions(ctx: AppContext, devices: dict[str, str], score: Any) -> dict[str, Any]:
    from eks_harness.drivers.sessions import WorkerSession
    from eks_harness.drivers.workers import WorkerManager

    manager = WorkerManager(ctx.paths)
    sessions: dict[str, Any] = {}
    for track_id, sid in devices.items():
        track = next((t for t in score.tracks if t.id == track_id), None)
        if track is None or track.kind != "device":
            raise bad_request(f"{track_id} is not a device track.", error="bad_device")
        kind = "web" if track.platform == "web" else "mobile"
        handle = manager.get(kind, sid) or (manager.get("mobile", sid) if kind == "web" else None)
        if handle is None:
            raise bad_request(f"No driver worker runs for lease {sid}; start one with a flow or "
                              f"'eks-harness driver' first.", error="no_worker")
        sessions[track_id] = WorkerSession(handle.client(), platform=track.platform, sid=sid)
    return sessions


@router.post("/api/scores/live")
async def open_live(request: Request, body: dict = Body(...), _: Principal = Depends(require_admin)) -> dict:
    from eks_harness.score.analysis import beat_analyzer

    ctx = get_ctx(request)
    score, base = _load(str(body.get("path") or ""))
    devices = _device_sessions(ctx, dict(body.get("devices") or {}), score)
    previews = (ctx.paths.cache_dir / "score-previews") if body.get("previews", True) else None
    session = await run_in_threadpool(live(ctx).open, score, base,
                                      analyzer=beat_analyzer if body.get("analyze", True) else None,
                                      devices=devices, loop=bool(body.get("loop")), previews=previews)
    return session.snapshot()


@router.get("/api/scores/live")
def list_live(request: Request, _: Principal = Depends(require_admin)) -> dict:
    return {"items": live(get_ctx(request)).list()}


@router.get("/api/scores/live/{session_id}")
def get_live(session_id: str, request: Request, _: Principal = Depends(require_admin)) -> dict:
    session = live(get_ctx(request)).get(session_id)
    if session is None:
        raise not_found(f"No live session {session_id}.", error="live_not_found")
    return session.snapshot()


@router.post("/api/scores/live/{session_id}/{verb}")
def control_live(session_id: str, verb: str, request: Request, body: dict = Body(default_factory=dict),
                 _: Principal = Depends(require_admin)) -> dict:
    session = live(get_ctx(request)).get(session_id)
    if session is None:
        raise not_found(f"No live session {session_id}.", error="live_not_found")
    if verb == "play":
        session.play(body.get("position"))
    elif verb == "pause":
        session.pause()
    elif verb == "seek":
        session.seek(float(body.get("position") or 0))
    elif verb == "stop":
        session.stop()
    elif verb == "emit":
        session.observe(str(body.get("source") or "ui"), str(body.get("name") or ""), dict(body.get("data") or {}))
    else:
        raise bad_request(f"Unknown live verb {verb}.", error="bad_verb")
    return {k: v for k, v in session.snapshot().items() if k != "plan"}


@router.get("/api/scores/live/{session_id}/audio")
def live_audio(session_id: str, request: Request, _: Principal = Depends(require_admin)) -> FileResponse:
    session = live(get_ctx(request)).get(session_id)
    path = session.audio_file() if session else None
    if path is None:
        raise not_found("This score has no audio track.", error="no_audio")
    media = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})


@router.get("/api/scores/live/{session_id}/preview/{track_id}")
def live_preview(session_id: str, track_id: str, request: Request,
                 _: Principal = Depends(require_admin)) -> FileResponse:
    session = live(get_ctx(request)).get(session_id)
    preview = (session.previews.get(track_id) if session else None) or {}
    if preview.get("state") != "ready":
        raise not_found(f"No preview for {track_id} yet.", error="preview_not_ready")
    return FileResponse(preview["path"], media_type="video/webm", headers={"Cache-Control": "no-cache"})


@router.delete("/api/scores/live/{session_id}")
def close_live(session_id: str, request: Request, _: Principal = Depends(require_admin)) -> dict:
    if not live(get_ctx(request)).close(session_id):
        raise not_found(f"No live session {session_id}.", error="live_not_found")
    return {"closed": session_id}


@router.websocket("/api/scores/live/{session_id}/ws")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
    ctx: AppContext = websocket.app.state.ctx
    try:
        principal = auth_middleware.authenticate(ctx.db, ctx.config, websocket)
    except Exception:
        principal = None
    if principal is None or not principal.is_admin:
        await websocket.close(code=4401, reason="admin login required")
        return
    session = live(ctx).get(session_id)
    if session is None:
        await websocket.close(code=4404, reason="no such live session")
        return
    await websocket.accept()
    role = websocket.query_params.get("role") or "ui"
    await pump_websocket(websocket, session, role=role, track=websocket.query_params.get("track"))


def _runtime_asset() -> Path:
    from eks_harness.video.plugins.builtin.media.web_scene.runner import runtime_script

    return runtime_script()


@router.get("/api/scores/scene-runtime.js")
def scene_runtime(_: Principal = Depends(require_admin)) -> FileResponse:
    return FileResponse(_runtime_asset(), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/api/scores/live/{session_id}/scene/{track_id}/{path:path}")
def live_scene_file(session_id: str, track_id: str, path: str, request: Request,
                    _: Principal = Depends(require_admin)) -> Response:
    session = live(get_ctx(request)).get(session_id)
    if session is None:
        raise not_found(f"No live session {session_id}.", error="live_not_found")
    track = next((t for t in session.score.tracks if t.id == track_id and t.kind == "web"), None)
    if track is None:
        raise not_found(f"No web track {track_id}.", error="track_not_found")
    entry = (session.base / track.entry).resolve()
    root = entry.parent
    target = (root / (path or entry.name)).resolve()
    if not target.is_file() or not target.is_relative_to(root):
        raise not_found(f"No file {path}.", error="not_found")
    if target == entry:
        context = session.plan.context
        config = {"mode": "live", "fps": context.fps, "track": track_id, "props": track.props,
                  "beats": list(context.beats), "downbeats": list(context.downbeats),
                  "socket": f"/api/scores/live/{session_id}/ws?role=scene&track={track_id}"}
        injection = (f"<script>window.__EHX_SCENE_CONFIG__ = {json.dumps(config)};"
                     f"window.__EHX_SCENE_CONFIG__.socket = (location.protocol === 'https:' ? 'wss://' : 'ws://')"
                     f" + location.host + window.__EHX_SCENE_CONFIG__.socket;</script>"
                     f"<script src=\"/api/scores/scene-runtime.js\"></script>")
        html = target.read_text(encoding="utf-8")
        lower = html.lower()
        index = lower.find("<head>")
        html = html[: index + 6] + injection + html[index + 6:] if index >= 0 else injection + html
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-store"})
