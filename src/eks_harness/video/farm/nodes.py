from __future__ import annotations

import fnmatch
import logging
import platform
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eks_harness.video.farm import FarmJob, Host, Slot, WorkerContext

_LOG = logging.getLogger(__name__)
ROOT_TOKEN = "${EHX_FARM_ROOT}"
JOB_KIND = "farm.batch"
TERMINAL = ("done", "failed", "cancelled")


def _client() -> Any:
    from eks_harness.cli.client import client_from_args

    return client_from_args(SimpleNamespace(url=None, api_key=None))


def _tag(caps: dict[str, Any]) -> str:
    system = str(caps.get("os") or "").lower()
    machine = str(caps.get("arch") or "").lower()
    if machine == "amd64":
        machine = "x86_64"
    return f"{system}-{machine}" if system else ""


def node_hosts(section: dict[str, Any], client: Any = None, *, skip_this_machine: bool = False) -> list[Host]:
    wanted = set(section.get("nodes") or [])
    skip = set(section.get("exclude") or [])
    owned = client is None
    client = client or _client()
    try:
        items = client.get("/api/nodes")["items"]
    except Exception as error:
        _LOG.warning("farm: the hub's nodes are not reachable (%s); rendering without them", error)
        return []
    finally:
        if owned:
            client.close()
    hosts: list[Host] = []
    for item in items:
        if not item.get("online") or item["id"] in skip or (wanted and item["id"] not in wanted):
            continue
        caps = item.get("capabilities") or {}
        if skip_this_machine and caps.get("hostname") and caps.get("hostname") == platform.node():
            continue
        slots: list[Slot] = []
        for slot in item.get("slots") or []:
            if section.get("gpu_only", True) and "gpu" not in (slot.get("tags") or []) and any(
                    "gpu" in (s.get("tags") or []) for s in item.get("slots") or []):
                continue
            workers = int(section.get("workers_per_slot") or slot.get("workers") or 1)
            slots.extend(Slot(env={}, id=str(slot["id"])) for _ in range(workers))
        if not slots:
            continue
        capabilities = sorted({c for c in ("blender", "chrome") if caps.get(c)} | set(caps.get("jobKinds") or []))
        tools = {"blender": str((caps.get("blender") or {}).get("path") or "blender")}
        hosts.append(Host(name=item["id"], node=item["id"], slots=slots, tools=tools, tag=_tag(caps),
                          capabilities=capabilities))
    return hosts


