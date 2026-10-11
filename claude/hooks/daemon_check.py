#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, enabled, payload, project_dir, state_file  # noqa: E402

EXIT_DAEMON_DOWN = 3
STATUS_TIMEOUT = 15
INSTALL = 'uv tool install --python 3.13 --link-mode copy "git+https://github.com/eksenova/eks-harness"'


def report(context: str, message: str) -> None:
    print(json.dumps({"systemMessage": message,
                      "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
    sys.exit(0)


def daemon_status(exe: str, cwd: Path) -> tuple[int, dict]:
    try:
        out = subprocess.run([exe, "daemon", "status", "--json"], cwd=cwd, capture_output=True, text=True,
                             timeout=STATUS_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, {}
    except OSError:
        return -2, {}
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        data = {}
    return out.returncode, data if isinstance(data, dict) else {}


def start_through_service(exe: str, cwd: Path) -> None:
    subprocess.Popen([exe, "daemon", "start"], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ))


def why_not_started(service: bool) -> str:
    if service:
        return "Its login service is installed but [claude] daemon_autostart = false, so nothing was started."
    return "It is not installed as a login service, so nothing was started."


def main() -> None:
    data = payload()
    start = project_dir(data)
    config = claude_config(start)
    if not enabled("daemon_check", config):
        return
    marker = state_file(str(data.get("session_id") or ""), "daemon-check")
    if marker.exists():
        return
    marker.write_text("1")
    exe = shutil.which("eks-harness")
    if exe is None:
        report(f"The eks-harness CLI is not installed on this machine, so eks-harness MCP tools and CLI calls will "
               f"fail. If the user wants the harness, tell them to install it ({INSTALL}) and run "
               f"'eks-harness setup'. Do not install it yourself.",
               "eks-harness is not installed; harness tools are unavailable until it is.")
    code, status = daemon_status(exe, start)
    if code == 0 or status.get("running") is True:
        if status.get("authRequired"):
            report(f"The eks-harness daemon runs at {status.get('url')} but this machine has no valid API key: "
                   f"{status.get('message', '')} Ask the user to run 'eks-harness login'.",
                   "eks-harness: log in with 'eks-harness login'.")
        return
    url = status.get("url") or status.get("localUrl") or "its configured URL"
    if code != EXIT_DAEMON_DOWN and status.get("running") is not False:
        report(f"'eks-harness daemon status' did not answer (exit {code}). Harness tools may fail; "
               f"'eks-harness doctor' explains why.",
               "eks-harness: the daemon status check failed; run 'eks-harness doctor'.")
    autostart = config.get("daemon_autostart", True) is not False
    if status.get("service") and autostart:
        start_through_service(exe, start)
        report(f"The eks-harness daemon was not running at {url}. Its login service is installed, so it was asked "
               f"to start ('eks-harness daemon start'); it takes a few seconds. Check with "
               f"'eks-harness daemon status' before harness work.",
               f"eks-harness: starting the daemon through its login service ({url}).")
    report(f"The eks-harness daemon is not running at {url}, so eks-harness MCP tools and CLI calls fail (exit 3) "
           f"until it is. {why_not_started(bool(status.get('service')))} If the user needs the "
           f"harness, tell them to run 'eks-harness daemon start' (or 'eks-harness daemon install' once for a "
           f"service that keeps it running). Do not start it on your own.",
           f"eks-harness daemon is not running ({url}); start it with 'eks-harness daemon start'.")


if __name__ == "__main__":
    main()
