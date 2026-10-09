from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import now


@dataclass(frozen=True)
class ApiKey:
    id: int
    user_id: int
    name: str
    prefix: str
    secret_sha256: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None
    username: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _row(row: sqlite3.Row | None) -> ApiKey | None:
    if row is None:
        return None
    keys = row.keys()
    return ApiKey(
        id=row["id"], user_id=row["user_id"], name=row["name"], prefix=row["prefix"],
        secret_sha256=row["secret_sha256"], created_at=row["created_at"], last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"], username=row["username"] if "username" in keys else None,
    )


_SELECT = "SELECT k.*, u.username AS username FROM api_keys k JOIN users u ON u.id = k.user_id"


def insert(conn: sqlite3.Connection, user_id: int, name: str, prefix: str, secret_sha256: str) -> ApiKey:
    cursor = conn.execute(
        "INSERT INTO api_keys (user_id, name, prefix, secret_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, name or "", prefix, secret_sha256, now()))
    return get(conn, cursor.lastrowid)


def get(conn: sqlite3.Connection, key_id: int) -> ApiKey | None:
    return _row(conn.execute(f"{_SELECT} WHERE k.id = ?", (key_id,)).fetchone())


def get_by_prefix(conn: sqlite3.Connection, prefix: str) -> ApiKey | None:
    return _row(conn.execute(f"{_SELECT} WHERE k.prefix = ?", (prefix,)).fetchone())


def prefix_exists(conn: sqlite3.Connection, prefix: str) -> bool:
    return conn.execute("SELECT 1 FROM api_keys WHERE prefix = ?", (prefix,)).fetchone() is not None


def list_keys(conn: sqlite3.Connection, user_id: int | None = None, include_revoked: bool = True) -> list[ApiKey]:
    clauses, params = [], []
    if user_id is not None:
        clauses.append("k.user_id = ?")
        params.append(user_id)
    if not include_revoked:
        clauses.append("k.revoked_at IS NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return [_row(r) for r in conn.execute(f"{_SELECT}{where} ORDER BY k.created_at DESC", params)]


def revoke(conn: sqlite3.Connection, key_id: int) -> bool:
    return conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (now(), key_id)).rowcount > 0


def revoke_all_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("UPDATE api_keys SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                        (now(), user_id)).rowcount


def touch(conn: sqlite3.Connection, key_id: int, ts: float | None = None) -> None:
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (ts or now(), key_id))


def delete(conn: sqlite3.Connection, key_id: int) -> bool:
    return conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,)).rowcount > 0
