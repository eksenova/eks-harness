from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

CALL = re.compile(r"return await ([\w.]+)\((.*)\)\s*$", re.S)


class FakeWorker:
    def __init__(self, answers: dict[str, Callable[[list[Any]], Any] | Any] | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, list[Any], dict]] = []
        self.events: list[dict] = []
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path.startswith("/health"):
                    return self._send(200, {"ok": True, "devtools": {"ios": {"connected": True}}})
                if self.path.startswith("/events"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in worker.events:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    return None
                return self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/status":
                    return self._send(200, {"up": True})
                if self.path != "/exec":
                    return self._send(404, {"error": "not found"})
                script = body.get("script", "")
                match = CALL.match(script.strip())
                helper, args = (match.group(1), json.loads(f"[{match.group(2)}]")) if match else ("<script>", [script])
                worker.calls.append((helper, args, body))
                answer = worker.answers.get(helper)
                try:
                    value = answer(args) if callable(answer) else answer
                except Exception as error:
                    return self._send(500, {"ok": False, "error": str(error)})
                return self._send(200, {"ok": True, "value": value, "steps": [{"name": helper}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def helpers(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def __enter__(self) -> FakeWorker:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
