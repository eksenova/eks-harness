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


SHOT_ID = "01J0000000000000000000000A"
VIDEO_ID = "01J0000000000000000000000B"
TOOL = "mcp__plugin_eks-harness_eks-harness__"


def evidence(tmp_path: Path, entries: list[dict]) -> subprocess.CompletedProcess:
    return run("evidence_guard.py", {"transcript_path": transcript(tmp_path, entries), "cwd": str(tmp_path)})


def test_evidence_guard_reads_the_text_report_of_eks_harness_run(tmp_path: Path) -> None:
    report = ("flow login: passed\nrun 01J0000000000000000000000R\nartifacts:\n"
              f"  screenshot login http://hub/p/acme/web/a/{SHOT_ID} {RAW}\n"
              "time: 812 ms (queued 0, preparing 400, running 412, paused 0)\n")
    entries = [use("1", "Bash", {"command": "eks-harness run web-app/.harness/flows/login.py"}), result("1", report)]
    blocked = evidence(tmp_path, entries)
    assert blocked.returncode == 2 and f"artifacts download {SHOT_ID}" in blocked.stderr
    entries.append(use("2", "Bash", {"command": f"eks-harness artifacts download {SHOT_ID} -o /tmp/ev"}))
    assert evidence(tmp_path, entries).returncode == 2
    entries.append(use("3", "Read", {"file_path": "/tmp/ev/login.png"}))
    assert evidence(tmp_path, entries).returncode == 0


def test_evidence_guard_reads_eks_tool_results_and_accepts_mcp_looks(tmp_path: Path) -> None:
    video = {"id": VIDEO_ID, "kind": "video", "name": "tour.mp4",
             "rawUrl": f"http://hub/raw/{VIDEO_ID}/tour.mp4", "url": f"http://hub/p/acme/web/a/{VIDEO_ID}"}
    shot = {"id": SHOT_ID, "kind": "screenshot", "name": "login.png", "rawUrl": RAW}
    pause_shot = {"id": "01J0000000000000000000000P", "kind": "screenshot", "name": "pause.png"}
    entries = [use("1", f"{TOOL}eks_run", {"spec": "flows/tour.py"}),
               result("1", json.dumps({"issues": [], "report": {"artifacts": [video, shot]},
                                       "pause": {"snapshots": [pause_shot]}}))]
    blocked = evidence(tmp_path, entries)
    assert blocked.returncode == 2
    assert f"video sheet {VIDEO_ID}" in blocked.stderr and SHOT_ID in blocked.stderr
    assert "01J0000000000000000000000P" not in blocked.stderr
    entries.append(use("2", f"{TOOL}get_artifact", {"artifact_id": SHOT_ID, "image": "full"}))
    entries.append(use("3", f"{TOOL}video_sheet", {"artifact_id": VIDEO_ID}))
    assert evidence(tmp_path, entries).returncode == 0
    session = [use("4", f"{TOOL}eks_session_do", {"session": "01J0000000000000000000000S", "op": "capture.shot"}),
               result("4", json.dumps({"value": shot, "artifacts": [shot]}))]
    assert evidence(tmp_path, session).returncode == 2
    sheet = [use("5", "Bash", {"command": "eks-harness run edits/demo.py --json"}),
             result("5", json.dumps({"artifacts": [video]})),
             use("6", "Bash", {"command": f"eks-harness video sheet {VIDEO_ID}"}),
             use("7", "Read", {"file_path": "/cache/review/tour/sheet-1.png"})]
    assert evidence(tmp_path, sheet).returncode == 0
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.toml").write_text("[claude.hooks]\nevidence = false\n", encoding="utf-8")
    assert evidence(tmp_path, entries[:2]).returncode == 0


def test_cheap_inspection_guard_denies_a_shot_after_an_action(tmp_path: Path) -> None:
    base = {"session_id": "s1", "cwd": str(tmp_path), "_tmp": str(tmp_path)}
    do = f"{TOOL}eks_session_do"

    def step(op: str, session: str = "run1") -> str:
        return run("cheap_inspection_guard.py", {**base, "tool_name": do,
                                                 "tool_input": {"session": session, "op": op, "args": {}}}).stdout

    assert step("ui.tap") == ""
    assert "deny" in step("capture.shot")
    assert step("capture.shot", session="run2") == ""
    assert step("ui.tree") == ""
    assert step("capture.shot") == ""
    control = {**base, "tool_name": f"{TOOL}eks_run_control",
               "tool_input": {"run_id": "run1", "action": "do", "op": "ui.fill"}}
    assert run("cheap_inspection_guard.py", control).stdout == ""
    shot = {**base, "tool_name": "Bash",
            "tool_input": {"command": "eks-harness runs control run1 do --op capture.shot --args '{}'"}}
    assert "deny" in run("cheap_inspection_guard.py", shot).stdout
    look = {**base, "tool_name": "Bash", "tool_input": {"command": "eks-harness runs control run1 do --op ui.url"}}
    assert run("cheap_inspection_guard.py", look).stdout == ""
    assert run("cheap_inspection_guard.py", shot).stdout == ""


def test_harness_hint_points_at_eks_flows_once(tmp_path: Path) -> None:
    data = {"session_id": "hint", "cwd": str(tmp_path), "_tmp": str(tmp_path), "prompt": "verify it in the harness"}
    first = json.loads(run("harness_hint.py", data).stdout)
    text = first["hookSpecificOutput"]["additionalContext"]
    assert "eks-harness:eks-flows" in text and "eks-harness run" in text
    assert run("harness_hint.py", data).stdout == ""
    assert run("harness_hint.py", {**data, "session_id": "other", "prompt": "fix the parser"}).stdout == ""


