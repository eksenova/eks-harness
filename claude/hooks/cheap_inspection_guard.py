#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, deny, enabled, payload, project_dir, state_file  # noqa: E402

NAV_ACTIONS = {"press", "click", "tap", "fill", "type", "key", "scroll", "navigate", "goto", "open", "back",
               "reload", "dispatch", "arm", "select", "check", "choose", "swipe", "submit", "toggle"}
SHOT_KINDS = {"screenshot", "shot", "video.start", "recording.start"}
CLI_SHOT = re.compile(r"\b(eks-harness|ehx)\s+(?:--\S+\s+)*capture\s+(screenshot|video\s+start)\b")


def main() -> None:
    data = payload()
    if not enabled("cheap_inspection", claude_config(project_dir(data))):
        return
    name = str(data.get("tool_name") or "")
    given = data.get("tool_input") or {}
    session = str(data.get("session_id") or "")
    if name.endswith("__driver_act"):
        if str(given.get("action") or "") in NAV_ACTIONS:
            state_file(session, f"nav-{given.get('sid')}").write_text("1")
        return
    if name.endswith("__driver_observe"):
        state_file(session, f"insp-{given.get('sid')}").write_text("1")
        return
    sid = None
    if name.endswith("__driver_capture") and str(given.get("kind") or "") in SHOT_KINDS:
        sid = given.get("sid")
    elif name == "Bash" and CLI_SHOT.search(str(given.get("command") or "")):
        match = re.search(r"--sid\s+(\S+)", str(given.get("command")))
        sid = match.group(1) if match else None
    if not sid:
        return
    nav, insp = state_file(session, f"nav-{sid}"), state_file(session, f"insp-{sid}")
    if nav.exists() and (not insp.exists() or insp.stat().st_mtime < nav.stat().st_mtime):
        deny("You acted on the app since your last cheap look at it and are about to pay for an image. Run "
             f"driver_observe(sid='{sid}', query='tree') (or 'text'/'url'/'route') first: an empty state, a "
             "permission error or an error toast shows up there for a fraction of the cost. Retry the capture after.")


if __name__ == "__main__":
    main()
