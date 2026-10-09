from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.service import source_checkout_src

DEFAULT_SPEC = "git+https://github.com/eksenova/eks-harness"
REMOTE_DIR = ".cache/eks-harness-install"


class InstallError(RuntimeError):
    pass


@dataclass
class RemoteTarget:
    host: str
    wsl: bool = False
    ssh_options: list[str] = field(default_factory=lambda: ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"])

    def command(self, script: str) -> list[str]:
        remote = f"bash -lc {shlex.quote(script)}"
        if self.wsl:
            remote = f"wsl -e bash -lc {shlex.quote(script)}"
        return ["ssh", *self.ssh_options, self.host, remote]

    def run(self, script: str, *, stdin: str | None = None, timeout: float = 900, check: bool = True) -> str:
        out = subprocess.run(self.command(script), input=stdin, capture_output=True, text=True, timeout=timeout)
        if check and out.returncode != 0:
            raise InstallError(f"{self.host}: `{script}` failed ({out.returncode}): {(out.stderr or out.stdout).strip()}")
        return out.stdout

    def copy(self, local: Path, remote_name: str) -> str:
        if self.wsl:
            data = local.read_bytes()
            target = f"$HOME/{REMOTE_DIR}/{remote_name}"
            script = f"mkdir -p $HOME/{REMOTE_DIR} && base64 -d > {target} && echo {target}"
            out = subprocess.run(self.command(script), input=base64.b64encode(data).decode(), capture_output=True,
                                 text=True, timeout=900)
            if out.returncode != 0:
                raise InstallError(f"{self.host}: copying {local.name} failed: {out.stderr.strip()}")
            return out.stdout.strip().splitlines()[-1]
        self.run(f"mkdir -p $HOME/{REMOTE_DIR}")
        home = self.run("echo $HOME").strip()
        target = f"{home}/{REMOTE_DIR}/{remote_name}"
        out = subprocess.run(["scp", "-q", *self.ssh_options, str(local), f"{self.host}:{target}"],
                             capture_output=True, text=True, timeout=900)
        if out.returncode != 0:
            raise InstallError(f"{self.host}: scp failed: {out.stderr.strip()}")
        return target


def build_wheel(out_dir: Path) -> Path:
    src = source_checkout_src()
    if src is None:
        raise InstallError("no source checkout to build a wheel from; pass --wheel or --spec")
    root = src.parent
    uv = shutil.which("uv")
    if uv is None:
        raise InstallError("uv is not on PATH")
    env = {**os.environ, "EKS_HARNESS_SKIP_WEB": "1"}
    out = subprocess.run([uv, "build", "--wheel", "--out-dir", str(out_dir), str(root)], capture_output=True,
                         text=True, env=env, timeout=900)
    if out.returncode != 0:
        raise InstallError(f"wheel build failed: {(out.stderr or out.stdout).strip()[-2000:]}")
    wheels = sorted(out_dir.glob("eks_harness-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise InstallError("wheel build produced no wheel")
    return wheels[-1]


def install_node(target: RemoteTarget, *, hub: str, token: str, name: str, slots: list[dict[str, Any]] | None = None,
                 blender: str = "", env: dict[str, str] | None = None, wheel: Path | None = None,
                 spec: str | None = None, extras: str = "", python: str = "3.13",
                 log: Callable[[str], None] = print) -> dict[str, Any]:
    facts = target.run("uname -s; uname -m; echo $HOME; command -v uv || test -x $HOME/.local/bin/uv && "
                       "echo $HOME/.local/bin/uv || echo NO_UV").split()
    system, machine, home = facts[0], facts[1], facts[2]
    log(f"{target.host}: {system} {machine}")
    uv = facts[3] if len(facts) > 3 and facts[3] != "NO_UV" else ""
    if not uv:
        log("installing uv")
        target.run("curl -LsSf https://astral.sh/uv/install.sh | sh")
        uv = f"{home}/.local/bin/uv"
    if spec is None:
        with tempfile.TemporaryDirectory() as tmp:
            built = wheel or build_wheel(Path(tmp))
            log(f"copying {built.name}")
            source = target.copy(built, built.name)
    else:
        source = spec
    requirement = f"eks-harness[{extras}] @ {source}" if extras and "://" in source else source
    if extras and "://" not in source:
        requirement = f"{source}[{extras}]"
    log("installing eks-harness")
    target.run(f"{shlex.quote(uv)} tool install --force --python {python} {shlex.quote(requirement)}")
    exe = f"{home}/.local/bin/eks-harness"
    args = ["node", "configure", "--hub", hub, "--name", name, "--token-stdin"]
    if slots:
        args += ["--slots", json.dumps(slots)]
    if blender:
        args += ["--blender", blender]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    target.run(" ".join(shlex.quote(a) for a in [exe, *args]), stdin=token + "\n")
    log("installing the node service")
    service_out = target.run(f"{shlex.quote(exe)} node service install --json", check=False)
    hint = ""
    if system == "Linux":
        lingering = target.run("loginctl show-user $USER --property=Linger 2>/dev/null || true", check=False)
        if "yes" not in lingering:
            hint = "run 'sudo loginctl enable-linger $USER' on the node so it keeps running after logout"
    return {"host": target.host, "system": system, "machine": machine, "executable": exe,
            "service": service_out.strip(), "hint": hint}
