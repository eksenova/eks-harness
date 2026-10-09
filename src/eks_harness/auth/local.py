from __future__ import annotations

import hmac
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass

from eks_harness.auth import core, keys
from eks_harness.auth import users as auth_users
from eks_harness.db import Database, open_database
from eks_harness.db.repos import api_keys, users
from eks_harness.paths import Paths


@contextmanager
def local_database(paths: Paths) -> Iterator[Database]:
    paths.ensure()
    database = open_database(paths.db_file)
    try:
        users.local_user(database.conn())
        yield database
    finally:
        database.close()


@dataclass(frozen=True)
class AdminResult:
    user: users.User
    created: bool
    password_set: bool
    promoted: bool = False
    enabled: bool = False


def ensure_admin(db: Database, username: str, password: str | None = None, *, promote: bool = False) -> AdminResult:
    username = auth_users.validate_username(username)
    if password is not None:
        auth_users.validate_password(password)
    conn = db.conn()
    existing = users.get_by_username(conn, username)
    if existing is None:
        user = auth_users.create_user(db, username, role="admin", password=password)
        return AdminResult(user=user, created=True, password_set=password is not None)
    if existing.builtin:
        raise auth_users.AccountError(400, "builtin_user", "The built-in local user cannot be an admin login.")
    promoted = False
    if not existing.is_admin:
        if not promote:
            raise auth_users.AccountError(
                409, "not_admin", f"{existing.username} exists but is a member, not an admin; pick another "
                                  f"username or allow promoting it.")
        promoted = True
    enabled = existing.disabled
    user = auth_users.update_user(db, existing, role="admin" if promoted else None,
                                disabled=False if enabled else None, password=password)
    return AdminResult(user=user, created=False, password_set=password is not None, promoted=promoted,
                       enabled=enabled)


def key_belongs_to(db: Database, raw: str | None, user_id: int) -> api_keys.ApiKey | None:
    parsed = keys.parse_api_key(raw or "")
    if parsed is None:
        return None
    prefix, secret = parsed
    record = api_keys.get_by_prefix(db.conn(), prefix)
    if record is None or record.revoked_at is not None or record.user_id != user_id:
        return None
    if not hmac.compare_digest(record.secret_sha256, core.sha256_hex(secret)):
        return None
    return record


def admin_candidates(db: Database) -> list[users.User]:
    return [u for u in users.list_all(db.conn(), include_builtin=False) if u.is_admin]


def usable_admin_exists(db: Database) -> bool:
    conn = db.conn()
    for user in users.list_all(conn, include_builtin=False):
        if not user.is_admin or user.disabled:
            continue
        if user.password_hash or api_keys.list_keys(conn, user.id, include_revoked=False):
            return True
    return False
