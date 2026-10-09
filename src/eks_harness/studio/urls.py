from __future__ import annotations

_PREFIX = ""


def set_prefix(prefix: str) -> None:
    global _PREFIX
    _PREFIX = "/" + prefix.strip("/") if prefix.strip("/") else ""


def prefix() -> str:
    return _PREFIX


def url(path: str) -> str:
    return _PREFIX + path
