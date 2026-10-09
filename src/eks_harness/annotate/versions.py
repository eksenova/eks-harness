from __future__ import annotations

import json
import posixpath
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from eks_harness.ids import new_ulid

VERSION_KINDS = ("initial", "rerender", "recapture", "restore")


@dataclass(frozen=True)
class Recipe:
    kind: str
    ref: str
    viewport: str
    project: str
    session: str | None = None
    device: str | None = None
    persona: str | None = None
    url: str | None = None
    route: str | None = None
    sid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "ref": self.ref, "viewport": self.viewport,
                                "project": self.project}
        if self.session is not None:
            data["session"] = self.session
        if self.device is not None:
            data["device"] = self.device
        if self.persona is not None:
            data["persona"] = self.persona
        if self.url is not None:
            data["url"] = self.url
        if self.route is not None:
            data["route"] = self.route
        if self.sid is not None:
            data["sid"] = self.sid
        return data

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Recipe:
        if raw.get("kind") not in ("script", "flow"):
            raise ValueError("recipe kind must be script or flow.")
        return Recipe(kind=raw["kind"], ref=str(raw.get("ref", "")), viewport=str(raw.get("viewport", "desktop")),
                      project=str(raw.get("project", "")), session=raw.get("session"), device=raw.get("device"),
                      persona=raw.get("persona"), url=raw.get("url"), route=raw.get("route"), sid=raw.get("sid"))


@dataclass(frozen=True)
class AnnotationVersion:
    id: str
    artifact_id: str
    version_no: int
    file_rel_path: str
    kind: str
    spec: dict[str, Any]
    style_name: str
    style_snapshot: dict[str, Any]
    boxes: dict[str, Any]
    report: dict[str, Any]
    created_at: float
    created_by: str | None


@dataclass
class AnnotationMeta:
    clean_id: str
    spec: dict[str, Any]
    normalized_spec: dict[str, Any]
    style_name: str
    style_snapshot: dict[str, Any]
    boxes: dict[str, Any]
    timeline: list[dict[str, Any]] = field(default_factory=list)
    recipe: dict[str, Any] = field(default_factory=dict)
    anchor_fallback: bool = False
    report: dict[str, Any] = field(default_factory=dict)
    crops: list[str] = field(default_factory=list)
    superseded: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "cleanId": self.clean_id,
            "spec": self.spec,
            "normalizedSpec": self.normalized_spec,
            "styleName": self.style_name,
            "styleSnapshot": self.style_snapshot,
            "boxes": self.boxes,
            "timeline": list(self.timeline),
            "recipe": dict(self.recipe),
            "anchorFallback": self.anchor_fallback,
            "report": dict(self.report),
            "crops": list(self.crops),
        }
        if self.superseded is not None:
            data["superseded"] = dict(self.superseded)
        return data

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> AnnotationMeta:
        return AnnotationMeta(
            clean_id=str(raw.get("cleanId", "")),
            spec=dict(raw.get("spec", {})),
            normalized_spec=dict(raw.get("normalizedSpec", {})),
            style_name=str(raw.get("styleName", "")),
            style_snapshot=dict(raw.get("styleSnapshot", {})),
            boxes=dict(raw.get("boxes", {})),
            timeline=list(raw.get("timeline", [])),
            recipe=dict(raw.get("recipe", {})),
            anchor_fallback=bool(raw.get("anchorFallback", False)),
            report=dict(raw.get("report", {})),
            crops=list(raw.get("crops", [])),
            superseded=dict(raw["superseded"]) if raw.get("superseded") else None,
        )


def version_rel_dir(artifact_rel_dir: str, version_no: int) -> str:
    return f"{artifact_rel_dir}/versions/{version_no}"


def version_rel_path(artifact_rel_dir: str, version_no: int, filename: str) -> str:
    return f"{version_rel_dir(artifact_rel_dir, version_no)}/{filename}"


def crop_rel_path(artifact_rel_dir: str, item_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in item_id) or "item"
    return f"{artifact_rel_dir}/crops/{safe}.png"


def _row_to_version(row: sqlite3.Row) -> AnnotationVersion:
    return AnnotationVersion(
        id=row["id"], artifact_id=row["artifact_id"], version_no=row["version_no"],
        file_rel_path=row["file_rel_path"], kind=row["kind"],
        spec=json.loads(row["spec"] or "{}"), style_name=row["style_name"] or "",
        style_snapshot=json.loads(row["style_snapshot"] or "{}"),
        boxes=json.loads(row["boxes"] or "{}"), report=json.loads(row["report"] or "{}"),
        created_at=row["created_at"], created_by=row["created_by"])


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_versions ("
        "id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,"
        " version_no INTEGER NOT NULL, file_rel_path TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT 'initial', spec TEXT NOT NULL DEFAULT '{}',"
        " style_name TEXT NOT NULL DEFAULT '', style_snapshot TEXT NOT NULL DEFAULT '{}',"
        " boxes TEXT NOT NULL DEFAULT '{}', report TEXT NOT NULL DEFAULT '{}',"
        " created_at REAL NOT NULL, created_by TEXT)")


def next_version_no(conn: sqlite3.Connection, artifact_id: str) -> int:
    _ensure_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version_no), 0) FROM artifact_versions WHERE artifact_id = ?",
                       (artifact_id,)).fetchone()
    return int(row[0]) + 1


def record_version(conn: sqlite3.Connection, *, artifact_id: str, file_rel_path: str, kind: str,
                   spec: dict[str, Any], style_name: str, style_snapshot: dict[str, Any],
                   boxes: dict[str, Any], report: dict[str, Any],
                   created_by: str | None = None,
                   created_at: float | None = None) -> AnnotationVersion:
    if kind not in VERSION_KINDS:
        raise ValueError(f"version kind must be one of {VERSION_KINDS}.")
    _ensure_table(conn)
    version_no = next_version_no(conn, artifact_id)
    version_id = new_ulid()
    stamp = created_at or time.time()
    conn.execute(
        "INSERT INTO artifact_versions (id, artifact_id, version_no, file_rel_path, kind, spec,"
        " style_name, style_snapshot, boxes, report, created_at, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (version_id, artifact_id, version_no, file_rel_path, kind, json.dumps(spec),
         style_name, json.dumps(style_snapshot), json.dumps(boxes), json.dumps(report),
         stamp, created_by))
    row = conn.execute("SELECT * FROM artifact_versions WHERE id = ?", (version_id,)).fetchone()
    return _row_to_version(row)


def list_versions(conn: sqlite3.Connection, artifact_id: str) -> list[AnnotationVersion]:
    _ensure_table(conn)
    rows = conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_no",
                        (artifact_id,)).fetchall()
    return [_row_to_version(row) for row in rows]


def get_version(conn: sqlite3.Connection, artifact_id: str, version_no: int) -> AnnotationVersion | None:
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM artifact_versions WHERE artifact_id = ? AND version_no = ?",
                       (artifact_id, version_no)).fetchone()
    return _row_to_version(row) if row is not None else None


def mark_superseded(meta: dict[str, Any], by_id: str, at_iso: str) -> dict[str, Any]:
    updated = dict(meta)
    annotation = dict(updated.get("annotation", {}))
    annotation["superseded"] = {"by": by_id, "at": at_iso}
    updated["annotation"] = annotation
    return updated


def overlay_filename(clean_filename: str) -> str:
    stem = posixpath.basename(clean_filename)
    base, dot, _ = stem.rpartition(".")
    return f"{base or stem}-annotated.png" if dot else f"{stem}-annotated.png"
