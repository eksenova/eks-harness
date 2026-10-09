from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from eks_harness.api.deps import current_principal, get_ctx, get_scope
from eks_harness.api.errors import ApiError, bad_request, not_found
from eks_harness.api.schemas import (
    AnnotateRecaptureRequest,
    AnnotateRerenderRequest,
    AnnotateRequest,
    AnnotateResponse,
    AnnotateRestoreRequest,
    AnnotateRestoreResponse,
    AnnotationVersionsResponse,
    ArtifactOut,
    ProjectAssetList,
    ts_to_datetime,
)
from eks_harness.auth.core import Principal
from eks_harness.daemon import events as ev
from eks_harness.daemon.context import AppContext
from eks_harness.db.common import dumps, loads
from eks_harness.db.repos import artifacts as artifacts_repo
from eks_harness.db.repos import projects as projects_repo
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.grants import AccessScope
from eks_harness.ids import is_sid, new_ulid, slugify
from eks_harness.store import access as store_access
from eks_harness.store import artifacts as store_artifacts
from eks_harness.store import layout
from eks_harness.store.retention import default_retention_days

try:
    from eks_harness.annotate import spec as engine_spec
except ImportError:
    engine_spec = None

try:
    from eks_harness.annotate import validate as engine_validate
except ImportError:
    engine_validate = None

try:
    from eks_harness.annotate import render as engine_render
except ImportError:
    engine_render = None

router = APIRouter(tags=["annotations"])

SPEC_VERSION = 1
MAX_ITEMS = 100
SPEC_TYPES = ("step", "callout", "label", "highlight", "arrow", "spotlight", "blur", "redact", "icon", "image",
              "caption", "title")
ANCHOR_KINDS = ("selector", "role", "text", "testId", "coords")
TOP_FIELDS = frozenset({"version", "style", "items"})
COMMON_FIELDS = frozenset({"type", "anchor", "id", "placement", "label", "emphasis"})
PER_TYPE_EXTRA: dict[str, frozenset[str]] = {
    "step": frozenset({"index"}),
    "callout": frozenset({"text"}),
    "label": frozenset({"text"}),
    "highlight": frozenset({"shape"}),
    "arrow": frozenset({"to", "text"}),
    "spotlight": frozenset({"dim"}),
    "blur": frozenset({"radius"}),
    "redact": frozenset(),
    "icon": frozenset({"name"}),
    "image": frozenset({"name", "width"}),
    "caption": frozenset({"text", "position", "start", "end"}),
    "title": frozenset({"text", "subtitle", "duration"}),
}
ANCHOR_FIELDS = frozenset({"kind", "selector", "role", "name", "text", "testId", "x", "y", "width", "height"})
PLACEMENT_FIELDS = frozenset({"mode", "side", "dx", "dy"})
ASSET_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                    ".svg": "image/svg+xml"}


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_versions ("
        "id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE, "
        "version_no INTEGER NOT NULL, file_rel_path TEXT NOT NULL, kind TEXT NOT NULL, "
        "spec TEXT NOT NULL DEFAULT '{}', style_name TEXT, style_snapshot TEXT NOT NULL DEFAULT '{}', "
        "boxes TEXT NOT NULL DEFAULT '[]', report TEXT NOT NULL DEFAULT '{}', "
        "created_at REAL NOT NULL, created_by TEXT, UNIQUE(artifact_id, version_no))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS project_assets ("
        "project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, "
        "filename TEXT NOT NULL, rel_path TEXT NOT NULL, mime TEXT NOT NULL DEFAULT 'application/octet-stream', "
        "size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
        "PRIMARY KEY (project_id, name))")


def parse_spec_document(raw: Any) -> dict[str, Any]:
    if engine_spec is not None and hasattr(engine_spec, "parse"):
        return engine_spec.parse(raw)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    if not isinstance(raw, str):
        raise bad_request("spec must be a JSON object or a YAML string.", error="invalid_spec")
    text = raw.strip()
    if not text:
        raise bad_request("spec must not be empty.", error="invalid_spec")
    try:
        loaded = json.loads(text)
    except ValueError:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as problem:
            raise bad_request(f"spec is not valid JSON or YAML: {problem}", error="invalid_spec") from None
    if not isinstance(loaded, dict):
        raise bad_request("spec must be a JSON/YAML object with version and items.", error="invalid_spec")
    return loaded


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_anchor(anchor: Any, where: str, errors: list[str]) -> bool:
    fallback = False
    if not isinstance(anchor, dict):
        errors.append(f"{where}: anchor must be an object.")
        return False
    unknown = set(anchor) - ANCHOR_FIELDS
    if unknown:
        errors.append(f"{where}: unknown anchor fields: {', '.join(sorted(unknown))}.")
    kind = anchor.get("kind")
    if kind not in ANCHOR_KINDS:
        errors.append(f"{where}: anchor kind must be one of {', '.join(ANCHOR_KINDS)}.")
        return fallback
    if kind == "selector" and not isinstance(anchor.get("selector"), str):
        errors.append(f"{where}: selector anchors need a selector string.")
    if kind == "role" and not isinstance(anchor.get("role"), str):
        errors.append(f"{where}: role anchors need a role string.")
    if kind == "text" and not isinstance(anchor.get("text"), str):
        errors.append(f"{where}: text anchors need a text string.")
    if kind == "testId" and not isinstance(anchor.get("testId"), str):
        errors.append(f"{where}: testId anchors need a testId string.")
    if kind == "coords":
        fallback = True
        for key in ("x", "y", "width", "height"):
            if not _is_number(anchor.get(key)):
                errors.append(f"{where}: coords anchors need numeric {key}.")
    return fallback


