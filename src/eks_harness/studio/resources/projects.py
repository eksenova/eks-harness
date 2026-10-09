"""``eks-harness://video/projects/{id}`` resources.

Reads ``project.json`` (snapshot) and ``project.py`` (canonical source) from
the project workspace. Subscriptions are honoured by the MCP framework - we
keep a small mtime poller so updates trigger ``send_resource_updated`` on
subscribers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from ._spec import ResourceSpec

from ..roots import RootsManager, get_roots_manager

__all__ = ["RESOURCES", "read_project_snapshot", "read_project_source", "watch_projects"]

_LOG = logging.getLogger(__name__)


def read_project_snapshot(project_id: str, *, roots: RootsManager | None = None) -> str:
    snapshot = (roots or get_roots_manager()).project_workspace() / project_id / "project.json"
    if not snapshot.exists():
        raise FileNotFoundError(f"no project.json under {snapshot.parent}")
    return snapshot.read_text(encoding="utf-8")


def read_project_source(project_id: str, *, roots: RootsManager | None = None) -> str:
    source = (roots or get_roots_manager()).project_workspace() / project_id / "project.py"
    if not source.exists():
        raise FileNotFoundError(f"no project.py under {source.parent}")
    return source.read_text(encoding="utf-8")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/projects/{project_id}', name='video_project_snapshot', title='Project snapshot',
                 description='JSON snapshot of the canonical project IR of the named project.',
                 mime_type='application/json', read=read_project_snapshot, needs_roots=True, needs_registry=False),
    ResourceSpec(uri='eks-harness://video/projects/{project_id}/source.py', name='video_project_source', title='Project source',
                 description='Python source of the named project.',
                 mime_type='text/x-python', read=read_project_source, needs_roots=True, needs_registry=False),
]


async def watch_projects(notify: Callable[[str], Awaitable[None]], *, roots: RootsManager,
                         interval_s: float = 1.0) -> None:
    """Optional background poller emitting resource_updated when project files change.

    Started by the server when run as a long-running process; tests can omit
    it. Tracks per-file mtimes and calls ``notify(uri)`` for either
    ``eks-harness://video/projects/<id>`` or ``.../source.py`` as appropriate.
    """

    seen: dict[str, float] = {}
    while True:
        workspace = roots.project_workspace()
        for project_dir in workspace.glob("*"):
            if not project_dir.is_dir():
                continue
            for filename, suffix in (("project.json", ""), ("project.py", "/source.py")):
                target = project_dir / filename
                if not target.exists():
                    continue
                key = f"{project_dir.name}{suffix}"
                mtime = target.stat().st_mtime
                if seen.get(key) == mtime:
                    continue
                seen[key] = mtime
                uri = f"eks-harness://video/projects/{project_dir.name}{suffix}"
                try:
                    await notify(uri)
                except Exception:
                    _LOG.debug("resource_updated dispatch failed for %s", uri, exc_info=True)
        await asyncio.sleep(interval_s)
