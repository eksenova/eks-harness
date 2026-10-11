from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eks_harness.drivers.client import WorkerClient
from eks_harness.drivers.profile import AppProfile, load_profile
from eks_harness.flows.client import (
    Artifact,
    Step,
    StepError,
    Timer,
    artifact_of,
    contact_sheet,
    download,
    stored_sheet,
)

Target = str | dict
BACKEND_POLL = 25.0


class BackendLinkError(RuntimeError):
    pass


def backend_env(link: dict | None) -> dict[str, str]:
    if not link:
        return {}
    info = link.get("info") or {}
    env = {"EKS_BACKEND_ID": str((link.get("backend") or {}).get("id") or ""),
           "EKS_BACKEND_LEASE": str((link.get("lease") or {}).get("id") or ""),
           "EKS_BACKEND_URL": str(info.get("url") or "")}
    for name, url in (info.get("urls") or {}).items():
        env[f"EKS_BACKEND_URL_{re.sub(r'[^A-Za-z0-9]+', '_', str(name)).upper()}"] = str(url)
    env.update({str(k): str(v) for k, v in (info.get("env") or {}).items()})
    return env


def worker_backend(link: dict | None) -> dict[str, Any] | None:
    if not link:
        return None
    backend = link.get("backend") or {}
    return {**(link.get("info") or {}), "id": backend.get("id"), "lease": (link.get("lease") or {}).get("id"),
            "generation": backend.get("generation") or 0, "status": backend.get("status"), "sid": link.get("sid")}


def js_args(*args: Any) -> str:
    while args and args[-1] is None:
        args = args[:-1]
    return ", ".join(json.dumps(a, ensure_ascii=False) for a in args)


def opts(**kwargs: Any) -> dict | None:
    clean = {k: v for k, v in kwargs.items() if v is not None}
    return clean or None


def ms(seconds: float | None) -> int | None:
    return None if seconds is None else int(seconds * 1000)


