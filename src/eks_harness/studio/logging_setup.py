"""Structured logging setup for the MCP server.

The server writes to stderr (stdout is reserved for the stdio transport).
Format includes the request id where available so log lines correlate with
in-flight MCP requests.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["configure_logging"]


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install a stderr handler with a stable format. Idempotent."""

    root = logging.getLogger()
    if any(getattr(h, "_eks_studio", False) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler._eks_studio = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
