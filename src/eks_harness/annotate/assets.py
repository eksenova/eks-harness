from __future__ import annotations

import hashlib
import posixpath
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eks_harness.annotate.spec import ASSET_NAME_MAX, check_asset_filename, normalize_asset_name
from eks_harness.annotate.spec import SpecError


@dataclass(frozen=True)
class ProjectAsset:
    project_id: str
    name: str
    filename: str
    rel_path: str
    mime: str
    size: int
    sha256: str
    created_at: float


def asset_rel_dir(project_id: str, name: str) -> str:
    owner, _, project = project_id.partition("/")
    return str(posixpath.join("assets", owner, project, name))


def asset_rel_path(project_id: str, name: str, filename: str) -> str:
    return f"{asset_rel_dir(project_id, name)}/{filename}"


def register_asset(conn: sqlite3.Connection, *, project_id: str, name: str, filename: str,
                   rel_path: str, mime: str, size: int, sha256: str,
                   created_at: float | None = None) -> ProjectAsset:
    conn.execute(
        "INSERT INTO project_assets (project_id, name, filename, rel_path, mime, size, sha256, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (project_id, name) DO UPDATE SET filename = excluded.filename,"
        " rel_path = excluded.rel_path, mime = excluded.mime, size = excluded.size,"
        " sha256 = excluded.sha256, created_at = excluded.created_at",
        (project_id, name, filename, rel_path, mime, size, sha256, created_at or time.time()))
    row = conn.execute(
        "SELECT project_id, name, filename, rel_path, mime, size, sha256, created_at"
        " FROM project_assets WHERE project_id = ? AND name = ?",
        (project_id, name)).fetchone()
    return ProjectAsset(project_id=row[0], name=row[1], filename=row[2], rel_path=row[3],
                        mime=row[4], size=row[5], sha256=row[6], created_at=row[7])


def list_assets(conn: sqlite3.Connection, project_id: str) -> list[ProjectAsset]:
    rows = conn.execute(
        "SELECT project_id, name, filename, rel_path, mime, size, sha256, created_at"
        " FROM project_assets WHERE project_id = ? ORDER BY name",
        (project_id,)).fetchall()
    return [ProjectAsset(project_id=row[0], name=row[1], filename=row[2], rel_path=row[3],
                         mime=row[4], size=row[5], sha256=row[6], created_at=row[7]) for row in rows]


def get_asset(conn: sqlite3.Connection, project_id: str, name: str) -> ProjectAsset | None:
    row = conn.execute(
        "SELECT project_id, name, filename, rel_path, mime, size, sha256, created_at"
        " FROM project_assets WHERE project_id = ? AND name = ?",
        (project_id, name)).fetchone()
    if row is None:
        return None
    return ProjectAsset(project_id=row[0], name=row[1], filename=row[2], rel_path=row[3],
                        mime=row[4], size=row[5], sha256=row[6], created_at=row[7])


def delete_asset(conn: sqlite3.Connection, project_id: str, name: str) -> ProjectAsset | None:
    asset = get_asset(conn, project_id, name)
    if asset is None:
        return None
    conn.execute("DELETE FROM project_assets WHERE project_id = ? AND name = ?",
                 (project_id, name))
    return asset


def prepare_upload(filename: str, content: bytes, *, requested_name: str | None = None) -> dict[str, Any]:
    suffix = check_asset_filename(filename)
    name = normalize_asset_name(requested_name, filename)
    if len(name) > ASSET_NAME_MAX:
        raise SpecError(f"asset name must be at most {ASSET_NAME_MAX} chars.")
    mime_by_suffix = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".webp": "image/webp", ".svg": "image/svg+xml"}
    return {"name": name, "filename": Path(filename).name, "mime": mime_by_suffix[suffix],
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
