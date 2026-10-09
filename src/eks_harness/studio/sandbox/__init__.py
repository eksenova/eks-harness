"""Subprocess sandbox for executing user / Claude-authored ``project.py`` files.

The sandbox spawns a fresh Python interpreter, hands the source on stdin,
applies platform-specific resource limits and (best-effort) network gating,
collects a pickled :class:`eks_harness.video.ir.Project` from a side channel, and kills
the process tree on timeout. See :mod:`eks_harness.studio.sandbox.runner` for the
public entry point.
"""

from __future__ import annotations

from .runner import SandboxResult, SandboxTimeout, run_project_py

__all__ = ["SandboxResult", "SandboxTimeout", "run_project_py"]
