from __future__ import annotations

import base64
import binascii
import itertools
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any

import httpx
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from eks_harness.live.common import LiveError, StreamSettings
from eks_harness.pools.base import LiveSource

log = logging.getLogger("eks_harness.live")

REQUEST_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
NO_PAGE_TIMEOUT = 60.0
SKIPPED_URL_PREFIXES = ("devtools://", "chrome-extension://", "chrome-untrusted://")


class CdpError(LiveError):
    pass


def browser_websocket_url(cdp_url: str, timeout: float = CONNECT_TIMEOUT) -> str:
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url
    try:
        response = httpx.get(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout, trust_env=False)
        response.raise_for_status()
        url = response.json().get("webSocketDebuggerUrl")
    except (httpx.HTTPError, ValueError) as error:
        raise LiveError(f"Chrome did not answer on {cdp_url}: {error}") from error
    if not url:
        raise LiveError(f"Chrome at {cdp_url} reported no DevTools websocket")
    return url


def list_pages(cdp_url: str, context_ids: Iterable[str] | None = None, timeout: float = 3.0) -> list[dict]:
    url = browser_websocket_url(cdp_url, timeout)
    wanted = frozenset(context_ids) if context_ids else None
    try:
        with connect(url, open_timeout=timeout, close_timeout=1, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Target.getTargets", "params": {}}))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LiveError(f"Chrome at {cdp_url} did not list its targets within {timeout:.0f}s")
                message = json.loads(ws.recv(timeout=remaining))
                if message.get("id") == 1:
                    break
    except (OSError, TimeoutError, WebSocketException, ValueError) as error:
        raise LiveError(f"could not list the pages of Chrome at {cdp_url}: {error}") from error
    if "error" in message:
        raise LiveError(f"Target.getTargets failed: {message['error']}")
    pages = []
    for info in (message.get("result") or {}).get("targetInfos") or []:
        if info.get("type") != "page" or str(info.get("url", "")).startswith(SKIPPED_URL_PREFIXES):
            continue
        if wanted is not None and info.get("browserContextId") not in wanted:
            continue
        if wanted is None and not info.get("browserContextId"):
            continue
        pages.append({"targetId": info.get("targetId"), "url": info.get("url"), "title": info.get("title"),
                      "attached": bool(info.get("attached")), "browserContextId": info.get("browserContextId")})
    return pages


def dispose_contexts(cdp_url: str, context_ids: Iterable[str], timeout: float = 10.0) -> list[str]:
    wanted = [str(c) for c in context_ids if c]
    if not wanted:
        return []
    url = browser_websocket_url(cdp_url, timeout)
    disposed: list[str] = []
    try:
        with connect(url, open_timeout=timeout, close_timeout=1, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Target.getBrowserContexts", "params": {}}))
            existing = set((_await_reply(ws, 1, timeout).get("result") or {}).get("browserContextIds") or [])
            for number, context_id in enumerate(wanted, start=2):
                if context_id not in existing:
                    continue
                ws.send(json.dumps({"id": number, "method": "Target.disposeBrowserContext",
                                    "params": {"browserContextId": context_id}}))
                reply = _await_reply(ws, number, timeout)
                if "error" in reply:
                    raise LiveError(f"Target.disposeBrowserContext failed for {context_id}: {reply['error']}")
                disposed.append(context_id)
    except (OSError, TimeoutError, WebSocketException, ValueError) as error:
        raise LiveError(f"could not close the browser contexts at {cdp_url}: {error}") from error
    return disposed


def _await_reply(ws: ClientConnection, message_id: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveError(f"Chrome did not answer request {message_id} within {timeout:.0f}s")
        message = json.loads(ws.recv(timeout=remaining))
        if message.get("id") == message_id:
            return message


class ChromeScreencastSource(LiveSource):
    def __init__(self, cdp_url: str, settings: StreamSettings, *, name: str,
                 context_ids: Iterable[str] | None = None, target_id: str | None = None,
                 no_page_timeout: float = NO_PAGE_TIMEOUT, exclusive: bool = False) -> None:
        self.cdp_url = cdp_url
        self.settings = settings
        self.name = name
        self.context_ids = frozenset(context_ids) if context_ids else None
        self.requested_target = target_id
        self.no_page_timeout = no_page_timeout
        self.target_id: str | None = None
        self.switches = 0
        self._ws: ClientConnection | None = None
        self._ids = itertools.count(1)
        self._backlog: deque[dict] = deque()
        self._pages: dict[str, dict] = {}
        self._order = itertools.count()
        self._session: str | None = None
        self._known_default: set[str] = set()
        self.exclusive = exclusive
        self._any_context = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        message_id = next(self._ids)
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._ws.send(json.dumps(message))
        return message_id

    def _receive(self, timeout: float) -> dict | None:
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except ConnectionClosed as error:
            if self._stop.is_set():
                return None
            raise LiveError(f"Chrome closed the DevTools connection of {self.name}") from error
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def _request(self, method: str, params: dict | None = None, session: str | None = None,
                 timeout: float = REQUEST_TIMEOUT) -> dict:
        message_id = self._send(method, params, session)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(f"Chrome did not answer {method} within {timeout:.0f}s")
            if self._stop.is_set():
                raise LiveError("stopped")
            message = self._receive(min(remaining, 0.5))
            if message is None:
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    error = message["error"]
                    raise CdpError(f"{method} failed: {error.get('message', error)}")
                return message.get("result") or {}
            if "method" in message:
                self._backlog.append(message)

    def _eligible(self, info: dict) -> bool:
        if info.get("type") != "page":
            return False
        if str(info.get("url", "")).startswith(SKIPPED_URL_PREFIXES):
            return False
        context = info.get("browserContextId")
        if self._any_context:
            return True
        if self.context_ids is None:
            return bool(context) and context not in self._known_default
        return context in self.context_ids

    def _track(self, info: dict) -> None:
        if self._eligible(info):
            self._pages[info["targetId"]] = {**info, "_order": next(self._order)}

    def _pick(self) -> str | None:
        if self.requested_target and self.requested_target in self._pages:
            return self.requested_target
        if not self._pages:
            return None
        return max(self._pages.values(), key=lambda p: p["_order"])["targetId"]

    def _attach(self, target_id: str) -> None:
        result = self._request("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session = result.get("sessionId")
        if not session:
            raise CdpError(f"Chrome gave no session for page {target_id}")
        self._session = session
        self.target_id = target_id
        edge = int(self.settings.max_edge)
        self._request("Page.startScreencast", {"format": "jpeg", "quality": int(self.settings.quality),
                                               "maxWidth": edge, "maxHeight": edge, "everyNthFrame": 1},
                      session=session)
        log.info("live view of %s follows page %s (%s)", self.name, target_id,
                 self._pages.get(target_id, {}).get("url", ""))

    def _detach(self) -> None:
        session, self._session = self._session, None
        self.target_id = None
        if session is None or self._ws is None:
            return
        for method, params, target in (("Page.stopScreencast", {}, session),
                                       ("Target.detachFromTarget", {"sessionId": session}, None)):
            try:
                self._send(method, params, target)
            except (ConnectionClosed, OSError, RuntimeError):
                return

    def _switch(self) -> bool:
        self._detach()
        while self._pages:
            target = self._pick()
            try:
                self._attach(target)
                self.switches += 1
                return True
            except CdpError as error:
                log.info("page %s of %s cannot be shown: %s", target, self.name, error)
                self._pages.pop(target, None)
                self._detach()
        return False

    def _handle(self, message: dict) -> bytes | None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Page.screencastFrame":
            if message.get("sessionId") != self._session or self._session is None:
                return None
            try:
                self._send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, self._session)
            except (ConnectionClosed, OSError, RuntimeError) as error:
                raise LiveError(f"Chrome closed the DevTools connection of {self.name}") from error
            try:
                return base64.b64decode(params.get("data") or "", validate=True)
            except (binascii.Error, ValueError):
                return None
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            known = info.get("targetId") in self._pages
            self._track(info)
            if not known and info.get("targetId") in self._pages and not self.requested_target:
                self._switch()
            return None
        if method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            target = info.get("targetId")
            if target in self._pages:
                self._pages[target].update(info)
            elif self._eligible(info):
                self._track(info)
                if self._session is None:
                    self._switch()
            return None
        if method == "Target.targetDestroyed":
            target = params.get("targetId")
            self._pages.pop(target, None)
            if target == self.target_id:
                self._switch()
            return None
        if method == "Target.detachedFromTarget":
            if params.get("sessionId") == self._session:
                self._session = None
                self._pages.pop(self.target_id, None)
                self.target_id = None
                self._switch()
            return None
        return None

    def frames(self) -> Iterator[bytes]:
        url = browser_websocket_url(self.cdp_url)
        try:
            with connect(url, max_size=None, open_timeout=CONNECT_TIMEOUT, close_timeout=2, proxy=None,
                         compression=None) as ws:
                with self._lock:
                    self._ws = ws
                    stopped = self._stop.is_set()
                if stopped:
                    return
                try:
                    yield from self._screencast()
                finally:
                    self._detach()
        except LiveError:
            raise
        except (OSError, TimeoutError, WebSocketException) as error:
            if self._stop.is_set():
                return
            raise LiveError(f"the DevTools connection of {self.name} failed: {error}") from error

    def _screencast(self) -> Iterator[bytes]:
        live = frozenset(self._request("Target.getBrowserContexts").get("browserContextIds") or [])
        if self.context_ids is not None and not self.context_ids & live:
            if not self.exclusive:
                raise LiveError(f"the browser contexts the harness reported for {self.name} no longer exist (the "
                                f"browser restarted after the lease opened them); release the lease and acquire it "
                                f"again")
            log.warning("the browser contexts reported for %s no longer exist; showing any page of the browser",
                        self.name)
            self.context_ids = None
            self._any_context = True
        created = live if self.context_ids is None else self.context_ids
        self._request("Target.setDiscoverTargets", {"discover": True})
        for info in self._request("Target.getTargets").get("targetInfos") or []:
            context = info.get("browserContextId")
            if self.context_ids is None and context and context not in created:
                self._known_default.add(context)
            self._track(info)
        if self.requested_target and self.requested_target not in self._pages:
            raise LiveError(f"page {self.requested_target} is not open in {self.name}")
        self._switch()
        waiting_since = time.monotonic() if self._session is None else None
        while not self._stop.is_set():
            message = self._backlog.popleft() if self._backlog else self._receive(0.5)
            if message is None:
                if self._session is None:
                    waiting_since = waiting_since or time.monotonic()
                    if time.monotonic() - waiting_since > self.no_page_timeout:
                        raise LiveError(f"{self.name} has no open page to show")
                continue
            if "method" not in message:
                continue
            frame = self._handle(message)
            if self._session is not None:
                waiting_since = None
            if frame:
                yield frame

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