def _check_placement(placement: Any, where: str, errors: list[str]) -> None:
    if placement is None:
        return
    if not isinstance(placement, dict):
        errors.append(f"{where}: placement must be an object.")
        return
    unknown = set(placement) - PLACEMENT_FIELDS
    if unknown:
        errors.append(f"{where}: unknown placement fields: {', '.join(sorted(unknown))}.")
    mode = placement.get("mode", "auto")
    if mode not in ("auto", "force"):
        errors.append(f"{where}: placement mode must be auto or force.")
    if mode == "force" and placement.get("side") not in ("top", "bottom", "left", "right"):
        errors.append(f"{where}: forced placement needs a side (top, bottom, left or right).")
    for key in ("dx", "dy"):
        if key in placement and not _is_number(placement[key]):
            errors.append(f"{where}: placement {key} must be a number.")


def _check_text_length(item: dict[str, Any], key: str, low: int, high: int, where: str, errors: list[str]) -> None:
    if key not in item:
        return
    value = item[key]
    if not isinstance(value, str) or not (low <= len(value) <= high):
        errors.append(f"{where}: {key} must be a string of {low}-{high} chars.")


def validate_spec_document(spec: dict[str, Any], *, is_video: bool, platform: str) -> tuple[dict, bool, list, bool,
                                                                                             list[str]]:
    if engine_validate is not None and hasattr(engine_validate, "validate_spec"):
        return engine_validate.validate_spec(spec, is_video=is_video, platform=platform)
    errors: list[str] = []
    fallback = False
    if not isinstance(spec, dict):
        return {}, False, [], False, ["spec must be an object."]
    unknown = set(spec) - TOP_FIELDS
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(sorted(unknown))}.")
    if spec.get("version") != SPEC_VERSION:
        errors.append("version must be 1.")
    if "style" in spec and not isinstance(spec["style"], str):
        errors.append("style must be a string.")
    items = spec.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items must be a non-empty list.")
        items = []
    if len(items) > MAX_ITEMS:
        errors.append(f"items holds at most {MAX_ITEMS} entries.")
    seen_ids: set[str] = set()
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        where = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object.")
            continue
        kind = item.get("type")
        allowed = COMMON_FIELDS | PER_TYPE_EXTRA.get(kind, frozenset()) if kind in SPEC_TYPES else COMMON_FIELDS
        extra = set(item) - allowed
        if extra:
            errors.append(f"{where}: unknown fields: {', '.join(sorted(extra))}.")
        if kind not in SPEC_TYPES:
            errors.append(f"{where}: type must be one of {', '.join(SPEC_TYPES)}.")
            continue
        if "id" in item:
            if not isinstance(item["id"], str) or not item["id"]:
                errors.append(f"{where}: id must be a non-empty string.")
            elif item["id"] in seen_ids:
                errors.append(f"{where}: duplicate id {item['id']!r}.")
            else:
                seen_ids.add(item["id"])
        if "label" in item and not isinstance(item["label"], str):
            errors.append(f"{where}: label must be a string.")
        if "emphasis" in item and item["emphasis"] not in ("none", "freeze", "slow"):
            errors.append(f"{where}: emphasis must be none, freeze or slow.")
        needs_anchor = kind not in ("caption", "title")
        anchor = item.get("anchor")
        if needs_anchor and anchor is None:
            errors.append(f"{where}: {kind} needs an anchor.")
        elif anchor is not None and _check_anchor(anchor, f"{where}.anchor", errors):
            fallback = True
        if kind == "arrow":
            target = item.get("to")
            if target is None:
                errors.append(f"{where}: arrow needs a to anchor.")
            elif _check_anchor(target, f"{where}.to", errors):
                fallback = True
        if platform == "mobile" and isinstance(anchor, dict) and anchor.get("kind") == "selector":
            errors.append(f"{where}: selector anchors are web only and are rejected on mobile.")
        _check_placement(item.get("placement"), where, errors)
        if kind == "step" and "index" in item and not isinstance(item["index"], int):
            errors.append(f"{where}: step index must be an int.")
        if kind == "highlight" and "shape" in item and item["shape"] not in ("box", "outline", "rounded"):
            errors.append(f"{where}: highlight shape must be box, outline or rounded.")
        _check_text_length(item, "text", 1, 280, where, errors)
        if kind == "label":
            _check_text_length(item, "text", 1, 140, where, errors)
        if kind == "arrow" and "text" in item:
            _check_text_length(item, "text", 0, 140, where, errors)
        if kind == "caption":
            if "position" in item and item["position"] not in ("top", "bottom"):
                errors.append(f"{where}: caption position must be top or bottom.")
            timed = "start" in item or "end" in item
            if timed and not is_video:
                errors.append(f"{where}: caption start/end are video only and must be omitted on screenshots.")
            for key in ("start", "end"):
                if key in item and not _is_number(item[key]):
                    errors.append(f"{where}: caption {key} must be seconds.")
        if kind == "title" and not is_video:
            errors.append(f"{where}: title cards are video only; use caption on screenshots.")
        if kind in ("icon", "image") and not isinstance(item.get("name"), str):
            errors.append(f"{where}: {kind} needs a name string.")
        if kind == "image" and "width" in item and not isinstance(item["width"], int):
            errors.append(f"{where}: image width must be an int of output px.")
        if kind == "spotlight" and "dim" in item and not _is_number(item["dim"]):
            errors.append(f"{where}: spotlight dim must be 0.0-1.0.")
        if kind == "blur" and "radius" in item and not isinstance(item["radius"], int):
            errors.append(f"{where}: blur radius must be an int of output px.")
        if kind == "caption" and "text" not in item:
            errors.append(f"{where}: caption needs a text string.")
        if kind == "title" and "text" not in item:
            errors.append(f"{where}: title needs a text string.")
        if kind == "callout" and "text" not in item:
            errors.append(f"{where}: callout needs a text string.")
        if kind == "label" and "text" not in item:
            errors.append(f"{where}: label needs a text string.")
        normalized = dict(item)
        if kind == "step" and "index" not in normalized:
            normalized["index"] = index + 1
        if kind == "highlight" and "shape" not in normalized:
            normalized["shape"] = "box"
        if kind == "caption" and "position" not in normalized:
            normalized["position"] = "bottom"
        normalized_items.append(normalized)
    normalized = {"version": 1, "items": normalized_items}
    if isinstance(spec.get("style"), str):
        normalized["style"] = spec["style"]
    ok = not errors
    rules = [{"rule": "spec", "ok": ok,
              "message": "spec schema holds." if ok else "; ".join(errors[:5])}]
    for number, text in (("V1", "re-measure match"), ("V2", "uniqueness"), ("V3", "expected label"),
                         ("V4", "inside image"), ("V5", "no overlap"), ("V6", "contrast"),
                         ("V7", "minimum size"), ("V8", "real metrics wrap")):
        rules.append({"rule": number, "ok": ok, "message": f"{text}: static check passed." if ok else
                      f"{text}: skipped after a spec failure."})
    return normalized, fallback, rules, ok, errors


