from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import bad_request
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.store import access, deletion, layout
from eks_harness.store import artifacts as store_artifacts

router = APIRouter(tags=["review"])
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


def _video(ctx: AppContext, principal: Principal, artifact_id: str) -> tuple[Artifact, Path]:
    artifact = access.visible_artifact(ctx.db, principal, artifact_id.upper(), "editor")
    if artifact.kind != "video" and not artifact.filename.lower().endswith(VIDEO_SUFFIXES):
        raise bad_request(f"{artifact.id} is a {artifact.kind}, not a video.", error="not_video")
    path = layout.file_path(ctx.paths, artifact.rel_path)
    if not path.is_file():
        raise bad_request(f"The file of {artifact.id} is missing from the store.", error="file_missing")
    return artifact, path


def _out(ctx: AppContext, principal: Principal, artifact: Artifact) -> dict:
    out = store_artifacts.to_out(ctx.db.conn(), ctx.links, ctx.paths, artifact, user_id=principal.user_id)
    return out.model_dump(by_alias=True, mode="json")


def _store(ctx: AppContext, principal: Principal, video: Artifact, path: Path, *, kind: str, caption: str,
           tags: list[str], meta: dict) -> Artifact:
    return store_artifacts.ingest_file(
        ctx.db, ctx.config, project=video.project_id, session=video.session_slug, path_or_stream=path, kind=kind,
        caption=caption, tags=list(dict.fromkeys([*tags, *(video.tags or ())])), meta=meta, source="agent",
        user=principal, filename=path.name,
        events=ctx.events, move=True)


def _replace(ctx: AppContext, principal: Principal, video: Artifact, key: str, role: str) -> None:
    previous = (video.meta or {}).get(key)
    ids = previous if isinstance(previous, list) else [previous] if previous else []
    doomed = []
    for artifact_id in ids:
        try:
            old = access.visible_artifact(ctx.db, principal, str(artifact_id), "editor")
        except Exception:
            continue
        if (old.meta or {}).get("role") == role and (old.meta or {}).get("video") == video.id:
            doomed.append(old.id)
    if doomed:
        deletion.delete_artifacts(ctx.db, ctx.paths, doomed, events=ctx.events, actor=principal.username)


def _link(ctx: AppContext, principal: Principal, artifact: Artifact, meta: dict) -> Artifact:
    return store_artifacts.update(ctx.db, artifact, meta=meta, events=ctx.events, actor=principal.username)


def make_sheet(ctx: AppContext, principal: Principal, artifact_id: str, body: dict) -> dict:
    from eks_harness.review import make_sheets, parse_markers
    from eks_harness.review.markers import Marker

    video, path = _video(ctx, principal, artifact_id)
    try:
        markers = parse_markers(body.get("markers"))
        for item in body.get("at") or []:
            entry = item if isinstance(item, dict) else {"t": item}
            markers.frames.append(Marker(t=float(entry.get("t", entry.get("at", 0))), kind="frame",
                                         label=str(entry.get("label") or entry.get("text") or "")))
    except (TypeError, ValueError) as error:
        raise bad_request(f"markers: {error}", error="bad_markers") from error
    frames = body.get("frames")
    stem = Path(video.filename).stem
    work = Path(tempfile.mkdtemp(prefix="sheet-", dir=ctx.paths.tmp_dir))
    try:
        result = make_sheets(path, work, frames=int(frames) if frames is not None else None, markers=markers,
                             max_edge=int(body.get("maxEdge") or 2000), name=stem)
        if body.get("replace", True):
            _replace(ctx, principal, video, "sheets", "sheet")
        stored = []
        for sheet in result.sheets:
            meta = {"role": "sheet", "video": video.id, "part": sheet.part, "parts": sheet.parts,
                    "range": [round(sheet.start, 3), round(sheet.end, 3)], "tiles": len(sheet.tiles)}
            if (video.meta or {}).get("checks"):
                meta["checks"] = video.meta["checks"]
            caption = f"Contact sheet of {video.filename}" + (f" ({sheet.part}/{sheet.parts})" if sheet.parts > 1 else "")
            artifact = _store(ctx, principal, video, sheet.path, kind="screenshot", caption=caption, tags=["sheet"],
                              meta=meta)
            stored.append({**_out(ctx, principal, artifact), "part": sheet.part, "parts": sheet.parts,
                           "range": meta["range"], "tiles": [t.as_dict() for t in sheet.tiles]})
        updated = _link(ctx, principal, video, {"sheets": [s["id"] for s in stored]})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"video": _out(ctx, principal, updated), "info": result.video.as_dict(), "sheets": stored,
            "markers": markers.as_dict()}


def run_check(ctx: AppContext, principal: Principal, artifact_id: str, body: dict) -> dict:
    from eks_harness.review import ExpectationError, parse_expectations, run_checks

    video, path = _video(ctx, principal, artifact_id)
    raw = body.get("expectations")
    if raw is None:
        raise bad_request("Send expectations: a list of checks or {checks: [...]}.", error="bad_expectations")
    tree = Path(body["tree"]).expanduser() if body.get("tree") else None
    try:
        expectations = parse_expectations(raw)
        from eks_harness.review.checks import load_plugin_checks

        load_plugin_checks(ctx.plugins, tree)
        report = run_checks(path, expectations, plugins=False)
    except ExpectationError as error:
        raise bad_request(str(error), error="bad_expectations") from error
    data = report.as_dict()
    data["video"]["path"] = video.filename
    text = report.text().replace(str(path.name), video.filename, 1)
    stem = Path(video.filename).stem
    work = Path(tempfile.mkdtemp(prefix="checks-", dir=ctx.paths.tmp_dir))
    try:
        file = work / f"{stem}-checks.json"
        file.write_text(json.dumps({**data, "text": text, "videoArtifact": video.id}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        if body.get("replace", True):
            _replace(ctx, principal, video, "checks", "checks")
        counts = report.counts()
        caption = (f"Checks of {video.filename}: {'PASS' if report.ok else 'FAIL'} "
                   + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
        stored = _store(ctx, principal, video, file, kind="log", caption=caption,
                        tags=["checks", "pass" if report.ok else "fail"],
                        meta={"role": "checks", "video": video.id, "ok": report.ok, "counts": counts,
                              "sheets": (video.meta or {}).get("sheets") or []})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    updated = _link(ctx, principal, video, {"checks": stored.id, "checksOk": report.ok})
    for sheet_id in (video.meta or {}).get("sheets") or []:
        try:
            sheet = access.visible_artifact(ctx.db, principal, str(sheet_id), "editor")
        except Exception:
            continue
        _link(ctx, principal, sheet, {"checks": stored.id})
    return {"ok": report.ok, "report": data, "text": text, "artifact": _out(ctx, principal, stored),
            "video": _out(ctx, principal, updated)}


@router.post("/api/artifacts/{artifact_id}/sheet")
async def artifact_sheet(artifact_id: str, request: Request, body: dict = Body(default_factory=dict),
                         principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return await run_in_threadpool(make_sheet, get_ctx(request), principal, artifact_id, body)


@router.post("/api/artifacts/{artifact_id}/checks")
async def artifact_checks(artifact_id: str, request: Request, body: dict = Body(...),
                          principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return await run_in_threadpool(run_check, get_ctx(request), principal, artifact_id, body)


@router.get("/api/review/checks")
def check_kinds(_: Principal = Depends(current_principal)) -> dict:
    from eks_harness.review import kinds

    return {"items": kinds()}
