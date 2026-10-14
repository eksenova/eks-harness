from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_removed.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_removed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_references_to_removed_apis_remain() -> None:
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr


def test_the_check_finds_removed_names_and_skips_the_planning_record(tmp_path: Path) -> None:
    check = load()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "dsl").mkdir(parents=True)
    (tmp_path / "workers" / "kit").mkdir(parents=True)
    (tmp_path / "src" / "old.py").write_text("from eks_harness.flows import WebApp\nclean = 1\n", encoding="utf-8")
    (tmp_path / "src" / "manifest.toml").write_text("[[contributes.driver]]\n[[contributes.driver_extension]]\n",
                                                   encoding="utf-8")
    (tmp_path / "workers" / "kit" / "server.mjs").write_text("const x = await post('/exec', {})\n", encoding="utf-8")
    (tmp_path / "docs" / "dsl" / "plan.md").write_text("`flow_run` and `/exec` are removed\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("Run `eks-harness flow run x.py`.\n", encoding="utf-8")
    (tmp_path / "docs" / "migration.md").write_text("`flow_run` became `eks_run`\n", encoding="utf-8")
    (tmp_path / "claude" / "skills" / "eks-migration").mkdir(parents=True)
    (tmp_path / "claude" / "skills" / "eks-migration" / "SKILL.md").write_text("WorkerSession\n", encoding="utf-8")
    hits = {(path, line, label) for path, line, label, _ in check.scan(tmp_path)}
    assert ("src/old.py", 1, "module eks_harness.flows") in hits
    assert ("src/old.py", 1, "flow app classes") in hits
    assert ("src/manifest.toml", 1, "removed contribution types") in hits
    assert not any(path == "src/manifest.toml" and line == 2 for path, line, _ in hits)
    assert ("workers/kit/server.mjs", 1, "worker /exec and legacy run routes") in hits
    assert ("docs/guide.md", 1, "CLI flow run|list") in hits
    assert not any(path in ("docs/migration.md", "claude/skills/eks-migration/SKILL.md") for path, _, _ in hits)
    assert not any(path.startswith("docs/dsl/") or line == 2 and path == "src/old.py" for path, line, _ in hits)
    assert check.main(["check_removed", str(tmp_path)]) == 1


def test_the_allowlist_is_narrow_and_counted(tmp_path: Path, capsys) -> None:
    check = load()
    kept = tmp_path / "src" / "eks_harness"
    (kept / "flows").mkdir(parents=True)
    (kept / "drivers").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (kept / "flows" / "runner.py").write_text("from eks_harness.drivers.sessions import WorkerSession\n",
                                              encoding="utf-8")
    (kept / "drivers" / "client.py").write_text(
        '        status, result = self.post("/exec", body, timeout)\nsession = WorkerSession()\n', encoding="utf-8")
    (tmp_path / "tests" / "test_mcp_http.py").write_text(
        '    assert "driver_act" not in names and "flow_run" not in names\n    call("flow_run")\n', encoding="utf-8")
    failing, kept_counts = check.split(check.scan(tmp_path))
    assert {(path, line) for path, line, _, _ in failing} == {("src/eks_harness/drivers/client.py", 2),
                                                             ("tests/test_mcp_http.py", 2)}
    assert kept_counts == {check.KEPT: 3, check.GONE: 1}
    assert check.main(["check_removed", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"3 allowlisted reference(s): {check.KEPT}" in out and f"1 allowlisted reference(s): {check.GONE}" in out
