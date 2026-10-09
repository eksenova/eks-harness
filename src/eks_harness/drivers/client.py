from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


class WorkerError(RuntimeError):
    pass


def js_args(*args: Any) -> str:
    while args and args[-1] is None:
        args = args[:-1]
    return ", ".join(json.dumps(a, ensure_ascii=False) for a in args)


class WorkerClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, route: str, body: dict | None = None, timeout: float = 600) -> tuple[int, Any]:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(self.base_url + route, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return res.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read() or b"{}")
            except json.JSONDecodeError:
                return err.code, {"error": str(err)}
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            return 0, {"error": f"driver worker not reachable at {self.base_url}: {err}"}

    def post(self, route: str, body: dict | None = None, timeout: float = 600) -> tuple[int, Any]:
        return self.request("POST", route, body, timeout)

    def get(self, route: str, timeout: float = 10) -> tuple[int, Any]:
        return self.request("GET", route, None, timeout)

    def alive(self, kind: str) -> bool:
        status, _ = self.post("/status", {}, 5) if kind == "web" else self.get("/health", 5)
        return status == 200

    def exec(self, script: str, *, sid: str | None = None, platform: str | None = None,
             timeout: float = 600) -> dict[str, Any]:
        body: dict[str, Any] = {"script": script}
        if sid:
            body["sid"] = sid
        if platform:
            body["platform"] = platform
        status, result = self.post("/exec", body, timeout)
        if not isinstance(result, dict):
            result = {"ok": False, "error": f"unexpected answer: {result!r}"[:300]}
        if status != 200 and "ok" not in result:
            result = {**result, "ok": False}
        return result

    def call(self, helper: str, *args: Any, sid: str | None = None, platform: str | None = None,
             timeout: float = 600) -> Any:
        result = self.exec(f"return await {helper}({js_args(*args)})", sid=sid, platform=platform, timeout=timeout)
        if not result.get("ok"):
            raise WorkerError(f"{helper}: {result.get('error') or 'failed'}")
        return result.get("value")

    def events(self, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        req = urllib.request.Request(self.base_url + "/events", headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            buffer = ""
            while True:
                chunk = res.readline()
                if not chunk:
                    return
                line = chunk.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("data: "):
                    buffer += line[6:]
                elif line == "" and buffer:
                    try:
                        yield json.loads(buffer)
                    except json.JSONDecodeError:
                        pass
                    buffer = ""
