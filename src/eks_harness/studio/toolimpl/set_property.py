"""``set_property`` tool.

Mutates a single field in ``project.json`` addressed by a JSONPath-like
dotted/indexed path, then re-validates the whole project before rewriting.

Path syntax (deliberately small - no quoting, no wildcards, no slicing):

    tracks[0].segments[1].effects[0].amount
    audio_tracks[0].gain_db
    render_settings.preset
    metadata.author

Tokens:
    * ``name``                  - dict key
    * ``name[index]``           - dict key followed by one or more numeric
                                  index lookups (e.g. ``tracks[0][1]``)

Whitespace inside the path is rejected. Indices must be non-negative
integers. The terminal segment may either set an existing key/index or
extend a list by one (``effects[3]`` on a length-3 list appends).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from eks_harness.video.ir import Project

from ..roots import RootsManager
from ._project_io import load_project_json, resolve_project_path

__all__ = ["DESCRIPTION", "run"]

_LOG = logging.getLogger(__name__)


def _run(
    *,
    project_path: Path,
    path: str,
    value: Any,
    roots: RootsManager,
) -> dict[str, Any]:
    resolved = resolve_project_path(project_path, roots)
    json_path, raw = load_project_json(resolved)

    tokens = _parse_path(path)
    if not tokens:
        raise ValueError("path must not be empty")

    _apply_set(raw, tokens, value, original_path=path)

    try:
        validated = Project.model_validate(raw, by_alias=True)
    except ValidationError as exc:
        _LOG.warning("set_property %s rejected: %s", path, exc.errors())
        raise

    json_path.write_text(
        validated.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "updated_path": str(json_path),
        "path": path,
    }


# --- Path parser -------------------------------------------------------------

_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|(\.)|(.)")


def _parse_path(path: str) -> list[str | int]:
    """Turn ``"a.b[0].c[1][2]"`` into ``["a", "b", 0, "c", 1, 2]``."""

    if any(ch.isspace() for ch in path):
        raise ValueError(f"path may not contain whitespace: {path!r}")

    tokens: list[str | int] = []
    expect_separator = False
    pos = 0
    while pos < len(path):
        match = _TOKEN_RE.match(path, pos)
        if match is None:
            raise ValueError(f"unexpected character at {pos} in path {path!r}")
        name, index, dot, bad = match.group(1, 2, 3, 4)
        if bad is not None:
            raise ValueError(f"unexpected character {bad!r} at {pos} in path {path!r}")
        if name is not None:
            if expect_separator:
                raise ValueError(
                    f"expected '.' or '[N]' before name {name!r} at {pos} in path {path!r}"
                )
            tokens.append(name)
            expect_separator = True
        elif index is not None:
            tokens.append(int(index))
            expect_separator = True
        elif dot is not None:
            if not expect_separator:
                raise ValueError(f"unexpected '.' at {pos} in path {path!r}")
            expect_separator = False
        pos = match.end()
    if not expect_separator:
        raise ValueError(f"path ends mid-token: {path!r}")
    return tokens


# --- Mutator -----------------------------------------------------------------


def _apply_set(
    container: Any,
    tokens: list[str | int],
    value: Any,
    *,
    original_path: str,
) -> None:
    cursor: Any = container
    for depth, token in enumerate(tokens[:-1]):
        cursor = _step(cursor, token, original_path, prefix_tokens=tokens[: depth + 1])
    leaf_token = tokens[-1]
    _assign(cursor, leaf_token, value, original_path)


def _step(container: Any, token: str | int, original_path: str, *, prefix_tokens: list[str | int]) -> Any:
    if isinstance(token, int):
        if not isinstance(container, list):
            raise ValueError(
                f"path {original_path!r}: expected list at {_format_prefix(prefix_tokens[:-1])} "
                f"to index [{token}], got {type(container).__name__}"
            )
        if token < 0 or token >= len(container):
            raise IndexError(
                f"path {original_path!r}: index {token} out of range for list of length "
                f"{len(container)} at {_format_prefix(prefix_tokens[:-1])}"
            )
        return container[token]
    if not isinstance(container, dict):
        raise ValueError(
            f"path {original_path!r}: expected dict at {_format_prefix(prefix_tokens[:-1])} "
            f"to access {token!r}, got {type(container).__name__}"
        )
    if token not in container:
        raise KeyError(
            f"path {original_path!r}: key {token!r} not found at "
            f"{_format_prefix(prefix_tokens[:-1])}"
        )
    return container[token]


def _assign(container: Any, token: str | int, value: Any, original_path: str) -> None:
    if isinstance(token, int):
        if not isinstance(container, list):
            raise ValueError(
                f"path {original_path!r}: cannot assign index [{token}] on "
                f"{type(container).__name__}"
            )
        if token == len(container):
            container.append(value)
            return
        if token < 0 or token > len(container):
            raise IndexError(
                f"path {original_path!r}: index {token} out of range for list of length "
                f"{len(container)}"
            )
        container[token] = value
        return
    if not isinstance(container, dict):
        raise ValueError(
            f"path {original_path!r}: cannot assign key {token!r} on "
            f"{type(container).__name__}"
        )
    container[token] = value


def _format_prefix(tokens: list[str | int]) -> str:
    if not tokens:
        return "<root>"
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            if parts:
                parts.append(f".{token}")
            else:
                parts.append(token)
    return "".join(parts)

DESCRIPTION = "Set a single field in project.json addressed by a dotted/indexed path (e.g. 'tracks[0].segments[1].effects[0].amount'). Re-validates the whole project; aborts with the failing path if validation rejects the new value."
run = _run
