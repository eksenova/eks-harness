from __future__ import annotations

import glob
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Handler = Callable[["JobContext"], Any]


class JobFailed(RuntimeError):
    def __init__(self, message: str, *, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass
class JobContext:
    id: str
    kind: str
    payload: dict[str, Any]
    slot: dict[str, Any]
    workdir: Path
    inputs: dict[str, Path]
    env: dict[str, str]
    cancelled: threading.Event
    progress: Callable[[float, str], None]
    log: Callable[[str], None]
    upload: Callable[[Path, str], dict[str, Any]]
    outputs: list[dict[str, Any]] = field(default_factory=list)

    def output(self, path: Path, name: str | None = None) -> dict[str, Any]:
        record = self.upload(path, name or path.name)
        self.outputs.append(record)
        return record

    def check(self) -> None:
        if self.cancelled.is_set():
            raise JobFailed("cancelled")

    def run(self, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            on_line: Callable[[str], None] | None = None, timeout: float | None = None) -> int:
        merged = {**self.env, **(env or {})}
        process = subprocess.Popen(argv, cwd=cwd or self.workdir, env=merged, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        started = time.time()
        assert process.stdout is not None
        for line in process.stdout:
            if on_line:
                on_line(line.rstrip("\n"))
            if self.cancelled.is_set() or (timeout and time.time() - started > timeout):
                process.kill()
                break
        code = process.wait()
        if self.cancelled.is_set():
            raise JobFailed("cancelled")
        return code


def _probe(ctx: JobContext) -> dict[str, Any]:
    from eks_harness.nodes.capabilities import probe

    return probe()


def _echo(ctx: JobContext) -> dict[str, Any]:
    delay = float(ctx.payload.get("sleep") or 0)
    steps = max(1, int(ctx.payload.get("steps") or 1))
    for index in range(steps):
        ctx.check()
        time.sleep(delay / steps)
        ctx.progress((index + 1) / steps, f"step {index + 1}/{steps}")
    if ctx.payload.get("fail"):
        raise JobFailed(str(ctx.payload["fail"]), retry=bool(ctx.payload.get("retry")))
    contents = {name: path.read_text(encoding="utf-8", errors="replace") for name, path in ctx.inputs.items()}
    if ctx.payload.get("write"):
        out = ctx.workdir / "out.txt"
        out.write_text(str(ctx.payload["write"]), encoding="utf-8")
        ctx.output(out)
    return {"echo": ctx.payload.get("value"), "inputs": contents, "slot": ctx.slot.get("id"),
            "env": {k: ctx.env.get(k) for k in ctx.payload.get("envKeys") or []}}


def _shell(ctx: JobContext) -> dict[str, Any]:
    argv = ctx.payload.get("argv")
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if not isinstance(argv, list) or not argv:
        raise JobFailed("shell jobs need payload.argv")
    tail: list[str] = []

    def line(text: str) -> None:
        tail.append(text)
        del tail[:-200]
        ctx.log(text)

    code = ctx.run([str(a) for a in argv], env={k: str(v) for k, v in (ctx.payload.get("env") or {}).items()},
                   on_line=line, timeout=ctx.payload.get("timeout"))
    for pattern in ctx.payload.get("outputs") or []:
        for match in sorted(glob.glob(str(ctx.workdir / pattern), recursive=True)):
            path = Path(match)
            if path.is_file():
                ctx.output(path, os.path.relpath(path, ctx.workdir))
    if code != 0:
        raise JobFailed(f"exit {code}: " + "\n".join(tail[-20:]))
    return {"exitCode": code, "tail": tail[-50:]}


def _farm_batch(ctx: JobContext) -> dict[str, Any]:
    from eks_harness.video.farm.nodes import node_farm_batch

    return node_farm_batch(ctx)


BUILTIN: dict[str, Handler] = {"probe": _probe, "echo": _echo, "shell": _shell, "farm.batch": _farm_batch}


def plugin_handlers() -> dict[str, Handler]:
    try:
        from eks_harness.config import load as load_config
        from eks_harness.paths import resolve_paths
        from eks_harness.plugins import PluginHost

        paths = resolve_paths()
        host = PluginHost(paths, load_config(paths))
        found: dict[str, Handler] = {}
        for contribution, target in host.load_all("node_job"):
            handler = getattr(target, "run", target)
            if callable(handler):
                found[contribution.id] = handler
        return found
    except Exception:
        return {}
