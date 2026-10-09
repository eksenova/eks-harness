"""Distinct exit codes for the render driver.

A single source of truth shared by the SDK (which raises domain errors the
driver maps to codes) and the MCP layer (which interprets the code returned
by the renderer subprocess). Each code has a stable category string so the
JSON event stream can carry the structured failure class to callers without
relying on numeric comparison.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["ExitCode", "category_for"]


class ExitCode(IntEnum):
    """Render driver exit codes.

    Numeric values are stable; callers may match either by name or by
    ``int(code)``. Use :func:`category_for` to obtain the string slug that
    appears in JSON ``error`` events.
    """

    SUCCESS = 0
    UNKNOWN = 1
    PROJECT_LOAD_ERROR = 2
    NOT_IMPLEMENTED = 3
    VALIDATION_ERROR = 4
    MARKER_EXTRACTION_ERROR = 5
    RENDER_PIPELINE_ERROR = 6
    MUX_ERROR = 7
    CANCELLED = 130


_CATEGORIES: dict[ExitCode, str] = {
    ExitCode.SUCCESS: "success",
    ExitCode.UNKNOWN: "unknown",
    ExitCode.PROJECT_LOAD_ERROR: "project_load_error",
    ExitCode.NOT_IMPLEMENTED: "not_implemented",
    ExitCode.VALIDATION_ERROR: "validation_error",
    ExitCode.MARKER_EXTRACTION_ERROR: "marker_extraction_error",
    ExitCode.RENDER_PIPELINE_ERROR: "render_pipeline_error",
    ExitCode.MUX_ERROR: "mux_error",
    ExitCode.CANCELLED: "cancelled",
}


def category_for(code: ExitCode | int) -> str:
    """Return the stable category slug for ``code``.

    Unknown numeric codes fall back to ``"unknown"`` rather than raising so
    consumers can log defensively without crashing.
    """

    try:
        return _CATEGORIES[ExitCode(int(code))]
    except (ValueError, KeyError):
        return "unknown"