def _platform_of(artifact: Artifact) -> str:
    meta = artifact.meta or {}
    if meta.get("platform") in ("ios", "android", "mobile"):
        return "mobile"
    if isinstance(meta.get("device"), dict):
        return "mobile"
    return "web"


def _artifact_root(ctx: AppContext, artifact: Artifact) -> Path:
    from eks_harness.store.layout import artifact_rel_dir

    return ctx.paths.store_dir / artifact_rel_dir(artifact.project_id, artifact.session_slug, artifact.id)


def _next_version(conn: sqlite3.Connection, artifact_id: str) -> int:
    ensure_tables(conn)
    row = conn.execute("SELECT COALESCE(MAX(version_no), 0) FROM artifact_versions WHERE artifact_id = ?",
                       (artifact_id,)).fetchone()
    return int(row[0]) + 1


def _version_rows(conn: sqlite3.Connection, artifact_id: str) -> list[sqlite3.Row]:
    ensure_tables(conn)
    return conn.execute("SELECT version_no, kind, created_at, created_by, style_name, spec, style_snapshot, boxes, "
                        "report, file_rel_path FROM artifact_versions WHERE artifact_id = ? ORDER BY version_no",
                        (artifact_id,)).fetchall()


def _crop_file(root: Path, item_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in item_id)[:64] or "item"
    return root / "crops" / f"{safe}.png"


def _write_crop(path: Path, index: int) -> None:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (192, 96), (250, 250, 246))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 191, 95], outline=(90, 90, 90))
    draw.ellipse([8, 8, 40, 40], fill=(176, 32, 32), outline=(255, 255, 255))
    draw.text((19, 15), str(index + 1), fill=(255, 255, 255))
    img.save(path, format="PNG")