def _excluded(relative: str, name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("/"):
            if relative == pattern[1:] or relative.startswith(pattern[1:] + "/"):
                return True
        elif fnmatch.fnmatch(name, pattern):
            return True
    return False


def pack(job: FarmJob, target: Path) -> Path:
    with tarfile.open(target, "w") as archive:
        for root in job.sync:
            root = root.resolve()
            if root.is_file():
                archive.add(root, arcname=f"abs{root.as_posix()}", recursive=False)
                continue
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if any(_excluded("/".join(relative.split("/")[: i + 1]), part, job.exclude)
                       for i, part in enumerate(relative.split("/"))):
                    continue
                if path.is_file():
                    info = archive.gettarinfo(str(path), arcname=f"abs{path.as_posix()}")
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
    return target


class NodeFarmSession:
    root = ROOT_TOKEN

    def __init__(self, job: FarmJob, client: Any = None) -> None:
        self.job = job
        self.client = client or _client()
        self.lock = threading.Lock()
        self.sync_hash: str | None = None

    def _upload_sync(self) -> str:
        with self.lock:
            if self.sync_hash:
                return self.sync_hash
            with tempfile.TemporaryDirectory() as tmp:
                archive = pack(self.job, Path(tmp) / "sync.tar")
                with archive.open("rb") as handle:
                    response = self.client._send("POST", "/api/blobs", content=handle,
                                                 headers={"Content-Type": "application/x-tar"}, timeout=3600)
                if response.status_code != 200:
                    raise RuntimeError(f"uploading farm inputs failed ({response.status_code}): {response.text[:300]}")
                self.sync_hash = response.json()["hash"]
            return self.sync_hash

    def run_batch(self, ctx: WorkerContext, argv: list[str], items: list[int]) -> list[str]:
        digest = self._upload_sync()
        slot_id = next((s.id for s in ctx.host.slots if s.id), "")
        payload = {"argv": argv, "env": ctx.env, "items": items, "outDir": ctx.path(self.job.out_dir),
                   "outPattern": self.job.out_pattern, "name": self.job.name, "syncHash": digest}
        requirements: dict[str, Any] = {"node": ctx.host.node}
        if slot_id:
            requirements["slot"] = slot_id
        created = self.client.post("/api/jobs", json={"kind": JOB_KIND, "payload": payload,
                                                      "requirements": requirements,
                                                      "inputs": [{"name": "sync", "hash": digest}]})
        job_id = created["id"]
        deadline = time.time() + 6 * 3600
        record = created
        while record.get("state") not in TERMINAL:
            if time.time() > deadline:
                self.client.post(f"/api/jobs/{job_id}/cancel")
                raise RuntimeError(f"node job {job_id} did not finish")
            record = self.client.get(f"/api/jobs/{job_id}", params={"wait": 120}, timeout=180)
        if record["state"] != "done":
            raise RuntimeError(f"node job {job_id} on {ctx.host.node} {record['state']}: {record.get('error')}")
        result = record.get("result") or {}
        self.job.out_dir.mkdir(parents=True, exist_ok=True)
        for output in result.get("outputs") or []:
            target = (self.job.out_dir / output["name"]).resolve()
            if not target.is_relative_to(self.job.out_dir.resolve()):
                continue
            tmp = target.with_suffix(target.suffix + ".part")
            self.client.download(f"/api/blobs/{output['hash']}", tmp)
            tmp.replace(target)
        data = result.get("data") or {}
        return [str(line) for line in data.get("lines") or []]


def node_farm_batch(ctx: Any) -> dict[str, Any]:
    import os
    import subprocess

    from eks_harness.nodes.jobs import JobFailed

    payload = ctx.payload
    archive = ctx.inputs.get("sync")
    if archive is None:
        raise JobFailed("farm batch without inputs")
    root = Path(os.environ.get("EKS_HARNESS_FARM_ROOT") or (Path.home() / ".cache" / "eks-harness-farm" / "nodes"))
    marker = root / "unpacked" / str(payload.get("syncHash") or archive.stat().st_size)
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        with tarfile.open(archive) as handle:
            handle.extractall(root, filter="data")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
    argv = [str(a).replace(ROOT_TOKEN, str(root)) for a in payload["argv"]]
    out_dir = Path(str(payload["outDir"]).replace(ROOT_TOKEN, str(root)))
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**ctx.env, **{k: str(v).replace(ROOT_TOKEN, str(root)) for k, v in (payload.get("env") or {}).items()}}
    started = time.time()
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=env, cwd=str(root))
    assert process.stdin is not None and process.stdout is not None
    lines: list[str] = []
    tail: list[str] = []
    pending = list(payload["items"])
    total = max(1, len(pending))
    finished = 0
    closed = False

    def feed() -> None:
        nonlocal closed
        if closed:
            return
        try:
            if pending:
                process.stdin.write(f"{pending.pop(0)}\n")
                process.stdin.flush()
            else:
                process.stdin.write("done\n")
                process.stdin.close()
                closed = True
        except (BrokenPipeError, ValueError):
            closed = True

    for raw in process.stdout:
        line = raw.rstrip("\n")
        tail.append(line)
        del tail[:-60]
        if ctx.cancelled.is_set():
            process.kill()
            break
        if line.startswith("EHX_READY"):
            feed()
        elif line.startswith(("EHX_FRAME", "EHX_SKIP")):
            lines.append(line)
            finished += 1
            ctx.progress(finished / total, line)
            feed()
        elif line.startswith("EHX_DEVICE"):
            lines.append(line)
    code = process.wait()
    if finished < total:
        raise JobFailed(f"worker exited {code} after {finished} of {total} items:\n" + "\n".join(tail[-30:]),
                        retry=True)
    for path in sorted(out_dir.glob(payload.get("outPattern") or "*")):
        if path.is_file() and path.stat().st_mtime >= started - 1:
            ctx.output(path, path.name)
    return {"lines": lines, "exitCode": code, "tail": tail[-10:]}
