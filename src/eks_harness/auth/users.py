from __future__ import annotations

import re
import sqlite3

from eks_harness.auth import core
from eks_harness.db import Database
from eks_harness.db.common import UNSET
from eks_harness.db.repos import users, web_sessions

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 512


class AccountError(ValueError):
    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise AccountError(400, "invalid_username",
                           "A username is 1 to 64 characters: letters, digits, '.', '_', '@' or '-', "
                           "starting with a letter or digit.")
    if username.casefold() == users.LOCAL_USERNAME:
        raise AccountError(400, "reserved_username", f"'{users.LOCAL_USERNAME}' is reserved for the built-in user.")
    return username


def validate_password(password: str) -> str:
    if password is None or len(password) < PASSWORD_MIN:
        raise AccountError(400, "weak_password", f"A password needs at least {PASSWORD_MIN} characters.")
    if len(password) > PASSWORD_MAX:
        raise AccountError(400, "invalid_password", f"A password can have at most {PASSWORD_MAX} characters.")
    return password


def resolve_user(conn: sqlite3.Connection, ref: str | int | None = None, *, user_id: int | None = None,
                 username: str | None = None) -> users.User:
    user = None
    if user_id is not None:
        user = users.get(conn, int(user_id))
    elif username:
        user = users.get_by_username(conn, username.strip())
    elif ref is not None:
        text = str(ref).strip()
        if text.isdigit():
            user = users.get(conn, int(text))
        if user is None:
            user = users.get_by_username(conn, text)
    if user is None:
        what = user_id if user_id is not None else (username or ref)
        raise AccountError(404, "user_not_found", f"No user {what}.")
    return user


def _removes_admin(user: users.User, role_after: str, disabled_after: bool, deleting: bool) -> bool:
    if user.builtin or not user.is_admin or user.disabled:
        return False
    return deleting or role_after != "admin" or disabled_after


def _guard_last_admin(conn: sqlite3.Connection, user: users.User, role_after: str, disabled_after: bool,
                      deleting: bool) -> None:
    if _removes_admin(user, role_after, disabled_after, deleting) and users.count_admins(conn) <= 1:
        raise AccountError(409, "last_admin",
                           f"{user.username} is the only enabled admin; add or enable another admin first.")


def create_user(db: Database, username: str, role: str = "member", password: str | None = None,
                disabled: bool = False) -> users.User:
    username = validate_username(username)
    if role not in users.ROLES:
        raise AccountError(400, "invalid_role", f"role must be one of {', '.join(users.ROLES)}")
    password_hash = core.hash_password(validate_password(password)) if password is not None else None
    with db.transaction() as conn:
        if users.get_by_username(conn, username) is not None:
            raise AccountError(409, "user_exists", f"A user named {username} already exists.")
        return users.create(conn, username, role=role, password_hash=password_hash, disabled=disabled)


def update_user(db: Database, user: users.User, *, username: str | None = None, password: str | None = None,
                role: str | None = None, disabled: bool | None = None,
                keep_web_session: str | None = None) -> users.User:
    if user.builtin:
        raise AccountError(403, "builtin_user", "The built-in local user cannot be changed.")
    if username is not None:
        username = validate_username(username)
    if role is not None and role not in users.ROLES:
        raise AccountError(400, "invalid_role", f"role must be one of {', '.join(users.ROLES)}")
    password_hash = core.hash_password(validate_password(password)) if password is not None else None
    with db.transaction() as conn:
        current = users.get(conn, user.id)
        if current is None:
            raise AccountError(404, "user_not_found", f"No user {user.username}.")
        role_after = role if role is not None else current.role
        disabled_after = disabled if disabled is not None else current.disabled
        _guard_last_admin(conn, current, role_after, disabled_after, deleting=False)
        if username is not None and username.casefold() != current.username.casefold():
            if users.get_by_username(conn, username) is not None:
                raise AccountError(409, "user_exists", f"A user named {username} already exists.")
        updated = users.update(
            conn, current.id,
            username=username if username is not None else UNSET,
            password_hash=password_hash if password_hash is not None else UNSET,
            role=role if role is not None else UNSET,
            disabled=disabled if disabled is not None else UNSET,
        )
        if disabled_after and not current.disabled:
            web_sessions.delete_for_user(conn, current.id)
        elif password_hash is not None:
            for record in web_sessions.list_for_user(conn, current.id):
                if record.id != keep_web_session:
                    web_sessions.delete(conn, record.id)
    return updated


def delete_user(db: Database, user: users.User) -> None:
    if user.builtin:
        raise AccountError(403, "builtin_user", "The built-in local user cannot be deleted.")
    with db.transaction() as conn:
        current = users.get(conn, user.id)
        if current is None:
            raise AccountError(404, "user_not_found", f"No user {user.username}.")
        _guard_last_admin(conn, current, current.role, current.disabled, deleting=True)
        users.delete(conn, current.id)
