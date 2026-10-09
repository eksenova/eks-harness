"""Roots manager.

The MCP client tells the server which filesystem paths it is willing to expose
(``roots/list``). We track the two well-known root names the plan defines -
``media_library`` and ``project_workspace`` - and offer a single
:func:`is_under_root` guard tools call before touching disk.

The manager is intentionally tolerant: if the client never advertises a
``media_library`` root, tools fall back to requiring fully explicit,
individually-validated paths instead of refusing all requests.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from .workspace import default_workspace_root, ensure_workspace_root

__all__ = [
    "MEDIA_LIBRARY_ROOT",
    "PROJECT_WORKSPACE_ROOT",
    "WELL_KNOWN_ROOTS",
    "RootsManager",
    "get_roots_manager",
    "set_roots_manager",
    "resolve_project_dir",
    "sync_roots_from_session",
    "install_client_roots_sync",
]

_LOG = logging.getLogger(__name__)

MEDIA_LIBRARY_ROOT = "media_library"
PROJECT_WORKSPACE_ROOT = "project_workspace"
WELL_KNOWN_ROOTS = (MEDIA_LIBRARY_ROOT, PROJECT_WORKSPACE_ROOT)


@dataclass
class RootEntry:
    """One filesystem root advertised by the client."""

    name: str
    path: Path


@dataclass
class RootsManager:
    """Keeps the set of currently-granted roots and answers containment checks."""

    entries: dict[str, RootEntry] = field(default_factory=dict)
    _project_workspace_fallback: Path | None = None
    _synced_session_ids: set[int] = field(default_factory=set)

    def register(self, name: str, path: Path) -> None:
        resolved = path.expanduser().resolve()
        self.entries[name] = RootEntry(name=name, path=resolved)
        _LOG.info("registered root %s -> %s", name, resolved)

    def clear(self) -> None:
        self.entries.clear()

    def replace_from_client_roots(self, client_roots: Iterable[tuple[str, str]]) -> None:
        """Re-populate the manager from a sequence of (name, uri) pairs as returned by the client.

        URIs may be ``file://`` URIs or bare local paths. Names that aren't
        well-known are kept as-is so future capabilities can address them.
        """

        self.clear()
        for raw_name, raw_uri in client_roots:
            path = _path_from_root_uri(raw_uri)
            if path is None:
                _LOG.warning("ignoring root %s with unsupported URI %s", raw_name, raw_uri)
                continue
            # MCP `Root.name` is an optional human label, not a semantic key,
            # so it's often absent. Fall back to the path's leaf so unnamed
            # roots still get a stable, non-clobbering entry.
            name = raw_name or path.name or str(path)
            self.register(name, path)

    def project_workspace(self) -> Path:
        """Return the current project workspace root, falling back if needed.

        Order: explicit grant from client → previously-resolved fallback →
        the server's launch directory (``Path.cwd()`` at first use). The
        fallback is created on first use and cached for the rest of the
        process lifetime so the workspace can't shift if cwd changes later.
        """

        entry = self.entries.get(PROJECT_WORKSPACE_ROOT)
        if entry is not None:
            entry.path.mkdir(parents=True, exist_ok=True)
            return entry.path
        # No root explicitly named ``project_workspace`` (the common case -
        # clients rarely use semantic names). If the client granted any root,
        # adopt the first non-``media_library`` one as the workspace.
        adopted = next(
            (e for n, e in self.entries.items() if n != MEDIA_LIBRARY_ROOT),
            None,
        )
        if adopted is not None:
            adopted.path.mkdir(parents=True, exist_ok=True)
            return adopted.path
        if self._project_workspace_fallback is None:
            fallback = ensure_workspace_root(default_workspace_root())
            self._project_workspace_fallback = fallback
            _LOG.debug(
                "no %s root granted; using launch directory %s as workspace",
                PROJECT_WORKSPACE_ROOT,
                fallback,
            )
        return self._project_workspace_fallback

    def media_library(self) -> Path | None:
        entry = self.entries.get(MEDIA_LIBRARY_ROOT)
        return entry.path if entry is not None else None

    def is_under_root(self, path: Path, root_name: str) -> bool:
        """Return True iff ``path`` is contained in the named root.

        Containment is computed via :meth:`Path.resolve` so symlinks are
        followed; this is intentional - granting ``media_library`` to a
        symlinked path means the symlink target is granted as well.
        """

        entry = self.entries.get(root_name)
        if entry is None:
            return False
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        try:
            resolved.relative_to(entry.path)
        except ValueError:
            return False
        return True

    def is_under_any(self, path: Path) -> str | None:
        """Return the name of the first granted root containing ``path``, or None."""

        for name in self.entries:
            if self.is_under_root(path, name):
                return name
        return None


def _path_from_root_uri(uri: str) -> Path | None:
    if not uri:
        return None
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        if path.startswith("/") and len(path) > 3 and path[2] == ":":
            path = path[1:]
        return Path(path)
    return Path(uri)


# --- Process-wide singleton ---------------------------------------------------
#
# The MCP client grants roots to ONE server process. Both the FastMCP tool
# surface and the dev-server WS handlers must observe the same granted-roots
# state - and the launch-directory fallback must be resolved (and logged) only
# once. Creating ``RootsManager()`` ad hoc per handler defeats both: each fresh
# instance has an empty grant set and re-logs the "did not grant" fallback on
# first use. Everything should go through :func:`get_roots_manager`.

_SINGLETON: RootsManager | None = None


def get_roots_manager() -> RootsManager:
    """Return the process-wide :class:`RootsManager`, creating it on first use."""

    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = RootsManager()
    return _SINGLETON


def set_roots_manager(manager: RootsManager) -> None:
    """Install ``manager`` as the process-wide instance (e.g. server startup)."""

    global _SINGLETON
    _SINGLETON = manager


def resolve_project_dir(project_id: str) -> Path:
    """Map a ``project_id`` to its on-disk directory (single source of truth).

    Resolution order:

    1. an absolute ``project_id`` is honoured as-is;
    2. ``<project_workspace>/<project_id>`` when that subdir exists;
    3. the workspace itself when its name matches ``project_id`` - this is the
       case when the dev server is launched from *inside* the project dir, so
       the workspace IS the project and renders live under
       ``<workspace>/renders`` rather than ``<workspace>/<project_id>/renders``;
    4. the same two checks against ``cwd``;
    5. otherwise the (non-existent) ``<workspace>/<project_id>`` candidate, so
       callers can 404 / return an empty list cleanly.

    Every dev-server handler that turns a ``project_id`` into a directory should
    use this so the launched-inside-project layout resolves consistently.
    """

    import os

    raw = Path(project_id)
    if raw.is_absolute():
        return raw.resolve()
    workspace = get_roots_manager().project_workspace()
    candidate = workspace / project_id
    if candidate.is_dir():
        return candidate.resolve()
    if workspace.name == project_id:
        return workspace.resolve()
    cwd = Path(os.getcwd())
    if (cwd / project_id).is_dir():
        return (cwd / project_id).resolve()
    if cwd.name == project_id:
        return cwd.resolve()
    return candidate.resolve()


# --- Client roots ingestion ---------------------------------------------------
#
# The MCP client advertises the filesystem paths it's willing to expose via the
# ``roots/list`` request. The server has to *ask* (``session.list_roots()``);
# the grant isn't pushed. We fetch once per session and feed the result into the
# process-wide manager so a granted workspace is honored instead of the
# launch-directory fallback. Transports without a client (the dev server) have
# no session, so this is a no-op there and the fallback stands.


async def sync_roots_from_session(session: object) -> None:
    """Fetch the client's roots once per session and update the manager.

    Idempotent per session id. Tolerant of clients that don't implement the
    roots capability (``list_roots`` raises) - those just keep the fallback.
    """

    manager = get_roots_manager()
    sid = id(session)
    if sid in manager._synced_session_ids:
        return
    # Mark up-front so concurrent tool calls in the same session don't each
    # issue a redundant round-trip (and so a hard failure isn't retried per call).
    manager._synced_session_ids.add(sid)

    list_roots = getattr(session, "list_roots", None)
    if list_roots is None:
        return
    try:
        result = await list_roots()
    except Exception:  # client lacks the roots capability, or transport error
        _LOG.debug("client exposes no roots capability; keeping fallback", exc_info=True)
        return

    pairs = [(r.name or "", str(r.uri)) for r in (getattr(result, "roots", None) or [])]
    if not pairs:
        return
    manager.replace_from_client_roots(pairs)
    _LOG.info(
        "ingested %d client root(s); project workspace -> %s",
        len(pairs),
        manager.project_workspace(),
    )


def install_client_roots_sync(server: object) -> None:
    """Wrap the low-level ``tools/call`` handler to refresh roots per session.

    ``tools/call`` is the single chokepoint every tool flows through, so this
    is where a once-per-session :func:`sync_roots_from_session` belongs. Must be
    called after all other ``CallToolRequest`` wrappers so the refresh runs
    outermost (before the request is dispatched).
    """

    from mcp.types import CallToolRequest

    low = server._mcp_server  # noqa: SLF001 - required to reach low-level handlers.
    handlers = low.request_handlers
    original = handlers.get(CallToolRequest)
    if original is None:
        _LOG.debug("no CallToolRequest handler registered; skipping roots sync wrapper")
        return

    async def call_tool_with_roots_sync(req: object) -> object:
        try:
            request_context = low.request_context
            session = getattr(request_context, "session", None)
            if session is not None:
                await sync_roots_from_session(session)
        except Exception:  # never let roots sync break a tool call
            _LOG.debug("client roots sync skipped", exc_info=True)
        return await original(req)

    handlers[CallToolRequest] = call_tool_with_roots_sync
