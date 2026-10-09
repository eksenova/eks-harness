#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, enabled, payload, project_dir  # noqa: E402


def main() -> None:
    data = payload()
    config = claude_config(project_dir(data))
    if not enabled("agent_stop", config):
        return
    agent = str(data.get("agent_type") or data.get("subagent_type") or "")
    names = [str(n) for n in config.get("harness_agents") or []] or ["harness"]
    if agent and not any(name in agent for name in names):
        return
    exe = shutil.which("eks-harness")
    if exe is None:
        return
    grace = str(config.get("agent_stop_grace") or os.environ.get("EKS_HARNESS_AGENT_STOP_GRACE") or 900)
    env = dict(os.environ)
    if data.get("session_id"):
        env["CLAUDE_CODE_SESSION_ID"] = str(data["session_id"])
    subprocess.Popen([exe, "lease", "idle", "--grace", grace], cwd=str(project_dir(data)), env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


if __name__ == "__main__":
    main()
