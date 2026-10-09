#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, enabled, payload, project_dir, state_file  # noqa: E402

WORKTREE_ADD = re.compile(r"\bgit\s+worktree\s+add\b(?:\s+-\S+(?:\s+\S+)?)*\s+(\S+)")


def target_tree(data: dict) -> Path | None:
    name = data.get("tool_name")
    given = data.get("tool_input") or {}
    if name == "EnterWorktree":
        response = data.get("tool_response") or {}
        path = given.get("path") or (response.get("worktreePath") if isinstance(response, dict) else None)
        return Path(path) if path else None
    if name == "Bash":
        match = WORKTREE_ADD.search(str(given.get("command") or ""))
        if match:
            path = Path(match.group(1))
            return path if path.is_absolute() else project_dir(data) / path
    return None


def main() -> None:
    data = payload()
    tree = target_tree(data)
    if tree is None or not tree.is_dir():
        return
    config = claude_config(tree)
    if not enabled("prewarm", config):
        return
    exe = shutil.which("eks-harness")
    if exe is None:
        return
    marker = state_file(str(data.get("session_id") or ""), f"prewarm-{abs(hash(str(tree)))}")
    if marker.exists():
        return
    marker.write_text("1")
    env = dict(os.environ)
    if data.get("session_id"):
        env["CLAUDE_CODE_SESSION_ID"] = str(data["session_id"])
    for platform in config.get("prewarm") or ["web"]:
        subprocess.Popen([exe, "flow", "run", "-e", "result = 'warm'", "--platform", str(platform), "--no-fetch"],
                         cwd=str(tree), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"eks-harness: warming {', '.join(map(str, config.get('prewarm') or ['web']))} for {tree} in the background")


if __name__ == "__main__":
    main()
