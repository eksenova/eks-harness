#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    claude_config,
    context,
    enabled,
    explicitly_enabled,
    payload,
    project_dir,
    state_file,
)

DEFAULT_PATHS = ("web/src/", "sdk/ui/", "/ui/src/")


def design_paths(config: dict) -> list[str]:
    configured = config.get("ui_design_paths")
    if isinstance(configured, list):
        return [str(p) for p in configured if str(p)]
    return list(DEFAULT_PATHS) if explicitly_enabled("design_hint", config) else []


def main() -> None:
    data = payload()
    config = claude_config(project_dir(data))
    if not enabled("design_hint", config):
        return
    given = data.get("tool_input") or {}
    path = str(given.get("file_path") or given.get("notebook_path") or "")
    if not path:
        return
    paths = design_paths(config)
    if not any(p in path for p in paths):
        return
    marker = state_file(str(data.get("session_id") or ""), "design-hint")
    if marker.exists():
        return
    marker.write_text("1")
    skill = str(config.get("ui_design_skill") or "eks-harness:eks-harness-ui-design")
    context("PreToolUse", f"This file is UI governed by a design system. Load the {skill} skill before editing it, "
                          f"and review the result with screenshots as that skill describes.")


if __name__ == "__main__":
    main()
