from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import HTTPConnection

from eks_harness.api.errors import forbidden
from eks_harness.db import Database
from eks_harness.db.common import now
from eks_harness.db.repos import users, web_sessions

COOKIE_NAME = "eks_harness_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_TOUCH_INTERVAL = 60.0

_hasher = PasswordHasher()


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    via: str
    key_id: int | None = None
    key_prefix: str | None = None
    web_session_id: str | None = None
    csrf: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def actor(self) -> str:
        return self.username


def local_principal(db: Database) -> Principal:
    user = users.local_user(db.conn())
    return Principal(user_id=user.id, username=user.username, role="admin", via="local")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_web_session(db: Database, user_id: int, hours: int, ip: str | None = None,
                       user_agent: str | None = None) -> tuple[str, web_sessions.WebSession]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    with db.transaction() as conn:
        record = web_sessions.create(conn, sha256_hex(token), user_id, csrf, now() + hours * 3600, ip, user_agent)
    return token, record


def end_web_session(db: Database, token_or_id: str) -> bool:
    conn = db.conn()
    return web_sessions.delete(conn, sha256_hex(token_or_id)) or web_sessions.delete(conn, token_or_id)


def verify_cookie(db: Database, request: HTTPConnection) -> Principal | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    conn = db.conn()
    record = web_sessions.get(conn, sha256_hex(token))
    if record is None:
        return None
    stamp = now()
    if record.expired(stamp):
        web_sessions.delete(conn, record.id)
        return None
    user = users.get(conn, record.user_id)
    if user is None or user.disabled or user.builtin:
        return None
    if record.last_seen_at is None or stamp - record.last_seen_at > SESSION_TOUCH_INTERVAL:
        web_sessions.touch(conn, record.id, stamp)
    return Principal(user_id=user.id, username=user.username, role=user.role, via="cookie",
                     web_session_id=record.id, csrf=record.csrf)


def check_csrf(request: HTTPConnection, principal: Principal) -> None:
    if principal.via != "cookie":
        return
    method = getattr(request, "method", "GET").upper()
    if method in SAFE_METHODS:
        return
    supplied = request.headers.get(CSRF_HEADER) or ""
    if not principal.csrf or not supplied or not hmac.compare_digest(supplied, principal.csrf):
        raise forbidden("Missing or wrong CSRF token: send the X-CSRF-Token header from /api/auth/me.",
                        error="csrf_failed")