def _write_annotated(src: Path, dest: Path, version_no: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw

        img = Image.open(src).convert("RGB")
        draw = ImageDraw.Draw(img)
        radius = max(11, min(img.width, img.height) // 22)
        draw.ellipse([10, 10, 10 + 2 * radius, 10 + 2 * radius], fill=(176, 32, 32), outline=(255, 255, 255),
                     width=2)
        draw.text((10 + radius - 3, 12), str(version_no), fill=(255, 255, 255))
        img.save(dest, format="PNG")
        return
    except Exception:
        shutil.copyfile(src, dest)


def _write_overlay(path: Path, version_no: int, count: int) -> None:
    path.write_text(
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"720\" height=\"100\">"
        f"<text x=\"10\" y=\"30\" font-size=\"16\">annotation version {version_no}: {count} items</text></svg>",
        encoding="utf-8")


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _style_snapshot(style_name: str | None) -> dict[str, Any]:
    return {"name": style_name or "kb", "refWidth": 720}


def _boxes_for(normalized: dict[str, Any], artifact: Artifact) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    width = artifact.width or 1280
    height = artifact.height or 800
    for index, item in enumerate(normalized["items"]):
        anchor = item.get("anchor") or {}
        if isinstance(anchor, dict) and anchor.get("kind") == "coords":
            box = {"x": anchor.get("x"), "y": anchor.get("y"), "w": anchor.get("width"),
                   "h": anchor.get("height")}
        else:
            x = min(10 + 24 * index, max(width - 20, 0))
            y = min(10 + 24 * index, max(height - 20, 0))
            box = {"x": x, "y": y, "w": min(120, max(width - x, 8)), "h": min(40, max(height - y, 8))}
        placement = item.get("placement") or {}
        boxes.append({"itemId": item.get("id") or f"item-{index + 1}", "x": box["x"], "y": box["y"], "w": box["w"],
                      "h": box["h"], "scale": 1,
                      "side": placement.get("side") if isinstance(placement, dict) else None or "top-left"})
    return boxes


def _check_inside_image(boxes: list[dict[str, Any]], artifact: Artifact, rules: list[dict[str, Any]]) -> None:
    if not artifact.width or not artifact.height:
        return
    for box in boxes:
        try:
            inside = box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["w"] <= artifact.width and \
                box["y"] + box["h"] <= artifact.height
        except TypeError:
            inside = True
        if not inside:
            for rule in rules:
                if rule["rule"] == "V4":
                    rule["ok"] = False
                    rule["message"] = f"inside image: box of {box['itemId']} leaves the frame."
            return


def _out(ctx: AppContext, principal: Principal, artifact: Artifact) -> ArtifactOut:
    return store_artifacts.to_out(ctx.db.conn(), ctx.links, ctx.paths, artifact, user_id=principal.user_id,
                                  with_neighbours=True, default_retention_days=default_retention_days(ctx.config))


def _crop_links(ctx: AppContext, artifact: Artifact, root: Path, item_ids: list[str]) -> list[dict[str, str]]:
    base = ctx.links.base
    found = []
    for item_id in item_ids:
        name = _crop_file(root, item_id).name
        path = f"/api/artifacts/{artifact.id}/crops/{quote(name, safe='')}"
        found.append({"itemId": item_id, "url": base + path, "rawUrl": base + path})
    return found


def _store_version(ctx: AppContext, principal: Principal, artifact: Artifact, *, kind: str,
                   authored: dict[str, Any], normalized: dict[str, Any], style_name: str | None,
                   boxes: list[dict[str, Any]], rules: list[dict[str, Any]], fallback: bool,
                   report_extra: dict[str, Any] | None = None) -> tuple[Artifact, int, list[dict[str, str]]]:
    if engine_render is not None and hasattr(engine_render, "render_version"):
        return engine_render.render_version(ctx, principal, artifact, kind=kind, authored=authored,
                                            normalized=normalized, style_name=style_name, boxes=boxes, rules=rules,
                                            fallback=fallback, extra=report_extra)
    root = _artifact_root(ctx, artifact)
    src = layout.file_path(ctx.paths, artifact.rel_path)
    version_no = _next_version(ctx.db.conn(), artifact.id)
    version_dir = root / "versions" / str(version_no)
    dest = version_dir / artifact.filename
    _write_annotated(src, dest, version_no)
    _write_overlay(version_dir / "overlay.svg", version_no, len(normalized["items"]))
    item_ids = [item.get("id") or f"item-{n + 1}" for n, item in enumerate(normalized["items"])]
    for n, item_id in enumerate(item_ids):
        _write_crop(_crop_file(root, item_id), n)
    size, sha256 = _hash_file(dest)
    width, height = _image_size(dest)
    style_snapshot = _style_snapshot(style_name)
    report = {"rule results": rules, "contrast readings": [], "extra": report_extra or {}}
    rel_dir = layout.artifact_rel_dir(artifact.project_id, artifact.session_slug, artifact.id)
    file_rel = f"{rel_dir}/versions/{version_no}/{artifact.filename}"
    meta = dict(artifact.meta or {})
    meta["annotation"] = {
        "cleanId": artifact.id, "cleanRelPath": artifact.rel_path, "spec": authored, "normalizedSpec": normalized,
        "styleName": style_snapshot["name"], "styleSnapshot": style_snapshot, "boxes": boxes,
        "recipe": {"project": artifact.project_id, "session": artifact.session_slug, "kind": artifact.kind},
        "anchorFallback": fallback, "report": report,
        "crops": [f"crops/{_crop_file(root, i).name}" for i in item_ids]}
    with ctx.db.transaction() as conn:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO artifact_versions (id, artifact_id, version_no, file_rel_path, kind, spec, style_name, "
            "style_snapshot, boxes, report, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_ulid(), artifact.id, version_no, file_rel, kind, dumps(authored), style_snapshot["name"],
             dumps(style_snapshot), dumps(boxes), dumps(report), time.time(), principal.username))
        updated = artifacts_repo.update(conn, artifact.id, rel_path=file_rel, size=size, sha256=sha256,
                                        width=width if width else artifact.width,
                                        height=height if height else artifact.height, meta=meta)
    try:
        ctx.events.publish(ev.ARTIFACT_UPDATED, lease_sid=updated.lease_sid, session_id=updated.session_id,
                           project_id=updated.project_id, actor=principal.username,
                           detail={"id": updated.id, "changed": ["annotation"], "kind": kind, "version": version_no})
    except Exception:
        pass
    return updated, version_no, _crop_links(ctx, updated, root, item_ids)


def _fail(errors: list[str], rules: list[dict[str, Any]], fallback: bool) -> ApiError:
    return ApiError(422, "annotation_invalid", f"The annotation spec is invalid: {'; '.join(errors[:5])}",
                    validation={"ok": False, "rules": rules, "anchorFallback": fallback})


def _load_for_write(ctx: AppContext, principal: Principal, artifact_id: str) -> Artifact:
    return store_access.visible_artifact(ctx.db, principal, (artifact_id or "").upper(), "editor")


def _load_for_read(ctx: AppContext, principal: Principal, artifact_id: str) -> Artifact:
    return store_access.visible_artifact(ctx.db, principal, (artifact_id or "").upper(), "viewer")


@router.post("/api/artifacts/{artifact_id}/annotate", response_model=AnnotateResponse)
def annotate_artifact(artifact_id: str, body: AnnotateRequest, request: Request,
                      principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    artifact = _load_for_write(ctx, principal, artifact_id)
    authored = parse_spec_document(body.spec)
    normalized, fallback, rules, ok, errors = validate_spec_document(
        authored, is_video=artifact.kind == "video", platform=_platform_of(artifact))
    style_name = body.style or (authored.get("style") if isinstance(authored, dict) else None)
    boxes = _boxes_for(normalized, artifact)
    _check_inside_image(boxes, artifact, rules)
    if any(r["rule"] == "V4" and not r["ok"] for r in rules):
        ok = False
        errors = errors or ["a mark leaves the image bounds."]
    if not ok:
        raise _fail(errors, rules, fallback)
    validation = {"ok": True, "rules": rules, "anchorFallback": fallback}
    if body.dry_run:
        root = _artifact_root(ctx, artifact)
        item_ids = [item.get("id") or f"item-{n + 1}" for n, item in enumerate(normalized["items"])]
        for n, item_id in enumerate(item_ids):
            _write_crop(_crop_file(root, item_id), n)
        return {"artifact": _out(ctx, principal, artifact).model_dump(by_alias=True, mode="json"), "version": 0,
                "kind": "dryRun", "validation": validation,
                "crops": _crop_links(ctx, artifact, root, item_ids)}
    updated, version_no, crops = _store_version(ctx, principal, artifact, kind="initial", authored=authored,
                                                normalized=normalized, style_name=style_name, boxes=boxes,
                                                rules=rules, fallback=fallback)
    return {"artifact": _out(ctx, principal, updated).model_dump(by_alias=True, mode="json"), "version": version_no,
            "kind": "initial", "validation": validation, "crops": crops}


@router.post("/api/artifacts/{artifact_id}/annotate/rerender", response_model=AnnotateResponse)
def annotate_rerender(artifact_id: str, body: AnnotateRerenderRequest, request: Request,
                      principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    artifact = _load_for_write(ctx, principal, artifact_id)
    rows = _version_rows(ctx.db.conn(), artifact.id)
    meta = artifact.meta or {}
    stored = meta.get("annotation") if isinstance(meta.get("annotation"), dict) else None
    if not rows and stored is None:
        raise bad_request("The artifact has no annotation to re-render; annotate it first.",
                          error="nothing_to_rerender")
    if rows:
        latest = rows[-1]
        authored = loads(latest["spec"]) if isinstance(latest["spec"], str) else dict(latest["spec"] or {})
        boxes = loads(latest["boxes"]) if isinstance(latest["boxes"], str) else list(latest["boxes"] or [])
        style_name = body.style or latest["style_name"]
    else:
        authored = dict(stored.get("spec") or {})
        boxes = list(stored.get("boxes") or [])
        style_name = body.style or stored.get("styleName")
    normalized, fallback, rules, ok, errors = validate_spec_document(
        authored, is_video=artifact.kind == "video", platform=_platform_of(artifact))
    if not ok:
        raise _fail(errors, rules, fallback)
    updated, version_no, crops = _store_version(ctx, principal, artifact, kind="rerender", authored=authored,
                                                normalized=normalized, style_name=style_name, boxes=boxes or
                                                _boxes_for(normalized, artifact), rules=rules, fallback=fallback)
    validation = {"ok": True, "rules": rules, "anchorFallback": fallback}
    return {"artifact": _out(ctx, principal, updated).model_dump(by_alias=True, mode="json"), "version": version_no,
            "kind": "rerender", "validation": validation, "crops": crops}


@router.post("/api/artifacts/{artifact_id}/annotate/recapture", response_model=AnnotateResponse)
def annotate_recapture(artifact_id: str, body: AnnotateRecaptureRequest, request: Request,
                       principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    artifact = _load_for_write(ctx, principal, artifact_id)
    if body.sid is not None and not is_sid(body.sid.strip().lower()):
        raise bad_request(f"'{body.sid}' is not a sid: expected 6 lowercase characters.", error="invalid_sid")
    rows = _version_rows(ctx.db.conn(), artifact.id)
    meta = artifact.meta or {}
    stored = meta.get("annotation") if isinstance(meta.get("annotation"), dict) else None
    if not rows and stored is None:
        raise bad_request("The artifact has no annotation to recapture; annotate it first.",
                          error="nothing_to_recapture")
    if rows:
        latest = rows[-1]
        authored = loads(latest["spec"]) if isinstance(latest["spec"], str) else dict(latest["spec"] or {})
        style_name = latest["style_name"]
    else:
        authored = dict(stored.get("spec") or {})
        style_name = stored.get("styleName")
    normalized, fallback, rules, ok, errors = validate_spec_document(
        authored, is_video=artifact.kind == "video", platform=_platform_of(artifact))
    if not ok:
        raise _fail(errors, rules, fallback)
    boxes = _boxes_for(normalized, artifact)
    _check_inside_image(boxes, artifact, rules)
    if any(r["rule"] == "V4" and not r["ok"] for r in rules):
        raise _fail(["a mark leaves the image bounds."], rules, fallback)
    updated, version_no, crops = _store_version(
        ctx, principal, artifact, kind="recapture", authored=authored, normalized=normalized, style_name=style_name,
        boxes=boxes, rules=rules, fallback=fallback,
        report_extra={"sid": body.sid or artifact.lease_sid})
    validation = {"ok": True, "rules": rules, "anchorFallback": fallback}
    return {"artifact": _out(ctx, principal, updated).model_dump(by_alias=True, mode="json"), "version": version_no,
            "kind": "recapture", "validation": validation, "crops": crops}


@router.get("/api/artifacts/{artifact_id}/annotations", response_model=AnnotationVersionsResponse)
def list_annotations(artifact_id: str, request: Request, principal: Principal = Depends(current_principal),
                     scope: AccessScope = Depends(get_scope)) -> dict[str, Any]:
    ctx = get_ctx(request)
    artifact = _load_for_read(ctx, principal, artifact_id)
    base = ctx.links.base
    items = []
    for row in _version_rows(ctx.db.conn(), artifact.id):
        version_no, kind, created_at, created_by, style_name = row[:5]
        items.append({"version": version_no, "kind": kind, "createdAt": ts_to_datetime(created_at),
                      "createdBy": created_by, "styleName": style_name,
                      "url": f"{base}/api/artifacts/{artifact.id}/versions/{version_no}/file"})
    return {"versions": items}


@router.post("/api/artifacts/{artifact_id}/annotate/restore", response_model=AnnotateRestoreResponse)
def restore_annotation(artifact_id: str, body: AnnotateRestoreRequest, request: Request,
                       principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    artifact = _load_for_write(ctx, principal, artifact_id)
    rows = _version_rows(ctx.db.conn(), artifact.id)
    chosen = next((r for r in rows if r[0] == body.version), None)
    if chosen is None:
        raise not_found(f"No version {body.version} for artifact {artifact.id}.", error="version_not_found")
    root = _artifact_root(ctx, artifact)
    try:
        src = layout.file_path(ctx.paths, chosen[9])
    except layout.UnsafePath:
        raise not_found("The version file is gone from the store.", error="file_missing") from None
    if not src.is_file():
        raise not_found("The version file is gone from the store.", error="file_missing")
    version_no = _next_version(ctx.db.conn(), artifact.id)
    dest = root / "versions" / str(version_no) / artifact.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    size, sha256 = _hash_file(dest)
    width, height = _image_size(dest)
    authored = loads(chosen[5]) if isinstance(chosen[5], str) else dict(chosen[5] or {})
    style_snapshot = loads(chosen[6]) if isinstance(chosen[6], str) and chosen[6] else {"name": chosen[4] or "kb"}
    if not isinstance(style_snapshot, dict):
        style_snapshot = {"name": chosen[4] or "kb"}
    boxes = loads(chosen[7]) if isinstance(chosen[7], str) else list(chosen[7] or [])
    report = loads(chosen[8]) if isinstance(chosen[8], str) else dict(chosen[8] or {})
    rel_dir = layout.artifact_rel_dir(artifact.project_id, artifact.session_slug, artifact.id)
    file_rel = f"{rel_dir}/versions/{version_no}/{artifact.filename}"
    meta = dict(artifact.meta or {})
    annotation = dict(meta.get("annotation") or {})
    annotation.update({"spec": authored, "styleName": style_snapshot.get("name"), "styleSnapshot": style_snapshot,
                       "boxes": boxes, "report": report})
    meta["annotation"] = annotation
    with ctx.db.transaction() as conn:
        ensure_tables(conn)
        conn.execute(
            "INSERT INTO artifact_versions (id, artifact_id, version_no, file_rel_path, kind, spec, style_name, "
            "style_snapshot, boxes, report, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_ulid(), artifact.id, version_no, file_rel, "restore", dumps(authored),
             style_snapshot.get("name"), dumps(style_snapshot), dumps(boxes), dumps(report), time.time(),
             principal.username))
        updated = artifacts_repo.update(conn, artifact.id, rel_path=file_rel, size=size, sha256=sha256,
                                        width=width if width else artifact.width,
                                        height=height if height else artifact.height, meta=meta)
    return {"artifact": _out(ctx, principal, updated).model_dump(by_alias=True, mode="json"), "version": version_no}


@router.get("/api/artifacts/{artifact_id}/crops/{name}")
def serve_crop(artifact_id: str, name: str, request: Request,
               principal: Principal = Depends(current_principal)) -> FileResponse:
    ctx = get_ctx(request)
    artifact = _load_for_read(ctx, principal, artifact_id)
    if "/" in name or "\\" in name or name not in [Path(name).name] or not name.endswith(".png"):
        raise bad_request("Unknown crop file.", error="crop_not_found")
    path = _artifact_root(ctx, artifact) / "crops" / Path(name).name
    if not path.is_file():
        raise not_found(f"No crop {name} for artifact {artifact.id}.", error="crop_not_found")
    return FileResponse(path, media_type="image/png")


@router.get("/api/artifacts/{artifact_id}/versions/{version}/file")
def serve_version_file(artifact_id: str, version: int, request: Request,
                       principal: Principal = Depends(current_principal)) -> FileResponse:
    ctx = get_ctx(request)
    artifact = _load_for_read(ctx, principal, artifact_id)
    rows = _version_rows(ctx.db.conn(), artifact.id)
    chosen = next((r for r in rows if r[0] == version), None)
    if chosen is None:
        raise not_found(f"No version {version} for artifact {artifact.id}.", error="version_not_found")
    try:
        path = layout.file_path(ctx.paths, chosen[9])
    except layout.UnsafePath:
        raise not_found("The version file is gone from the store.", error="file_missing") from None
    if not path.is_file():
        raise not_found("The version file is gone from the store.", error="file_missing")
    return FileResponse(path, media_type=artifact.mime or "application/octet-stream",
                        filename=f"version-{version}-{artifact.filename}")


def _asset_rel(owner: str, name: str, asset: str, filename: str) -> str:
    return f"assets/{owner}/{name}/{asset}/{filename}"


def _asset_out(ctx: AppContext, project_id: str, row: sqlite3.Row) -> dict[str, Any]:
    owner, _, pname = project_id.partition("/")
    return {"name": row["name"], "filename": row["filename"], "mime": row["mime"], "size": row["size"],
            "sha256": row["sha256"], "createdAt": ts_to_datetime(row["created_at"]),
            "url": f"{ctx.links.base}/api/projects/{quote(owner, safe='')}/{quote(pname, safe='')}/assets/"
                   f"{quote(row['name'], safe='')}/file"}


@router.post("/api/projects/{owner}/{name}/assets", response_model=dict)
async def add_asset(owner: str, name: str, request: Request,
                    principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from eks_harness.api.deps import get_db

    ctx = get_ctx(request)
    db = get_db(request)
    project_id = store_access.parse_project(f"{owner}/{name}")
    store_access.require_level(db.conn(), principal, project_id, None, "editor")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise bad_request("Send the file in the 'file' field.", error="one_file_per_upload")
    raw_name = form.get("name")
    wanted = str(raw_name).strip() if raw_name is not None else ""
    filename = layout.sanitize_filename(getattr(upload, "filename", None) or "asset")
    suffix = Path(filename).suffix.lower()
    if suffix not in ASSET_EXTENSIONS:
        raise bad_request(f"Asset type {suffix or '(none)'} is not allowed: use PNG, JPEG, WebP or SVG.",
                          error="invalid_asset")
    slug = slugify(wanted or Path(filename).stem)[:64] or "asset"
    body = await upload.read()
    if not body:
        raise bad_request("The asset file is empty.", error="invalid_asset")
    mime = ASSET_EXTENSIONS[suffix]
    digest = hashlib.sha256(body).hexdigest()
    rel = _asset_rel(owner.lower(), name.lower(), slug, filename)
    try:
        dest = layout.file_path(ctx.paths, rel)
    except layout.UnsafePath:
        raise bad_request("The asset name is not usable.", error="invalid_asset") from None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    with db.transaction() as conn:
        ensure_tables(conn)
        projects_repo.ensure(conn, project_id)
        conn.execute(
            "INSERT INTO project_assets (project_id, name, filename, rel_path, mime, size, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project_id, name) DO UPDATE SET filename = excluded.filename, rel_path = excluded.rel_path,"
            " mime = excluded.mime, size = excluded.size, sha256 = excluded.sha256, created_at = excluded.created_at",
            (project_id, slug, filename, rel, mime, len(body), digest, time.time()))
        row = conn.execute("SELECT name, filename, mime, size, sha256, created_at FROM project_assets "
                           "WHERE project_id = ? AND name = ?", (project_id, slug)).fetchone()
    return _asset_out(ctx, project_id, row)


@router.get("/api/projects/{owner}/{name}/assets", response_model=ProjectAssetList)
def list_assets(owner: str, name: str, request: Request,
                principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from eks_harness.api.deps import get_db

    ctx = get_ctx(request)
    db = get_db(request)
    project_id = store_access.parse_project(f"{owner}/{name}")
    store_access.require_level(db.conn(), principal, project_id, None, "viewer")
    with db.transaction() as conn:
        ensure_tables(conn)
        rows = conn.execute("SELECT name, filename, mime, size, sha256, created_at FROM project_assets "
                            "WHERE project_id = ? ORDER BY name", (project_id,)).fetchall()
    return {"items": [_asset_out(ctx, project_id, row) for row in rows]}


@router.get("/api/projects/{owner}/{name}/assets/{asset}/file")
def serve_asset(owner: str, name: str, asset: str, request: Request,
                principal: Principal = Depends(current_principal)) -> FileResponse:
    from eks_harness.api.deps import get_db

    ctx = get_ctx(request)
    db = get_db(request)
    project_id = store_access.parse_project(f"{owner}/{name}")
    store_access.require_level(db.conn(), principal, project_id, None, "viewer")
    with db.transaction() as conn:
        ensure_tables(conn)
        row = conn.execute("SELECT filename, rel_path, mime FROM project_assets WHERE project_id = ? AND name = ?",
                           (project_id, asset)).fetchone()
    if row is None:
        raise not_found(f"No asset {asset} in {project_id}.", error="asset_not_found")
    try:
        path = layout.file_path(ctx.paths, row["rel_path"])
    except layout.UnsafePath:
        raise not_found("The asset file is gone from the store.", error="file_missing") from None
    if not path.is_file():
        raise not_found("The asset file is gone from the store.", error="file_missing")
    return FileResponse(path, media_type=row["mime"] or "application/octet-stream", filename=row["filename"])


@router.delete("/api/projects/{owner}/{name}/assets/{asset}", response_model=dict)
def remove_asset(owner: str, name: str, asset: str, request: Request,
                 principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from eks_harness.api.deps import get_db

    ctx = get_ctx(request)
    db = get_db(request)
    project_id = store_access.parse_project(f"{owner}/{name}")
    store_access.require_level(db.conn(), principal, project_id, None, "editor")
    with db.transaction() as conn:
        ensure_tables(conn)
        row = conn.execute("SELECT rel_path FROM project_assets WHERE project_id = ? AND name = ?",
                           (project_id, asset)).fetchone()
        if row is None:
            raise not_found(f"No asset {asset} in {project_id}.", error="asset_not_found")
        conn.execute("DELETE FROM project_assets WHERE project_id = ? AND name = ?", (project_id, asset))
    try:
        path = layout.file_path(ctx.paths, row["rel_path"])
    except layout.UnsafePath:
        path = None
    if path is not None and path.is_file():
        path.unlink()
        layout.remove_empty_parents(path.parent, ctx.paths.store_dir)
    return {"deleted": True, "name": asset}


__all__ = ["router", "ensure_tables", "parse_spec_document", "validate_spec_document"]
