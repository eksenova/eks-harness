from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "claude" / "hooks"
RAW = "http://hub/raw/01J0000000000000000000000A/login.png"
PR = "gh pr " + "create"


def run(hook: str, data: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps(data), capture_output=True,
                          text=True, env={**os.environ, "TMPDIR": data.get("_tmp", "/tmp")})


def transcript(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return str(path)


def use(tool_id: str, name: str, given: dict) -> dict:
    return {"message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": given}]}}


def result(tool_id: str, text: str) -> dict:
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def test_evidence_guard_blocks_until_screenshots_are_read(tmp_path: Path) -> None:
    report = "screenshot login\n  direct: " + RAW + "\n  local: /tmp/x/login.png\n"
    entries = [use("1", "Bash", {"command": "eks-harness flow run flows/login.py"}), result("1", report)]
    blocked = run("evidence_guard.py", {"transcript_path": transcript(tmp_path, entries), "cwd": str(tmp_path)})
    assert blocked.returncode == 2 and "/tmp/x/login.png" in blocked.stderr
    entries.append(use("2", "Read", {"file_path": "/tmp/x/login.png"}))
    ok = run("evidence_guard.py", {"transcript_path": transcript(tmp_path, entries), "cwd": str(tmp_path)})
    assert ok.returncode == 0
    video = {"kind": "video", "name": "demo", "raw": RAW.replace("login.png", "d.mp4"), "sheet": "/tmp/x/demo-sheet.png"}
    mcp = [use("3", "mcp__plugin_eks-harness_eks-harness__flow_run", {"flow": "f.py"}),
           result("3", json.dumps({"artifacts": [video]}))]
    blocked = run("evidence_guard.py", {"transcript_path": transcript(tmp_path, mcp), "cwd": str(tmp_path)})
    assert blocked.returncode == 2 and "demo-sheet.png" in blocked.stderr
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.toml").write_text("[claude.hooks]\nevidence = false\n", encoding="utf-8")
    assert run("evidence_guard.py", {"transcript_path": transcript(tmp_path, mcp), "cwd": str(tmp_path)}).returncode == 0


def test_cheap_inspection_guard_denies_capture_after_action(tmp_path: Path) -> None:
    base = {"session_id": "s1", "cwd": str(tmp_path), "_tmp": str(tmp_path)}
    act = "mcp__plugin_eks-harness_eks-harness__driver_act"
    cap = "mcp__plugin_eks-harness_eks-harness__driver_capture"
    obs = "mcp__plugin_eks-harness_eks-harness__driver_observe"
    assert run("cheap_inspection_guard.py", {**base, "tool_name": act,
                                             "tool_input": {"sid": "abc", "action": "press"}}).stdout == ""
    denied = run("cheap_inspection_guard.py",
                 {**base, "tool_name": cap, "tool_input": {"sid": "abc", "kind": "screenshot"}})
    assert "deny" in denied.stdout
    run("cheap_inspection_guard.py", {**base, "tool_name": obs, "tool_input": {"sid": "abc", "query": "tree"}})
    allowed = run("cheap_inspection_guard.py",
                  {**base, "tool_name": cap, "tool_input": {"sid": "abc", "kind": "screenshot"}})
    assert allowed.stdout == ""


def test_pr_guard_needs_evidence_for_ui_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.toml").write_text('[claude]\nui_paths = ["app/"]\nbase_ref = "main"\n',
                                                    encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "feature"], check=True)
    (repo / "app").mkdir()
    (repo / "app" / "page.tsx").write_text("x", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "ui"], check=True)
    data = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": f"{PR} --title t --body 'fix'"}}
    assert "deny" in run("pr_evidence_guard.py", data).stdout
    data["tool_input"]["command"] = f"{PR} --title t --body 'see http://hub/s/abc'"
    assert run("pr_evidence_guard.py", data).stdout == ""
