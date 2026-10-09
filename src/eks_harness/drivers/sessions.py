from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from eks_harness.drivers.client import WorkerClient, WorkerError

WEB_ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "click": ("click", ("target",)),
    "press": ("click", ("target",)),
    "tap": ("click", ("target",)),
    "dblclick": ("dblclick", ("target",)),
    "fill": ("fill", ("target", "text")),
    "type": ("type", ("target", "text")),
    "key": ("press", ("key", "target")),
    "hover": ("hover", ("target",)),
    "scroll": ("wheel", ("target",)),
    "select": ("select", ("target", "value")),
    "check": ("check", ("target", "on")),
    "choose": ("choose", ("target", "option")),
    "navigate": ("nav", ("route",)),
    "goto": ("goto", ("route",)),
    "open": ("goto", ("url",)),
    "back": ("back", ()),
    "reload": ("reload", ()),
    "emit": ("emit", ("name", "data")),
}

MOBILE_ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "press": ("press", ("target",)),
    "tap": ("press", ("target",)),
    "click": ("press", ("target",)),
    "fill": ("fill", ("target", "text")),
    "type": ("type", ("target", "text")),
    "submit": ("submit", ("target",)),
    "toggle": ("toggle", ("target", "value")),
    "scroll": ("scroll", ("target",)),
    "navigate": ("navigate", ("route", "params")),
    "back": ("goBack", ()),
    "dispatch": ("dispatch", ("action",)),
    "patch": ("patch", ("target",)),
    "unpatch": ("unpatch", ("key",)),
    "inject": ("inject", ()),
    "arm": ("arm", ("fake", "payload")),
    "emit": ("emit", ("name", "data")),
    "reload": ("reload", ()),
}

WEB_OBSERVE = {"text": "text", "exists": "exists", "visible": "visible", "count": "count", "value": "value",
               "tree": "tree", "url": "url", "logs": "logs"}
MOBILE_OBSERVE = {"text": "text", "exists": "exists", "tree": "tree", "route": "route", "state": "state",
                  "logs": "logs", "find": "find", "layout": "layout", "routes": "routeNames"}


class WorkerSession:
    def __init__(self, client: WorkerClient, *, platform: str, sid: str | None) -> None:
        self.client = client
        self.platform = platform
        self.sid = sid
        self.mobile = platform != "web"

    def _call(self, helper: str, *args: Any, timeout: float = 600) -> Any:
        return self.client.call(helper, *args, sid=self.sid, platform=self.platform if self.mobile else None,
                                timeout=timeout)

    def exec(self, script: str, timeout: float = 600) -> dict[str, Any]:
        return self.client.exec(script, sid=self.sid, platform=self.platform if self.mobile else None,
                                timeout=timeout)

    def act(self, action: str, **params: Any) -> dict[str, Any]:
        table = MOBILE_ACTIONS if self.mobile else WEB_ACTIONS
        if action == "evaluate":
            script = params.get("script") or params.get("code") or ""
            value = self._call("evaluate", script) if self.mobile else self._evaluate_web(script)
            return {"action": action, "value": value}
        if action == "overlay":
            kind = params.pop("kind", "caption")
            text = params.pop("text", None)
            target = params.pop("target", None)
            args = [target, text] if kind in ("callout",) else [text] if kind in ("caption", "title") else [target]
            return {"action": action, "value": self._call(f"annotate.{kind}", *args, params or None)}
        if action == "mark":
            return {"action": action, "value": self._call("annotate.caption", params.get("name"), {"hold": 0})}
        if action not in table:
            helper, order = action, ()
        else:
            helper, order = table[action]
        positional = [params.pop(name, None) for name in order]
        if action == "inject":
            positional = [params]
            params = {}
        while positional and positional[-1] is None:
            positional.pop()
        args = [*positional, params] if params else positional
        return {"action": action, "value": self._call(helper, *args)}

    def _evaluate_web(self, script: str) -> Any:
        result = self.exec(f"return await evaluate(() => {{ {script} }})")
        if not result.get("ok"):
            raise WorkerError(f"evaluate: {result.get('error')}")
        return result.get("value")

    def observe(self, query: str, **params: Any) -> dict[str, Any]:
        table = MOBILE_OBSERVE if self.mobile else WEB_OBSERVE
        helper = table.get(query, query)
        target = params.pop("target", None)
        args = [target] if target is not None else []
        if params:
            args.append(params)
        return {"query": query, "value": self._call(helper, *args)}

    def capture(self, kind: str, **params: Any) -> dict[str, Any]:
        name = params.pop("name", kind)
        if kind in ("screenshot", "shot"):
            return {"kind": kind, "value": self._call("shot", name, params or None)}
        if kind in ("video.start", "recording.start"):
            return {"kind": kind, "value": self._call("video.start", name, params or None)}
        if kind in ("video.stop", "recording.stop"):
            return {"kind": kind, "value": self._call("video.stop", params or None, timeout=900)}
        if kind in ("dom", "mhtml", "a11y") and not self.mobile:
            return {"kind": kind, "value": self._call("dump", kind, name, params or None)}
        if kind in ("har.start", "har.stop") and not self.mobile:
            return {"kind": kind, "value": self._call(kind, params or None)}
        raise WorkerError(f"{self.platform} cannot capture {kind}")

    def events(self, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        return self.client.events(timeout)

    def close(self) -> None:
        return None
