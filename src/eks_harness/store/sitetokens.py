from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from eks_harness.db import Database
from eks_harness.db.repos import kv

SECRET_KEY = "store.siteTokenSecret"
PREFIX = "~t."
LIFETIME_SECONDS = 30 * 60
_TOKEN = re.compile(r"^~t\.(\d{1,12})\.(\d{1,12})\.([A-Za-z0-9_-]{16,64})$")


@dataclass(frozen=True)
class SiteGrant:
    user_id: int
    expires_at: int


def _secret(db: Database) -> bytes:
    conn = db.conn()
    value = kv.get(conn, SECRET_KEY)
    if not value:
        with db.transaction() as tx:
            value = kv.get(tx, SECRET_KEY)
            if not value:
                value = secrets.token_hex(32)
                kv.put(tx, SECRET_KEY, value)
    return bytes.fromhex(value)


def _signature(secret: bytes, artifact_id: str, user_id: int, expires_at: int) -> str:
    digest = hmac.new(secret, f"{artifact_id}.{user_id}.{expires_at}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode().rstrip("=")


def issue(db: Database, artifact_id: str, user_id: int, lifetime: int = LIFETIME_SECONDS) -> str:
    expires_at = int(time.time()) + lifetime
    return f"{PREFIX}{user_id}.{expires_at}.{_signature(_secret(db), artifact_id, user_id, expires_at)}"


def is_token_segment(segment: str) -> bool:
    return segment.startswith(PREFIX)


def verify(db: Database, artifact_id: str, segment: str) -> SiteGrant | None:
    match = _TOKEN.match(segment or "")
    if not match:
        return None
    user_id, expires_at, signature = int(match.group(1)), int(match.group(2)), match.group(3)
    if expires_at < time.time():
        return None
    expected = _signature(_secret(db), artifact_id, user_id, expires_at)
    if not hmac.compare_digest(expected, signature):
        return None
    return SiteGrant(user_id=user_id, expires_at=expires_at)
