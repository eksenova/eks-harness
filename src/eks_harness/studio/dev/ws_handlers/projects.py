"""``projects.list`` handler - autocomplete source for the Topbar.

Aggregates candidate project directories from every source the dev server
can reach without explicit MCP-client cooperation:

1. The default :class:`RootsManager` workspace (which falls back to the
   server's launch cwd when no client root was granted - see ``roots.py``).
2. The current process cwd.
3. The parent of the cwd (lets users keep the dev server inside a single
   project dir while still discovering siblings under the parent).

Each root is scanned shallowly (root + immediate children) for a
``project.py`` file. Matches are returned newest-mtime-first so the
dropdown surfaces recently-touched projects at the top.

The request carries an optional ``query`` string. Substring match against
``id`` and ``path`` runs server-side so the wire payload stays small.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from ...roots import get_roots_manager
from ..ws_hub import WSHub
from ..ws_protocol import (
    ErrorEnvelope,
    ProjectListItem,
    ProjectsListRequest,
    ReplyEnvelope,
)

__all__ = ["register"]

_LOG = logging.getLogger(__name__)

# Cap per-scan to keep the response bounded on huge workspaces. 256 entries
# is more than any human keeps in front of them at once.
_MAX_RESULTS = 256


def _candidate_roots() -> list[Path]:
    """Best-effort collection of every directory that could contain projects."""

    roots: list[Path] = []

    # 1) RootsManager workspace (client-granted ``project_workspace`` or
    #    the cached launch-cwd fallback).
    try:
        ws = get_roots_manager().project_workspace()
        if ws.is_dir():
            roots.append(ws)
    except Exception:
        _LOG.debug("project_workspace probe failed", exc_info=True)

    # 2) Process cwd - useful when the dev server was launched from inside
    #    a multi-project repo without a workspace-root grant.
    try:
        cwd = Path(os.getcwd()).resolve()
        roots.append(cwd)
    except Exception:
        _LOG.debug("cwd probe failed", exc_info=True)

    # 3) The cwd's parent - covers the common "I launched the server from
    #    inside a single project; the siblings should still appear" case.
    try:
        parent = Path(os.getcwd()).resolve().parent
        roots.append(parent)
    except Exception:
        _LOG.debug("parent probe failed", exc_info=True)

    # Dedup while preserving order; the workspace stays first.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for r in roots:
        try:
            key = r.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _project_candidates(roots: Iterable[Path]) -> list[Path]:
    """Walk each root one level deep; surface directories containing ``project.py``."""

    found: list[Path] = []
    for root in roots:
        if (root / "project.py").is_file():
            found.append(root)
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if (child / "project.py").is_file():
                    found.append(child)
        except (PermissionError, OSError) as exc:
            _LOG.debug("could not list %s: %s", root, exc)
            continue
        if len(found) >= _MAX_RESULTS:
            break
    return found


def _to_item(path: Path) -> ProjectListItem:
    return ProjectListItem(id=path.name, name=path.name, path=str(path))


def _matches(item: ProjectListItem, query: str) -> bool:
    if not query:
        return True
    q = query.casefold()
    return q in item.id.casefold() or q in item.path.casefold()


def register(register_handler: Any) -> None:
    async def handle_projects_list(
        request: ProjectsListRequest, *, hub: WSHub, client_id: str
    ) -> ReplyEnvelope | ErrorEnvelope:
        del hub, client_id  # request/reply only.

        roots = _candidate_roots()
        candidates = _project_candidates(roots)

        # Dedup by resolved path while preserving order.
        seen: set[Path] = set()
        unique: list[Path] = []
        for p in candidates:
            try:
                key = p.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)

        # Order by mtime of project.py descending; fall back to natural order
        # if any stat fails.
        try:
            unique.sort(key=lambda p: -(p / "project.py").stat().st_mtime)
        except OSError:
            pass

        items = [_to_item(p) for p in unique[:_MAX_RESULTS]]
        if request.payload.query:
            items = [i for i in items if _matches(i, request.payload.query)]

        return ReplyEnvelope(
            id=request.id,
            type="projects.list.reply",
            result={"projects": [i.model_dump() for i in items]},
        )

    register_handler("projects.list", handle_projects_list)
