from __future__ import annotations

import re
import secrets
import time
import unicodedata

TURKISH_FOLD = str.maketrans("çğıöşüâîûÇĞİÖŞÜÂÎÛ", "cgiosuaiuCGIOSUAIU")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SID_ALPHABET = "23456789abcdefghjkmnpqrstvwxyz"
SID_LENGTH = 6
SID_PATTERN = re.compile(rf"^[{SID_ALPHABET}]{{{SID_LENGTH}}}$")
ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PROJECT_PART_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SHARE_TOKEN_LENGTH = 32
SHARE_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SHARE_TOKEN_PATTERN = re.compile(rf"^[A-Za-z0-9]{{{SHARE_TOKEN_LENGTH}}}$")


def new_ulid(timestamp: float | None = None) -> str:
    millis = int((time.time() if timestamp is None else timestamp) * 1000) & ((1 << 48) - 1)
    value = (millis << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def ulid_timestamp(ulid: str) -> float:
    value = 0
    for char in ulid.upper():
        value = (value << 5) | CROCKFORD.index(char)
    return (value >> 80) / 1000.0


def is_ulid(value: str) -> bool:
    return bool(ULID_PATTERN.match(value or ""))


def new_sid() -> str:
    return "".join(secrets.choice(SID_ALPHABET) for _ in range(SID_LENGTH))


def is_sid(value: str) -> bool:
    return bool(SID_PATTERN.match(value or ""))


def new_share_token() -> str:
    return "".join(secrets.choice(SHARE_TOKEN_ALPHABET) for _ in range(SHARE_TOKEN_LENGTH))


def is_share_token(value: str) -> bool:
    return bool(SHARE_TOKEN_PATTERN.match(value or ""))


def new_lease_id() -> str:
    return secrets.token_hex(6)


def fold_ascii(text: str) -> str:
    folded = (text or "").translate(TURKISH_FOLD)
    return unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")


def slugify(name: str) -> str:
    text = fold_ascii(name).strip().lower()
    text = re.sub(r"[/\\\s]+", "-", text)
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return text[:100] or "session"


def is_project_part(value: str) -> bool:
    return bool(PROJECT_PART_PATTERN.match(value or ""))


def parse_project_id(project_id: str) -> tuple[str, str]:
    owner, sep, name = (project_id or "").partition("/")
    if not sep or not is_project_part(owner) or not is_project_part(name):
        raise ValueError(
            f"invalid project id '{project_id}': expected owner/name, each part matching [a-z0-9][a-z0-9._-]{{0,62}}")
    return owner, name


def project_id(owner: str, name: str) -> str:
    return "/".join(parse_project_id(f"{owner}/{name}"))
