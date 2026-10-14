#!/usr/bin/env python3
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, deny, enabled, payload, project_dir, state_file  # noqa: E402

ACTION_OPS = {"ui.nav", "ui.back", "ui.reload", "ui.open_url", "ui.deeplink", "ui.tap", "ui.double_tap",
              "ui.long_press", "ui.hover", "ui.fill", "ui.type", "ui.clear", "ui.key", "ui.select", "ui.check",
              "ui.choose", "ui.scroll", "ui.swipe", "ui.drag", "web.wheel", "web.viewport", "mobile.dispatch",
              "mobile.invoke", "mobile.toggle", "mobile.submit", "mobile.logout", "mobile.patch", "mobile.mount",
              "fake.arm", "session.as", "inject.script", "inject.eval"}
LOOK_OPS = {"ui.tree", "ui.text", "ui.value", "ui.url", "ui.route", "ui.exists", "ui.visible", "ui.count",
            "ui.state", "ui.layout", "ui.attr", "web.logs", "mobile.state", "mobile.route_names", "capture.dump",
            "expect.target"}
SHOT_OPS = {"capture.shot"}
CLI_DO = re.compile(r"\b(?:eks-harness|ehx)\s+(?:--\S+\s+)*runs\s+control\s+(\S+)\s+do\b")


def session_op(name: str, given: dict) -> tuple[str, str] | None:
    if name.endswith("__eks_session_do"):
        return str(given.get("session") or ""), str(given.get("op") or "")
    if name.endswith("__eks_run_control") and str(given.get("action") or "") == "do":
        return str(given.get("run_id") or ""), str(given.get("op") or "")
    if name == "Bash":
        command = str(given.get("command") or "")
        match = CLI_DO.search(command)
        if not match:
            return None
        try:
            words = shlex.split(command[match.end():])
        except ValueError:
            words = command[match.end():].split()
        op = next((words[i + 1] for i, word in enumerate(words[:-1]) if word == "--op"), "")
        return match.group(1), op
    return None


def main() -> None:
    data = payload()
    if not enabled("cheap_inspection", claude_config(project_dir(data))):
        return
    found = session_op(str(data.get("tool_name") or ""), data.get("tool_input") or {})
    if found is None:
        return
    session, op = found
    if not session or not op:
        return
    key = re.sub(r"[^A-Za-z0-9_-]", "_", session)[:64]
    acted = state_file(str(data.get("session_id") or ""), f"act-{key}")
    looked = state_file(str(data.get("session_id") or ""), f"look-{key}")
    if op in ACTION_OPS:
        acted.write_text("1")
        return
    if op in LOOK_OPS:
        looked.write_text("1")
        return
    if op not in SHOT_OPS:
        return
    if acted.exists() and (not looked.exists() or looked.stat().st_mtime < acted.stat().st_mtime):
        deny("You acted on the app since your last cheap look at it and are about to pay for an image. Run "
             f"ui.tree (or ui.text, ui.url, ui.route, web.logs) in session {session} first: an empty state, a "
             "permission error or an error toast shows up there for a fraction of the cost. Retry the shot after.")


if __name__ == "__main__":
    main()
