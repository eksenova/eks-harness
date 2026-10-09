#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, context, enabled, payload, project_dir, state_file  # noqa: E402

DEFAULT_WORDS = ("harness", "eks-harness", "ehx", "simulator", "emulator", "flow run")


def main() -> None:
    data = payload()
    config = claude_config(project_dir(data))
    if not enabled("harness_hint", config):
        return
    prompt = str(data.get("prompt") or "")
    words = [str(w) for w in config.get("harness_words") or []] or list(DEFAULT_WORDS)
    if not any(re.search(rf"\b{re.escape(word)}\b", prompt, re.IGNORECASE) for word in words):
        return
    marker = state_file(str(data.get("session_id") or ""), "harness-hint")
    if marker.exists():
        return
    marker.write_text("1")
    extra = str(config.get("harness_hint") or "")
    context("UserPromptSubmit", "The user mentioned the harness. If they want an app run or verified, load the "
                                "eks-harness:flows skill and drive it with a flow (one flow per question, read every "
                                "screenshot and contact sheet it prints). If they talk about the harness itself, "
                                "start nothing." + (f" {extra}" if extra else ""))


if __name__ == "__main__":
    main()
