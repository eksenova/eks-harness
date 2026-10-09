from __future__ import annotations

import json
import textwrap
from pathlib import Path

from eks_harness.cli import main
from eks_harness.cli.migrate_cmds import farm_nodes, studio_jobs


def test_doctor_offline_reports_checks(capsys) -> None:
    assert main(["doctor", "--offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = {c["check"] for c in data["checks"]}
    assert {"version", "config", "tool ffmpeg", "service daemon", "service node"} <= names


def test_migrate_config_applies_known_keys(tmp_path: Path, capsys) -> None:
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"browser.instances": 2, "legacy.thing": 1, "devices.ios": "x"}), encoding="utf-8")
    assert main(["migrate", "config", str(old), "--set", 'devices.namePrefix=Acme', "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == {"browser.instances": 2, "devices.namePrefix": "Acme"}
    assert set(data["skipped"]) == {"legacy.thing", "devices.ios"}
    from eks_harness.config import load
    from eks_harness.paths import resolve_paths

    assert load(resolve_paths())["devices.namePrefix"] == "Acme"


def test_farm_toml_becomes_nodes(tmp_path: Path) -> None:
    farm = tmp_path / "farm.toml"
    farm.write_text(textwrap.dedent("""
        [local]
        enabled = true
        [[remote]]
        name = "gpu_box"
        host = "user@gpu-box.lan"
        workers_per_slot = 2
        env = { VIDEODSL_CYCLES_DEVICE = "OPTIX" }
        slots = [{ env = { CUDA_VISIBLE_DEVICES = "0" } }, { env = { CUDA_VISIBLE_DEVICES = "1" } }]
        tools = { blender = "~/opt/blender/blender" }
        [[remote]]
        host = "win-box"
        wsl = true
    """), encoding="utf-8")
    nodes = farm_nodes(farm)
    assert [n["id"] for n in nodes] == ["gpu-box", "win-box"]
    gpu = nodes[0]
    assert gpu["blender"] == "~/opt/blender/blender" and gpu["wsl"] is False
    assert [s["id"] for s in gpu["slots"]] == ["gpu0", "gpu1"]
    assert gpu["slots"][1] == {"id": "gpu1", "workers": 2, "tags": ["gpu", "optix"], "backend": "OPTIX",
                               "env": {"CUDA_VISIBLE_DEVICES": "1", "EKS_HARNESS_CYCLES_DEVICE": "OPTIX"}}
    assert nodes[1]["wsl"] is True and nodes[1]["slots"][0]["tags"] == ["cpu"]


def test_studio_jobs_picks_finished_renders(tmp_path: Path) -> None:
    out = tmp_path / "r.mp4"
    out.write_bytes(b"x")
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": [
        {"job_id": "a", "status": "succeeded", "output_path": str(out), "project_id": "promo"},
        {"job_id": "b", "status": "failed", "output_path": None},
        {"job_id": "c", "status": "succeeded", "output_path": str(tmp_path / "gone.mp4")}]}), encoding="utf-8")
    assert [j["job_id"] for j in studio_jobs(tmp_path)] == ["a"]
