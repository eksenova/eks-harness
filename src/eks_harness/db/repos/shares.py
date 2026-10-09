from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import now


@dataclass(frozen=True)
class Share:
    token: str
    artifact_id: str
    created_by: str | None
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    views: int
    last_viewed_at: float | None

    def active(self, at: float | None = None) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or (at or now()) < self.expires_at


def _row(row: sqlite3.Row | None) -> Share | None:
    if row is None:
        return None
    return Share(
        token=row["token"], artifact_id=row["artifact_id"], created_by=row["created_by"],
        created_at=row["created_at"], expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        views=row["views"], last_viewed_at=row["last_viewed_at"],
    )


def create(conn: sqlite3.Connection, token: str, artifact_id: str, created_by: str | None,
           expires_at: float | None = None) -> Share:
    conn.execute(
        "INSERT INTO shares (token, artifact_id, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, artifact_id, created_by, now(), expires_at))
    return get(conn, token)


def get(conn: sqlite3.Connection, token: str) -> Share | None:
    return _row(conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone())


def list_for_artifact(conn: sqlite3.Connection, artifact_id: str, include_inactive: bool = True) -> list[Share]:
    shares = [_row(r) for r in conn.execute(
        "SELECT * FROM shares WHERE artifact_id = ? ORDER BY created_at DESC", (artifact_id,))]
    return shares if include_inactive else [s for s in shares if s.active()]


def count_active(conn: sqlite3.Connection, artifact_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM shares WHERE artifact_id = ? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)", (artifact_id, now())).fetchone()[0]


def revoke(conn: sqlite3.Connection, token: str) -> bool:
    return conn.execute("UPDATE shares SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL",
                        (now(), token)).rowcount > 0


def record_view(conn: sqlite3.Connection, token: str, ts: float | None = None) -> None:
    conn.execute("UPDATE shares SET views = views + 1, last_viewed_at = ? WHERE token = ?", (ts or now(), token))


def delete(conn: sqlite3.Connection, token: str) -> bool:
    return conn.execute("DELETE FROM shares WHERE token = ?", (token,)).rowcount > 0


def count_for_session(conn: sqlite3.Connection, session_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM shares WHERE artifact_id IN (SELECT id FROM artifacts WHERE session_id = ?)",
        (session_id,)).fetchone()[0]


def count_for_project(conn: sqlite3.Connection, project_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM shares WHERE artifact_id IN (SELECT id FROM artifacts WHERE project_id = ?)",
        (project_id,)).fetchone()[0]


def expired(conn: sqlite3.Connection, at: float | None = None) -> list[Share]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM shares WHERE revoked_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?",
        (at or now(),))]


def delete_inactive_before(conn: sqlite3.Connection, before: float) -> int:
    return conn.execute(
        "DELETE FROM shares WHERE (revoked_at IS NOT NULL AND revoked_at < ?) "
        "OR (expires_at IS NOT NULL AND expires_at < ?)", (before, before)).rowcount
