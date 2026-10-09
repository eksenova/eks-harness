from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eks_harness.drivers.client import WorkerClient
from eks_harness.drivers.profile import AppProfile
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

    def prepare(self) -> None:
        return None

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
