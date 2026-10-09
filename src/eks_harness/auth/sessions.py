from __future__ import annotations

import threading

from starlette.requests import HTTPConnection
from starlette.responses import Response

from eks_harness.auth import core, keys, proxies
from eks_harness.config import Config
from eks_harness.db import Database
from eks_harness.db.repos import users

_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = core.hash_password("eks-harness timing equaliser")
        return _dummy_hash


def check_password_login(db: Database, username: str, password: str) -> users.User | None:
    conn = db.conn()
    user = users.get_by_username(conn, (username or "").strip())
    if user is None or not user.password_hash or user.builtin:
        core.verify_password(_dummy(), password or "")
        return None
    if not core.verify_password(user.password_hash, password or ""):
        return None
    if user.disabled:
        return None
    if core.password_needs_rehash(user.password_hash):
        users.update(conn, user.id, password_hash=core.hash_password(password))
    return user


def check_key_login(db: Database, raw: str) -> users.User | None:
    principal = keys.verify_api_key(db, raw)
    if principal is None:
        return None
    return users.get(db.conn(), principal.user_id)


def cookie_secure(config: Config, connection: HTTPConnection | None = None) -> bool:
    if config["auth.cookieSecure"]:
        return True
    if str(config["server.publicUrl"] or "").lower().startswith("https://"):
        return True
    if connection is not None and proxies.client_scheme(connection, config) == "https":
        return True
    return False


def set_session_cookie(response: Response, token: str, config: Config, connection: HTTPConnection | None,
                       max_age: int) -> None:
    response.set_cookie(core.COOKIE_NAME, token, max_age=max_age, path="/", httponly=True, samesite="strict",
                        secure=cookie_secure(config, connection))


def clear_session_cookie(response: Response, config: Config, connection: HTTPConnection | None) -> None:
    response.delete_cookie(core.COOKIE_NAME, path="/", httponly=True, samesite="strict",
                           secure=cookie_secure(config, connection))
