from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from eks_harness.db.common import UNSET, assignments, now

LOCAL_USERNAME = "local"
ROLES = ("admin", "member")


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str | None
    role: str
    disabled: bool
    builtin: bool
    created_at: float
    updated_at: float

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


def _row(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        id=row["id"], username=row["username"], password_hash=row["password_hash"], role=row["role"],
        disabled=bool(row["disabled"]), builtin=bool(row["builtin"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    return _row(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def local_user(conn: sqlite3.Connection) -> User:
    user = _row(conn.execute("SELECT * FROM users WHERE builtin = 1 AND username = ?", (LOCAL_USERNAME,)).fetchone())
    if user is None:
        ts = now()
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role, disabled, builtin, created_at, updated_at) "
            "VALUES (?, NULL, 'admin', 0, 1, ?, ?)", (LOCAL_USERNAME, ts, ts))
        user = get_by_username(conn, LOCAL_USERNAME)
    return user


def list_all(conn: sqlite3.Connection, include_builtin: bool = True) -> list[User]:
    sql = "SELECT * FROM users" + ("" if include_builtin else " WHERE builtin = 0") + " ORDER BY username COLLATE NOCASE"
    return [_row(r) for r in conn.execute(sql)]


def create(conn: sqlite3.Connection, username: str, role: str = "member", password_hash: str | None = None,
           disabled: bool = False) -> User:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    ts = now()
    cursor = conn.execute(
        "INSERT INTO users (username, password_hash, role, disabled, builtin, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)", (username, password_hash, role, int(disabled), ts, ts))
    return get(conn, cursor.lastrowid)


def update(conn: sqlite3.Connection, user_id: int, *, username=UNSET, password_hash=UNSET, role=UNSET,
           disabled=UNSET) -> User | None:
    if role is not UNSET and role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    fields = {"username": username, "password_hash": password_hash, "role": role,
              "disabled": int(disabled) if disabled is not UNSET else UNSET}
    sql, params = assignments(fields)
    if sql:
        conn.execute(f"UPDATE users SET {sql}, updated_at = ? WHERE id = ?", (*params, now(), user_id))
    return get(conn, user_id)


def delete(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("DELETE FROM users WHERE id = ? AND builtin = 0", (user_id,)).rowcount > 0


def count_admins(conn: sqlite3.Connection, include_builtin: bool = False, enabled_only: bool = True) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    if not include_builtin:
        sql += " AND builtin = 0"
    if enabled_only:
        sql += " AND disabled = 0"
    return conn.execute(sql).fetchone()[0]
