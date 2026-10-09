#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, deny, enabled, payload, project_dir, tree_of  # noqa: E402

DEFAULT_MARKERS = ("/raw/", "/s/", "NO-UI-EVIDENCE")


def run(args: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def base_ref(tree: Path, configured: str | None) -> str:
    for ref in [configured, "origin/HEAD", "origin/main", "origin/master", "origin/development"]:
        if ref and run(["git", "rev-parse", "--verify", "--quiet", ref], tree):
            return ref
    return "HEAD~1"


def main() -> None:
    data = payload()
    start = project_dir(data)
    config = claude_config(start)
    if not enabled("pr_evidence", config):
        return
    if data.get("tool_name") != "Bash":
        return
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not re.search(r"\bgh\s+pr\s+(create|edit)\b", command):
        return
    if re.search(r"\bgh\s+pr\s+edit\b", command) and "--body" not in command:
        return
    ui_paths = [str(p) for p in config.get("ui_paths") or []]
    if not ui_paths:
        return
    tree = tree_of(start)
    base = base_ref(tree, config.get("base_ref"))
    merge_base = run(["git", "merge-base", base, "HEAD"], tree) or base
    changed = run(["git", "diff", "--name-only", f"{merge_base}...HEAD"], tree).splitlines()
    touched = sorted({p for p in ui_paths if any(f.startswith(p) for f in changed)})
    if not touched:
        return
    markers = [str(m) for m in config.get("evidence_markers") or DEFAULT_MARKERS]
    body = command
    file_match = re.search(r"--body-file\s+(\S+)", command)
    if file_match:
        try:
            body += Path(file_match.group(1)).read_text(encoding="utf-8")
        except OSError:
            pass
    if any(marker in body for marker in markers):
        return
    deny("This branch changes UI (" + ", ".join(touched) + ") and the PR body carries no UI evidence. Capture "
         "screenshots (and a video for animated changes) with a flow (eks-harness flow run ...), create share links "
         "(eks-harness share create <artifact-id>) and paste them into the body. For a change with no visible "
         "effect put NO-UI-EVIDENCE and one line of why in the body.")


if __name__ == "__main__":
    main()
