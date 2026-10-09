from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

DEFAULTS: dict[str, Any] = {
    "evidence": True,
    "cheap_inspection": True,
    "pr_evidence": True,
    "agent_stop": True,
    "prewarm": False,
    "design_hint": True,
    "harness_hint": True,
}


def payload() -> dict[str, Any]:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def project_dir(data: dict[str, Any] | None = None) -> Path:
    raw = (data or {}).get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(raw)


def tree_of(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".harness" / "project.toml").is_file():
            return candidate
    try:
        out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def claude_config(start: Path) -> dict[str, Any]:
    file = tree_of(start) / ".harness" / "project.toml"
    if tomllib is None or not file.is_file():
        return {}
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    section = data.get("claude")
    return section if isinstance(section, dict) else {}


def enabled(name: str, config: dict[str, Any]) -> bool:
    if os.environ.get(f"EKS_HARNESS_HOOK_{name.upper()}") == "0":
        return False
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
    return bool(hooks.get(name, DEFAULTS.get(name, True)))


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def context(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
    sys.exit(0)


def iter_transcript(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def state_file(session_id: str, name: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")[:48]
    base = Path(os.environ.get("TMPDIR") or "/tmp") / "eks-harness-hooks"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{safe}-{name}"
