from __future__ import annotations

import sys
from pathlib import Path

import httpx

from eks_harness.video import farm
from eks_harness.video.farm.nodes import node_hosts, pack
from test_nodes import AgentThread, live_hub, make_agent  # noqa: F401

WORKER = """
import os, sys, pathlib
out = pathlib.Path(sys.argv[1])
scene = pathlib.Path(sys.argv[2]).read_text().strip()
print("EHX_DEVICE fake", flush=True)
print("EHX_READY", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line or line == "done":
        break
    (out / f"f_{int(line):06d}.png").write_text(f"{scene}:{line}:{os.environ.get('SLOT_MARK', '')}:{os.environ.get('EKS_HARNESS_SLOT')}")
    print(f"EHX_FRAME {line} fp linux", flush=True)
"""


def test_farm_runs_batches_on_nodes(live_hub, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    ctx, base = live_hub
    monkeypatch.setenv("EKS_HARNESS_FARM_ROOT", str(tmp_path / "node-farm"))
    project = tmp_path / "project"
    (project / "cache").mkdir(parents=True)
    (project / "scene.txt").write_text("hello", encoding="utf-8")
    (project / "cache" / "big.bin").write_text("skip me", encoding="utf-8")
    (project / "worker.py").write_text(WORKER, encoding="utf-8")
    with httpx.Client(base_url=base, timeout=60) as client:
        token = client.post("/api/nodes", json={"id": "gpu-box"}).json()["token"]
        agent = make_agent(tmp_path, base, token, slots=[{"id": "gpu0", "workers": 1, "tags": ["gpu", "cuda"],
                                                           "env": {"SLOT_MARK": "g0"}},
                                                          {"id": "gpu1", "workers": 1, "tags": ["gpu", "cuda"],
                                                           "env": {"SLOT_MARK": "g1"}}])
        agent.handlers.update({"farm.batch": __import__("eks_harness.nodes.jobs", fromlist=["BUILTIN"]).BUILTIN["farm.batch"]})
        with AgentThread(agent):
            from eks_harness.cli.client import HarnessClient

            hub = HarnessClient(base)
            hosts = node_hosts({"enabled": True}, client=hub)
            assert [h.node for h in hosts] == ["gpu-box"] and len(hosts[0].slots) == 2
            hosts[0].capabilities.append("python")
            out_dir = tmp_path / "frames"
            seen: list[str] = []

            def command(worker: farm.WorkerContext) -> list[str]:
                return [sys.executable, worker.path(project / "worker.py"), worker.path(out_dir),
                        worker.path(project / "scene.txt")]

            job = farm.FarmJob(name="test", items=list(range(1, 11)), command=command, out_dir=out_dir,
                               sync=[project], exclude=["/cache"], on_line=lambda line, tag: seen.append(line),
                               requires=["python"], batch=3)
            stats = farm.run(job, farm.FarmConfig(hosts=hosts))
            assert sorted(p.name for p in out_dir.glob("f_*.png")) == [f"f_{i:06d}.png" for i in range(1, 11)]
            frames = [p.read_text().split(":") for p in out_dir.glob("f_*.png")]
            assert all(scene == "hello" and mark == "g" + slot[-1] for scene, _, mark, slot in frames)
            requested = {j["requirements"].get("slot") for j in httpx.get(base + "/api/jobs").json()["items"]}
            assert requested == {"gpu0", "gpu1"}
            assert len(seen) == 10 and sum(s.done for s in stats) == 10
            hub.close()
    names = []
    import tarfile

    archive = pack(job, tmp_path / "sync.tar")
    with tarfile.open(archive) as handle:
        names = handle.getnames()
    assert any(n.endswith("scene.txt") for n in names) and not any("big.bin" in n for n in names)


def test_frames_reported_by_two_batches_download_once(tmp_path: Path) -> None:
    import threading
    from types import SimpleNamespace

    from eks_harness.video.farm.nodes import NodeFarmSession

    downloads: list[str] = []
    gate = threading.Barrier(2)

    class Client:
        def post(self, path, json=None):
            gate.wait(timeout=5)
            outputs = [{"name": "f_000001.png", "hash": "h1"}, {"name": "f_000002.png", "hash": "h2"}]
            return {"id": "j", "state": "done", "result": {"outputs": outputs, "data": {"lines": []}}}

        def download(self, path, dest):
            downloads.append(path)
            Path(dest).write_text(path)

    job = farm.FarmJob(name="test", items=[1, 2], command=lambda w: [], out_dir=tmp_path / "frames")
    session = NodeFarmSession(job, client=Client())
    session.sync_hash = "s"
    host = farm.Host(name="box", node="box", slots=[farm.Slot(id="gpu0"), farm.Slot(id="gpu1")])
    contexts = [farm.WorkerContext(name=f"box#{i}", host=host, env={}, index=i, job=job, slot=host.slots[i])
                for i in range(2)]
    threads = [threading.Thread(target=session.run_batch, args=(c, [], [1, 2])) for c in contexts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(downloads) == ["/api/blobs/h1", "/api/blobs/h2"]
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == ["f_000001.png", "f_000002.png"]
