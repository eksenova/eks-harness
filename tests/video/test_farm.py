"""eks_harness.video.farm: config discovery, path mapping and the pull queue with real local workers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from eks_harness.video import farm


@pytest.fixture(autouse=True)
def _no_user_farm(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never pick up the developer's user farm.toml (it would send test work to real machines)."""

    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("EKS_HARNESS_FARM_CONFIG", raising=False)
    monkeypatch.delenv("EKS_HARNESS_FARM", raising=False)


WORKER = """
import sys, pathlib
out = pathlib.Path(sys.argv[1])
print("EHX_DEVICE test-worker", flush=True)
print("EHX_READY", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line or line == "done":
        break
    (out / f"f_{int(line):06d}.png").write_text(line)
    print(f"EHX_FRAME {line} - -", flush=True)
"""


def test_no_config_means_local_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EKS_HARNESS_FARM_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = farm.load(tmp_path, local_workers=3, local_slots=[{"A": "1"}, {"A": "2"}])
    assert config.source is None and not config.remotes
    assert [s.env["A"] for s in config.hosts[0].slots] == ["1", "2", "1"]


def test_toml_config_hosts_slots_and_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".harness" / "farm.toml"
    path.parent.mkdir()
    path.write_text("""
[local]
enabled = false

[[remote]]
host = "gpu-box"
root = "~/farm"
workers_per_slot = 2
slots = [{ env = { CUDA_VISIBLE_DEVICES = "0" } }, { env = { CUDA_VISIBLE_DEVICES = "1" } }]
env = { X = "y" }
tools = { blender = "~/opt/blender/blender" }
""")
    monkeypatch.delenv("EKS_HARNESS_FARM_CONFIG", raising=False)
    config = farm.load(tmp_path)
    assert config.source == path
    (remote,) = config.hosts
    assert remote.remote and remote.name == "gpu-box"
    assert [s.env["CUDA_VISIBLE_DEVICES"] for s in remote.slots] == ["0", "0", "1", "1"]
    assert remote.tools["blender"] == "~/opt/blender/blender"
    monkeypatch.setenv("EKS_HARNESS_FARM", "0")
    assert farm.load(tmp_path).source is None


def test_remote_paths_mirror_absolute_paths(tmp_path: Path) -> None:
    host = farm.Host(name="r", host="r", root="/srv/farm")
    job = farm.FarmJob(name="j", items=[], command=lambda w: [], out_dir=tmp_path / "out")
    root = tmp_path.resolve().as_posix()
    assert job.remote_path(tmp_path / "scenes" / "a.py", host) == f"/srv/farm/abs{root}/scenes/a.py"
    assert job.remote_path(Path("/opt/tool/x.py"), host) == "/srv/farm/abs/opt/tool/x.py"


def test_local_workers_pull_every_item(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    out = tmp_path / "out"
    seen: list[str] = []
    job = farm.FarmJob(name="test", items=list(range(1, 21)), out_dir=out,
                       command=lambda w: [sys.executable, str(worker), str(out)],
                       on_line=lambda line, tag: seen.append(line.split()[1]))
    stats = farm.run(job, farm.load(None, local_workers=3))
    assert sorted(int(x) for x in seen) == list(range(1, 21))
    assert len(list(out.glob("f_*.png"))) == 20
    assert sum(s.done for s in stats) == 20 and all(s.device == "test-worker" for s in stats)