def test_prewarm_runs_a_one_shot_op_per_target(tmp_path: Path) -> None:
    project(tmp_path, '[claude]\nprewarm = ["web", "mobile-app:ios"]\n\n[claude.hooks]\nprewarm = true\n')
    bin_dir = fake_cli(tmp_path, {}, 0)
    data = {"session_id": "warm", "cwd": str(tmp_path), "_tmp": str(tmp_path), "tool_name": "EnterWorktree",
            "tool_input": {"path": str(tmp_path)}}
    out = run_with("prewarm.py", data, PATH=f"{bin_dir}:/usr/bin:/bin").stdout
    assert "warming web, mobile-app:ios" in out
    log = tmp_path / "calls.log"
    assert wait_for_call(log, "do ui.settle strict=false --platform web")
    assert wait_for_call(log, "do ui.settle strict=false --platform ios --app mobile-app")


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


def run_with(hook: str, data: dict, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps(data), capture_output=True,
                          text=True, env={**os.environ, "TMPDIR": data.get("_tmp", "/tmp"), **env})


def project(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".harness").mkdir(exist_ok=True)
    (tmp_path / ".harness" / "project.toml").write_text(body, encoding="utf-8")
    return tmp_path


def design_hint(tmp_path: Path, session: str, file: str) -> str:
    data = {"session_id": session, "cwd": str(tmp_path), "_tmp": str(tmp_path), "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / file)}}
    return run("design_hint.py", data).stdout


def test_design_hint_only_fires_where_the_repo_opts_in(tmp_path: Path) -> None:
    assert design_hint(tmp_path, "none", "web/src/App.tsx") == ""
    project(tmp_path, "[claude]\nui_design_paths = []\n\n[claude.hooks]\ndesign_hint = true\n")
    assert design_hint(tmp_path, "empty", "web/src/App.tsx") == ""
    project(tmp_path, "[claude.hooks]\ndesign_hint = true\n")
    assert "eks-harness-ui-design" in design_hint(tmp_path, "defaults", "web/src/App.tsx")
    project(tmp_path, '[claude]\nui_design_paths = ["app/ui/"]\nui_design_skill = "acme:design"\n')
    assert design_hint(tmp_path, "custom", "web/src/App.tsx") == ""
    assert "acme:design" in design_hint(tmp_path, "custom", "app/ui/Button.tsx")


def test_bash_guards_are_scoped_by_if_conditions() -> None:
    config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event in ("PreToolUse", "PostToolUse"):
        for group in config[event]:
            if group.get("matcher") == "Bash":
                assert all(str(h.get("if", "")).startswith("Bash(") for h in group["hooks"]), group
            assert "Bash|" not in str(group.get("matcher", "")), group
    commands = [h["command"] for g in config["PreToolUse"] for h in g["hooks"]]
    assert len(commands) == len(set(commands)) + 1


def fake_cli(tmp_path: Path, status: dict, code: int) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "eks-harness"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path / 'calls.log'}\n"
                      f"if [ \"$2\" = status ]; then echo '{json.dumps(status)}'; exit {code}; fi\nexit 0\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return bin_dir


def daemon_check(tmp_path: Path, session: str, bin_dir: Path) -> dict:
    data = {"session_id": session, "cwd": str(tmp_path), "_tmp": str(tmp_path), "source": "startup"}
    out = run_with("daemon_check.py", data, PATH=f"{bin_dir}:/usr/bin:/bin").stdout
    return json.loads(out) if out.strip() else {}


def wait_for_call(log: Path, text: str) -> bool:
    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        if log.is_file() and text in log.read_text(encoding="utf-8"):
            return True
        time.sleep(0.05)
    return False


def test_daemon_check_reports_and_only_starts_an_installed_service(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    missing = daemon_check(tmp_path, "s-missing", empty)
    assert "not installed" in missing["systemMessage"]
    assert daemon_check(tmp_path, "s-up", fake_cli(tmp_path, {"running": True}, 0)) == {}
    down = {"running": False, "url": "http://hub:7171", "service": False}
    reported = daemon_check(tmp_path, "s-down", fake_cli(tmp_path, down, 3))
    context = reported["hookSpecificOutput"]["additionalContext"]
    assert "eks-harness daemon start" in context and "nothing was started" in context
    assert "daemon start" not in (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert daemon_check(tmp_path, "s-down", fake_cli(tmp_path, down, 3)) == {}
    managed = daemon_check(tmp_path, "s-service", fake_cli(tmp_path, {**down, "service": True}, 3))
    assert "login service" in managed["systemMessage"]
    assert wait_for_call(tmp_path / "calls.log", "daemon start")
    project(tmp_path, "[claude]\ndaemon_autostart = false\n")
    (tmp_path / "calls.log").unlink()
    report_only = daemon_check(tmp_path, "s-optout", fake_cli(tmp_path, {**down, "service": True}, 3))
    assert "nothing was started" in report_only["hookSpecificOutput"]["additionalContext"]
    assert "daemon start" not in (tmp_path / "calls.log").read_text(encoding="utf-8")
    project(tmp_path, "[claude.hooks]\ndaemon_check = false\n")
    assert daemon_check(tmp_path, "s-off", fake_cli(tmp_path, down, 3)) == {}
