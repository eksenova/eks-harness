from __future__ import annotations

import os
import re
from typing import Any

from eks_harness.flows.client import Artifact
from eks_harness.flows.core import App, Target, opts


class MobileApp(App):
    kind = "mobile"
    platform = "ios"

    def __init__(self, *args: Any, platform: str | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.platform = platform or os.environ.get("EHX_PLATFORM") or "ios"
        self.kind = f"mobile-{self.platform}"
        self.log_mark = 0

    def body(self, script: str) -> dict:
        return {"script": script, "sid": self.sid, "platform": self.platform}

    def healthy(self) -> bool:
        if self.worker is None:
            return False
        status, res = self.worker.get("/health", 2)
        if status != 200 or not isinstance(res, dict):
            return False
        return bool(((res.get("devtools") or {}).get(self.platform) or {}).get("connected")) and bool(self.sid)

    def press(self, target: Target, *, long: bool | None = None) -> Any:
        return self.call("press", target, opts(long=long))

    def fill(self, target: Target, text: str, *, submit: bool | None = None, pace: str | None = None) -> Any:
        return self.call("fill", target, text, opts(submit=submit, pace=pace))

    def type(self, target: Target, text: str, *, submit: bool | None = None, pace: str | None = None) -> Any:
        return self.call("type", target, text, opts(submit=submit, pace=pace))

    def submit(self, target: Target) -> Any:
        return self.call("submit", target)

    def toggle(self, target: Target, value: bool | None = None) -> Any:
        return self.call("toggle", target, value)

    def scroll(self, target: Target | None = None, *, to: str | None = None, offset: int | None = None,
               index: int | None = None) -> Any:
        return self.call("scroll", target, opts(to=to, offset=offset, index=index))

    def navigate(self, name: str, params: dict | None = None) -> Any:
        return self.call("navigate", name, params)

    def go_back(self) -> Any:
        return self.call("goBack")

    def route(self) -> Any:
        return self.call("route")

    def tree(self, *, all: bool | None = None) -> str:
        return self.call("tree", opts(all=all))

    def text(self, target: Target | None = None) -> str:
        return self.call("text", target)

    def exists(self, target: Target) -> bool:
        return self.call("exists", target)

    def wait_for(self, target: Target, *, timeout: float = 10) -> Any:
        return self.call("waitFor", target, {"timeout": int(timeout * 1000)})

    def wait_gone(self, target: Target, *, timeout: float = 10) -> Any:
        return self.call("waitGone", target, {"timeout": int(timeout * 1000)})

    def idle(self) -> Any:
        return self.call("idle")

    def state(self, path: str | None = None) -> Any:
        return self.call("state", path)

    def dispatch(self, action: dict) -> Any:
        return self.call("dispatch", action)

    def js(self, code: str) -> Any:
        return self.call("evaluate", code, label="js")

    def reload(self) -> Any:
        return self.call("reload", timeout=60)

    def login(self, *args: Any, **kwargs: Any) -> Any:
        return self.call("login", *args, opts(**kwargs), timeout=120)

    def arm(self, slot: str, **spec: Any) -> Any:
        return self.call("arm", slot, spec or None)

    def clear_fakes(self) -> Any:
        return self.call("clearFakes")

    def patch(self, target: Target, *, props: dict | None = None, style: dict | None = None,
              text: str | None = None, key: str | None = None) -> Any:
        return self.call("patch", target, opts(props=props, style=style, text=text, key=key))

    def unpatch(self, key: str) -> Any:
        return self.call("unpatch", key)

    def inject(self, component: str, *, props: dict | None = None, target: Target | None = None,
               key: str | None = None) -> Any:
        return self.call("inject", opts(component=component, props=props, target=target, key=key))

    def logout(self) -> Any:
        return self.call("logout", timeout=30)

    def pointer(self, target: Target | None = None, *, ms: int | None = None) -> Any:
        return self.call("pointer", target, opts(ms=ms))

    def shot(self, name: str, *, caption: str | None = None, **_: Any) -> list[Artifact]:
        value = self.call("shot", name, opts(caption=caption))
        art = Artifact(kind="screenshot", name=name, id=(value or {}).get("id"), url=(value or {}).get("url"),
                       raw=(value or {}).get("rawUrl"), session=(value or {}).get("sessionUrl"))
        self.artifacts.append(art)
        return [art]

    def checks(self) -> dict:
        info: dict[str, Any] = {}
        try:
            route = self.route()
            info["route"] = route.get("name") if isinstance(route, dict) else route
        except Exception as error:
            info["route"] = f"unavailable: {error}"
        lines = self.call("logs", 200, label="logs") or []
        fresh = lines[self.log_mark:] if self.log_mark <= len(lines) else lines
        self.log_mark = len(lines)
        errors = [re.sub(r"^\S+\s+", "", l)[:240] for l in fresh
                  if f"[{self.platform}]" in l or "[" not in l[:30]
                  if re.search(r"\[(error)\]|Error:|exception", l, re.I)]
        if errors:
            info["errors"] = errors[-6:]
        return info

    def start_recording(self, name: str, *, pace: str | dict | None = None, from_here: bool | None = None,
                        caption: str | None = None, hold: int | None = None, no_pointer: bool | None = None,
                        **_: Any) -> Any:
        return self.call("video.start", name, opts(pace=pace, fromHere=from_here, caption=caption, hold=hold,
                                                   noPointer=no_pointer), timeout=60)

    def stop_recording(self, name: str, *, trim: bool | None = None) -> Artifact | None:
        value = self.call("video.stop", opts(trim=trim), timeout=900) or {}
        art = Artifact(kind="video", name=name, id=value.get("id"), url=value.get("url"), raw=value.get("rawUrl"),
                       session=value.get("sessionUrl"))
        art.extra = {k: v for k, v in {"durationSeconds": value.get("durationSeconds") or value.get("duration"),
                                       "warnings": value.get("warnings"),
                                       "annotations": len(value.get("annotations") or [])}.items() if v}
        art.marks = value.get("annotations") or []
        return art
