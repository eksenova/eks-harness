from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass

from starlette.requests import HTTPConnection

from eks_harness.auth.core import Principal, sha256_hex
from eks_harness.auth.users import AccountError
from eks_harness.db import Database
from eks_harness.db.common import now
from eks_harness.db.repos import api_keys, users

API_KEY_PATTERN = re.compile(r"^ehk_([A-Za-z0-9]{8})_([A-Za-z0-9]{32})$")
KEY_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
KEY_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
KEY_TOUCH_INTERVAL = 60.0


def _random(length: int) -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class NewApiKey:
    raw: str
    prefix: str
    secret_sha256: str


def generate_api_key(db: Database | None = None) -> NewApiKey:
    while True:
        prefix, secret = _random(8), _random(32)
        if db is None or not api_keys.prefix_exists(db.conn(), prefix):
            return NewApiKey(raw=f"ehk_{prefix}_{secret}", prefix=prefix, secret_sha256=sha256_hex(secret))


def create_api_key(db: Database, user_id: int, name: str = "") -> tuple[str, api_keys.ApiKey]:
    with db.transaction() as conn:
        key = generate_api_key(db)
        record = api_keys.insert(conn, user_id, name, key.prefix, key.secret_sha256)
    return key.raw, record


def parse_api_key(raw: str) -> tuple[str, str] | None:
    match = API_KEY_PATTERN.match((raw or "").strip())
    return (match.group(1), match.group(2)) if match else None


def verify_api_key(db: Database, raw: str) -> Principal | None:
    parsed = parse_api_key(raw)
    if parsed is None:
        return None
    prefix, secret = parsed
    conn = db.conn()
    record = api_keys.get_by_prefix(conn, prefix)
    if record is None or record.revoked_at is not None:
        return None
    if not hmac.compare_digest(record.secret_sha256, sha256_hex(secret)):
        return None
    user = users.get(conn, record.user_id)
    if user is None or user.disabled or user.builtin:
        return None
    stamp = now()
    if record.last_used_at is None or stamp - record.last_used_at > KEY_TOUCH_INTERVAL:
        api_keys.touch(conn, record.id, stamp)
    return Principal(user_id=user.id, username=user.username, role=user.role, via="key",
                     key_id=record.id, key_prefix=record.prefix)


def bearer_token(connection: HTTPConnection) -> str | None:
    header = connection.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def create_key(db: Database, user: users.User, name: str = "") -> tuple[str, api_keys.ApiKey]:
    if user.builtin:
        raise AccountError(400, "builtin_user",
                           "The built-in local user cannot hold API keys; create a real user first.")
    if user.disabled:
        raise AccountError(400, "user_disabled", f"{user.username} is disabled; enable the user first.")
    return create_api_key(db, user.id, (name or "").strip()[:100])


def find_key(conn: sqlite3.Connection, ref: str) -> api_keys.ApiKey:
    text = (ref or "").strip()
    parsed = parse_api_key(text)
    if parsed is not None:
        text = parsed[0]
    elif text.startswith("ehk_"):
        text = text[4:].split("_", 1)[0]
    record = None
    if KEY_PREFIX_PATTERN.match(text):
        record = api_keys.get_by_prefix(conn, text)
    if record is None and text.isdigit():
        record = api_keys.get(conn, int(text))
    if record is None:
        raise AccountError(404, "key_not_found", f"No API key {ref}.")
    return record