class App:
    kind = "app"
    platform = "web"

    def __init__(self, profile: AppProfile, *, worker: WorkerClient | None = None, sid: str = "",
                 harness: Any = None, verbose: bool = False, out: Path | None = None,
                 params: dict[str, str] | None = None) -> None:
        self.profile = profile
        self.tree = profile.root
        self.worker = worker
        self.sid = sid
        self.harness = harness
        self.verbose = verbose
        self.timer = Timer()
        self.steps: list[Step] = []
        self.artifacts: list[Artifact] = []
        self.checkpoints: list[dict] = []
        self.notes: list[str] = []
        self.recording: str | None = None
        self.out = out or Path(tempfile.mkdtemp(prefix=f"ehx-flow-{self.kind}-"))
        self.params: dict[str, str] = dict(params or {})
        self.harness_dir: Path | None = None
        self.backend: Any = None
        self.backend_link: dict[str, Any] | None = None

    def prepare(self) -> None:
        return None

    def link_backend(self, backend: str, *, tree: str | Path | None = None, key: str | None = None,
                     params: dict[str, Any] | None = None, wait: float = 3600.0) -> Any:
        if self.harness is None or not self.sid:
            raise BackendLinkError("link_backend needs a leased app; run the flow with eks-harness flow run")
        body = {"backend": backend, "tree": str(tree) if tree else None, "key": key, "params": params}
        self.harness.post(f"/api/leases/{self.sid}/backend", json=body, timeout=600)
        return self.refresh_backend(wait)

    def refresh_backend(self, wait: float = 0.0) -> Any:
        if self.harness is None or not self.sid:
            return None
        deadline = time.time() + max(0.0, wait)
        while True:
            remaining = max(0.0, deadline - time.time())
            data = self.harness.get(f"/api/leases/{self.sid}/backend",
                                    params={"secrets": "true", "waitSeconds": round(min(BACKEND_POLL, remaining), 1)},
                                    timeout=BACKEND_POLL + 60)
            backend = data.get("backend") or {}
            status = backend.get("status")
            if status == "running":
                break
            if status == "failed":
                raise BackendLinkError(f"backend {backend.get('id')} failed: "
                                       f"{str(backend.get('error') or '').splitlines()[:1]}")
            if time.time() >= deadline:
                raise BackendLinkError(f"backend {backend.get('id')} is {status}, not running, after {wait:.0f}s")
        self.use_backend(data)
        return self.backend

    def use_backend(self, data: dict[str, Any]) -> None:
        from eks_harness.plugins.sdk import BackendInfo

        self.backend_link = data
        self.backend = BackendInfo.from_dict(data.get("info") or {})
        if self.harness_dir is not None:
            fresh = load_profile(self.harness_dir, env={**os.environ, **backend_env(data)})
            self.profile.web, self.profile.mobile, self.profile.checks = fresh.web, fresh.mobile, fresh.checks

    def boot(self) -> None:
        return None

    def worker_options(self) -> dict[str, Any]:
        return {}

    def body(self, script: str) -> dict:
        return {"script": script, "sid": self.sid}

    def exec(self, script: str, label: str | None = None, timeout: float = 600) -> Any:
        if self.worker is None:
            raise StepError(label or "exec", "no driver worker is attached to this app")
        started = time.monotonic()
        status, res = self.worker.post("/exec", self.body(script), timeout)
        took = int((time.monotonic() - started) * 1000)
        name = label or script.strip().split("(")[0].replace("return await ", "")[:40]
        ok = status == 200 and isinstance(res, dict) and res.get("ok", False)
        error = None if ok else ((res or {}).get("error") if isinstance(res, dict) else None) or f"HTTP {status}"
        self.steps.append(Step(name=name, args=script[:160], ms=took, ok=ok, error=error))
        if self.verbose:
            print(f"  {'ok ' if ok else 'ERR'} {took:6d}ms {name}" + (f"  {error}" if error else ""), flush=True)
        if not ok:
            raise StepError(name, str(error), res if isinstance(res, dict) else {})
        return res.get("raw", res.get("value"))

    def call(self, helper: str, *args: Any, label: str | None = None, timeout: float = 600) -> Any:
        return self.exec(f"return await {helper}({js_args(*args)})", label=label or helper, timeout=timeout)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def sleep(self, seconds: float) -> None:
        self.call("sleep", int(seconds * 1000), label="sleep")

    def emit(self, name: str, data: Any = None) -> Any:
        return self.call("emit", name, data)

    def title(self, title: str, subtitle: str | None = None, *, kicker: str | None = None, hold: float | None = None,
              keep: bool = False) -> Any:
        return self.call("annotate.title", title, opts(subtitle=subtitle, kicker=kicker, hold=ms(hold), keep=keep))

    def caption(self, text: str | None, *, step: int | str | None = None, position: str | None = None,
                hold: float | None = None, keep: bool = True) -> Any:
        return self.call("annotate.caption", text, opts(step=step, position=position, hold=ms(hold), keep=keep))

    def callout(self, target: Target, text: str, *, placement: str | None = None, hold: float | None = None,
                keep: bool = False, key: str | None = None) -> Any:
        return self.call("annotate.callout", target, text, opts(placement=placement, hold=ms(hold), keep=keep, key=key))

    def highlight(self, target: Target, *, hold: float | None = None, keep: bool = False,
                  key: str | None = None) -> Any:
        return self.call("annotate.highlight", target, opts(hold=ms(hold), keep=keep, key=key))

    def spotlight(self, target: Target, text: str | None = None, *, placement: str | None = None,
                  hold: float | None = None, keep: bool = False, key: str | None = None) -> Any:
        return self.call("annotate.spotlight", target,
                         opts(text=text, placement=placement, hold=ms(hold), keep=keep, key=key))

    def unannotate(self, key: str | None = None) -> Any:
        return self.call("annotate.remove", key) if key else self.call("annotate.clear")

    def shot(self, name: str, **kwargs: Any) -> list[Artifact]:
        raise NotImplementedError

    def checks(self) -> dict:
        return {}

    def checkpoint(self, label: str, *, shot: bool = True, **shot_kwargs: Any) -> dict:
        entry: dict[str, Any] = {"label": label, "atMs": self.timer.ms()}
        entry.update(self.checks())
        if shot:
            entry["shots"] = [a.name for a in self.shot(label, **shot_kwargs)]
        self.checkpoints.append(entry)
        return entry

    def start_recording(self, name: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stop_recording(self, name: str, **kwargs: Any) -> Artifact | None:
        raise NotImplementedError

    @contextmanager
    def recording_of(self, name: str, *, title: str | None = None, subtitle: str | None = None,
                     kicker: str | None = None, **kwargs: Any) -> Iterator[App]:
        if title and "hold" not in kwargs:
            kwargs["hold"] = 0
        self.start_recording(name, **kwargs)
        self.recording = name
        try:
            if title:
                self.title(title, subtitle, kicker=kicker)
            yield self
        finally:
            self.recording = None
            art = self.stop_recording(name)
            if art:
                self.artifacts.append(art)

    def add(self, kind: str, name: str, value: Any) -> Artifact | None:
        art = artifact_of(kind, name, value)
        if art:
            self.artifacts.append(art)
        return art

    def failure(self, error: StepError) -> dict:
        info: dict[str, Any] = {"step": error.step, "error": error.message}
        try:
            info.update(self.checks())
            info["shots"] = [a.name for a in self.shot("failure")]
        except Exception as extra:
            info["captureError"] = str(extra)[:300]
        return info

    def collect(self, fetch: bool = True, sheet: bool = True) -> None:
        if not fetch or self.harness is None:
            return
        ids = [a.id for a in self.artifacts if a.id and not a.local]
        found = download(self.harness, [i for i in ids if i], self.out)
        for art in self.artifacts:
            if art.id and art.id in found:
                art.local = str(found[art.id])
                if sheet and art.kind == "video":
                    try:
                        stored = stored_sheet(self.harness, art, self.out)
                    except Exception as error:
                        self.notes.append(f"sheet of {art.name}: {error}")
                        stored = False
                    if not stored:
                        made = contact_sheet(found[art.id], self.out / f"{art.id}-sheet.png", art.marks)
                        if made:
                            art.sheet = str(made)

    def report(self, *, failure: dict | None = None, value: Any = None) -> dict:
        slow = sorted(self.steps, key=lambda s: -s.ms)[:5]
        return {
            "app": self.kind,
            "platform": self.platform,
            "sid": self.sid,
            "ok": failure is None,
            "elapsedMs": self.timer.ms(),
            "steps": len(self.steps),
            "slowest": [f"{s.name} {s.ms}ms" for s in slow],
            "failure": failure,
            "checkpoints": self.checkpoints,
            "artifacts": [{k: v for k, v in vars(a).items() if v and k not in ("extra", "marks")} | a.extra
                          for a in self.artifacts],
            "notes": self.notes,
            "value": value,
        }
