from __future__ import annotations

import re
from pathlib import Path

QUOTE = re.compile(r"^`(?P<path>examples/[^`]+)`:\n\n```(?P<lang>[a-z]*)\n(?P<body>.*?)\n```$", re.MULTILINE | re.DOTALL)


def synced(text: str, root: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        source = root / match.group("path")
        if not source.is_file():
            return match.group(0)
        body = source.read_text(encoding="utf-8").rstrip("\n")
        return f"`{match.group('path')}`:\n\n```{match.group('lang')}\n{body}\n```"

    return QUOTE.sub(replace, text)


def stale_quotes(text: str, root: Path) -> list[str]:
    stale: list[str] = []
    for match in QUOTE.finditer(text):
        source = root / match.group("path")
        if not source.is_file():
            stale.append(f"{match.group('path')} (missing)")
        elif source.read_text(encoding="utf-8").rstrip("\n") != match.group("body"):
            stale.append(match.group("path"))
    return stale


def sync_file(doc: Path, root: Path) -> bool:
    text = doc.read_text(encoding="utf-8")
    updated = synced(text, root)
    if updated != text:
        doc.write_text(updated, encoding="utf-8")
        return True
    return False


__all__ = ["stale_quotes", "sync_file", "synced"]
