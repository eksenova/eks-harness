from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

TREE_ENV = "EKS_HARNESS_TREE"
TREE_HEADER = "X-Eks-Harness-Tree"
TREE_QUERY = "tree"

REQUEST_TREE: contextvars.ContextVar[str | None] = contextvars.ContextVar("eks_harness_mcp_tree", default=None)
REMOTE: contextvars.ContextVar[bool] = contextvars.ContextVar("eks_harness_mcp_remote", default=False)


@contextmanager
def remote_request(tree: str | None) -> Iterator[None]:
    tree_token = REQUEST_TREE.set(tree)
    remote_token = REMOTE.set(True)
    try:
        yield
    finally:
        REMOTE.reset(remote_token)
        REQUEST_TREE.reset(tree_token)


def working_dir() -> Path | None:
    requested = REQUEST_TREE.get()
    if requested:
        return Path(requested).expanduser()
    if REMOTE.get():
        return None
    configured = os.environ.get(TREE_ENV)
    if configured and Path(configured).expanduser().is_dir():
        return Path(configured).expanduser()
    return Path(os.getcwd())


def resolve_tree(tree: str | None) -> str | None:
    from eks_harness.plugins import find_tree

    base = Path(tree).expanduser() if tree else working_dir()
    if base is None:
        return None
    found = find_tree(base)
    return str(found or base.resolve())


def required_tree(tree: str | None) -> str:
    resolved = resolve_tree(tree)
    if resolved is None:
        raise ToolError(f"Pass tree (the work tree path on the hub machine), or connect with the "
                        f"{TREE_HEADER} header or ?{TREE_QUERY}= on the MCP URL.")
    return resolved


def local_path(path: str) -> Path:
    file_path = Path(path).expanduser()
    if file_path.is_absolute():
        return file_path
    base = working_dir()
    if base is None:
        raise ToolError(f"'{path}' is relative and this MCP connection has no work tree; pass an absolute path.")
    return base / file_path
