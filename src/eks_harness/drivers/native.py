from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from xml.etree import ElementTree

from pydantic import JsonValue

from eks_harness.engine.errors import DriverError
from eks_harness.pools.devices import tool

NativeKind = Literal["ios", "android"]
Runner = Callable[[Sequence[str], float], tuple[int, str]]

POLL = 0.25
DEFAULT_TIMEOUT = 10.0
LONG_PRESS_MS = 800
SWIPE_MS = 300
SWIPE_FRACTION = 0.6
ANDROID_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
ANDROID_TEXT_SAFE = re.compile(r"^[\x20-\x7e]*$")
ROLE_CLASSES = {
    "button": ("Button", "ImageButton"),
    "textbox": ("EditText", "TextField", "SecureTextField", "SearchField"),
    "searchbox": ("EditText", "SearchField"),
    "switch": ("Switch", "ToggleButton"),
    "checkbox": ("CheckBox", "Switch"),
    "link": ("Button", "Link"),
    "heading": ("TextView", "StaticText", "Heading"),
    "text": ("TextView", "StaticText"),
}
NATIVE_OPS = frozenset({"ui.tap", "ui.long_press", "ui.double_tap", "ui.fill", "ui.type", "ui.clear", "ui.swipe",
                        "ui.drag", "ui.exists", "ui.visible", "ui.count", "ui.text", "ui.wait_for", "ui.wait_gone",
                        "ui.tree", "expect.target", "ui.deeplink", "ui.open_url", "ui.settle", "check.checkpoint",
                        "ui.route"})
SETTLE_STEADY = 2
OPEN_PROMPT_WAIT = 6.0
OPEN_PROMPT_APPEAR = 3.0
TRANSLATION_RETRIES = 10
TRANSLATION_WAIT = 1.0
STALE_COMPANION = "has been invalidated"
DISMISS_BUTTONS = ("Don’t Allow", "Don't Allow", "Don’t allow", "Don't allow", "Cancel", "Not Now")


@dataclass(frozen=True, slots=True)
class NativeNode:
    kind: str
    text: str
    label: str
    ident: str
    value: str
    x: float
    y: float
    width: float
    height: float
    enabled: bool = True
    checked: bool | None = None
    visible: bool = True

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def contains(self, other: NativeNode) -> bool:
        return (other.x >= self.x and other.y >= self.y and other.x + other.width <= self.x + self.width
                and other.y + other.height <= self.y + self.height)

    def describe(self) -> str:
        name = self.text or self.label or self.ident
        return f"{self.kind}{f' #{self.ident}' if self.ident else ''}{f' {name!r}' if name else ''}"


def default_runner(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def parse_android(xml: str) -> list[NativeNode]:
    start = xml.find("<?xml")
    if start < 0:
        start = xml.find("<hierarchy")
    end = xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise DriverError("native_unavailable", f"uiautomator returned no hierarchy: {xml[-200:]}")
    root = ElementTree.fromstring(xml[start:end + len("</hierarchy>")])
    found: list[NativeNode] = []
    for element in root.iter("node"):
        bounds = ANDROID_BOUNDS.match(element.get("bounds", ""))
        if not bounds:
            continue
        left, top, right, bottom = (int(part) for part in bounds.groups())
        ident = element.get("resource-id", "")
        found.append(NativeNode(
            kind=element.get("class", "").rsplit(".", 1)[-1], text=element.get("text", ""),
            label=element.get("content-desc", ""), ident=ident.rsplit("/", 1)[-1], value=element.get("text", ""),
            x=left, y=top, width=max(0, right - left), height=max(0, bottom - top),
            enabled=element.get("enabled") != "false",
            checked=element.get("checked") == "true" if element.get("checkable") == "true" else None,
            visible=right > left and bottom > top))
    return found


def parse_ios(payload: str) -> list[NativeNode]:
    try:
        data = json.loads(payload)
    except ValueError as error:
        raise DriverError("native_unavailable", f"idb ui describe-all returned no JSON: {payload[-200:]}") from error
    found: list[NativeNode] = []
    for item in data if isinstance(data, list) else []:
        frame = item.get("frame") or {}
        role = str(item.get("type") or item.get("role") or "")
        value = item.get("AXValue")
        found.append(NativeNode(
            kind=role, text=str(item.get("AXLabel") or ""), label=str(item.get("AXLabel") or ""),
            ident=str(item.get("AXUniqueId") or ""), value=str(value) if value is not None else "",
            x=float(frame.get("x", 0)), y=float(frame.get("y", 0)), width=float(frame.get("width", 0)),
            height=float(frame.get("height", 0)), enabled=bool(item.get("enabled", True)),
            checked=(str(value) == "1") if role in ("Switch", "CheckBox") else None,
            visible=float(frame.get("width", 0)) > 0 and float(frame.get("height", 0)) > 0))
    return found


def _text_matches(value: str, needle: str, exact: bool) -> bool:
    if exact:
        return " ".join(value.split()) == " ".join(needle.split())
    return needle.casefold() in value.casefold()


def _matches(node: NativeNode, target: Mapping[str, Any]) -> bool:
    by = target.get("by")
    args = target.get("args") or {}
    exact = args.get("exact", by == "text") is not False
    if by == "test_id":
        return node.ident == str(args.get("id"))
    if by == "text":
        return _text_matches(node.text or node.label, str(args.get("text")), exact)
    if by == "label":
        return _text_matches(node.label or node.text, str(args.get("text")), bool(args.get("exact")))
    if by == "placeholder":
        return _text_matches(node.value, str(args.get("text")), bool(args.get("exact")))
    if by == "role":
        classes = ROLE_CLASSES.get(str(args.get("role")), ())
        if not any(node.kind.endswith(name) for name in classes):
            return False
        name = args.get("name")
        return name is None or _text_matches(node.text or node.label, str(name), bool(args.get("exact")))
    raise DriverError("target_unsupported", f"the native surface cannot find targets by {by}", detail={"target": dict(target)})


def _describe(target: Mapping[str, Any]) -> str:
    args = target.get("args") or {}
    head = f"#{args.get('id')}" if target.get("by") == "test_id" else f"{target.get('by')}={next(iter(args.values()), '')}"
    return f"{head} (native)"


@dataclass
class NativeSurface:
    kind: NativeKind
    udid: str | None = None
    serial: str | None = None
    runner: Runner = default_runner
    _idb: str | None = field(default=None, init=False)
    _checkpoint_at: float = field(default_factory=time.time, init=False)

    def _adb(self, *args: str, timeout: float = 30.0) -> str:
        adb = tool("adb")
        if not adb or not self.serial:
            raise DriverError("native_unavailable", "adb or the emulator serial is not available for the native surface")
        code, out = self.runner([adb, "-s", self.serial, *args], timeout)
        if code != 0:
            raise DriverError("native_unavailable", f"adb {' '.join(args[:3])} failed: {out[-300:]}")
        return out

    def _idb_run(self, *args: str, timeout: float = 30.0) -> str:
        idb = self._idb or shutil.which("idb")
        if not idb or not self.udid:
            raise DriverError("native_unavailable",
                              "the iOS native surface needs idb (brew install idb-companion and pip install fb-idb)")
        self._idb = idb
        code, out = self.runner([idb, *args, "--udid", self.udid], timeout)
        if code != 0 and STALE_COMPANION in out:
            self.runner([idb, "kill"], 30)
            code, out = self.runner([idb, *args, "--udid", self.udid], timeout)
        if code != 0:
            raise DriverError("native_unavailable", f"idb {' '.join(args[:2])} failed: {out[-300:]}")
        return out

    def nodes(self) -> list[NativeNode]:
        if self.kind == "android":
            return parse_android(self._adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=30))
        for attempt in range(TRANSLATION_RETRIES):
            try:
                return parse_ios(self._idb_run("ui", "describe-all", "--json", timeout=30))
            except DriverError as error:
                if "translation object" not in error.message or attempt == TRANSLATION_RETRIES - 1:
                    raise
                time.sleep(TRANSLATION_WAIT)
        return []

    def screen_size(self) -> tuple[float, float]:
        nodes = self.nodes()
        if not nodes:
            return 1080.0, 1920.0
        return max(node.x + node.width for node in nodes), max(node.y + node.height for node in nodes)

    def candidates(self, target: Mapping[str, Any], nodes: Sequence[NativeNode] | None = None) -> list[NativeNode]:
        pool = list(nodes if nodes is not None else self.nodes())
        within = target.get("within")
        if within:
            scope = self.pick(within, pool)
            pool = [node for node in pool if node is not scope and scope.contains(node)]
        found = [node for node in pool if node.visible and _matches(node, target)]
        inner = {id(node) for node in found for other in found if other is not node and other.contains(node)
                 and (other.text or other.label) == (node.text or node.label)}
        found = [node for node in found if id(node) not in inner]
        near = target.get("near")
        if near and found:
            anchor = self.pick(near, pool).centre
            found.sort(key=lambda node: math.hypot(node.centre[0] - anchor[0], node.centre[1] - anchor[1]))
        return found

    def pick(self, target: Mapping[str, Any], nodes: Sequence[NativeNode] | None = None) -> NativeNode:
        found = self.candidates(target, nodes)
        nth = target.get("nth")
        if not found:
            raise DriverError("target_not_found", f"no native element matches {_describe(target)}",
                              detail={"target": dict(target)}, retryable=True)
        if nth is None and not target.get("near"):
            if len(found) > 1:
                raise DriverError("target_ambiguous",
                                  f"{len(found)} native elements match {_describe(target)}; add nth or narrow it",
                                  detail={"count": len(found), "candidates": [node.describe() for node in found[:10]]})
            return found[0]
        index = int(nth or 0)
        index = len(found) + index if index < 0 else index
        if not 0 <= index < len(found):
            raise DriverError("target_not_found", f"{_describe(target)} asks for match {nth} but {len(found)} match",
                              retryable=True)
        return found[index]

    def point(self, target: Mapping[str, Any], timeout: float) -> tuple[float, float]:
        args = target.get("args") or {}
        if target.get("by") == "point":
            return float(args["x"]), float(args["y"])
        if target.get("by") == "rect":
            return float(args["x"]) + float(args["w"]) / 2, float(args["y"]) + float(args["h"]) / 2
        return self.wait(target, timeout).centre

    def wait(self, target: Mapping[str, Any], timeout: float) -> NativeNode:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.pick(target)
            except DriverError as error:
                if error.code != "target_not_found" or time.monotonic() >= deadline:
                    raise
            time.sleep(POLL)

    def wait_gone(self, target: Mapping[str, Any], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.candidates(target):
            if time.monotonic() >= deadline:
                raise DriverError("timeout", f"{_describe(target)} is still shown after {timeout:.1f} s", retryable=True)
            time.sleep(POLL)

    def tap(self, x: float, y: float, *, hold_ms: int = 0) -> None:
        if self.kind == "android":
            if hold_ms:
                self._adb("shell", "input", "swipe", *(str(round(v)) for v in (x, y, x, y)), str(hold_ms))
            else:
                self._adb("shell", "input", "tap", str(round(x)), str(round(y)))
            return
        args = ["ui", "tap", str(round(x)), str(round(y))]
        if hold_ms:
            args += ["--duration", f"{hold_ms / 1000:.2f}"]
        self._idb_run(*args)

    def swipe(self, start: tuple[float, float], end: tuple[float, float], ms: int = SWIPE_MS) -> None:
        if self.kind == "android":
            self._adb("shell", "input", "swipe", *(str(round(v)) for v in (*start, *end)), str(ms))
            return
        self._idb_run("ui", "swipe", *(str(round(v)) for v in (*start, *end)), "--duration", f"{ms / 1000:.2f}")

    def type_text(self, text: str) -> None:
        if self.kind == "android":
            if not ANDROID_TEXT_SAFE.match(text):
                raise DriverError("native_unavailable",
                                  "adb input text only types ASCII; type non-ASCII text such as Turkish letters on "
                                  "the app surface instead of .native")
            escaped = re.sub(r"([\\\"'`$&|;<>()*?#~ ])", lambda match: "%s" if match.group(1) == " " else "\\" + match.group(1), text)
            self._adb("shell", "input", "text", escaped)
            return
        self._idb_run("ui", "text", text)

    def clear_focused(self, length: int) -> None:
        if self.kind == "android":
            self._adb("shell", "input", "keyevent", "KEYCODE_MOVE_END")
            for _ in range(max(1, length)):
                self._adb("shell", "input", "keyevent", "KEYCODE_DEL")
            return
        for _ in range(max(1, length)):
            self._idb_run("ui", "key", "42")

    def open_url(self, url: str) -> None:
        if self.kind == "android":
            self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
            return
        if not self.udid:
            raise DriverError("native_unavailable", "the simulator udid is not known")
        code, out = self.runner(["xcrun", "simctl", "openurl", self.udid, url], 30)
        if code != 0:
            raise DriverError("navigation", f"simctl openurl failed: {out[-300:]}")
        self._confirm_open()

    def _confirm_open(self) -> None:
        appear = time.monotonic() + OPEN_PROMPT_APPEAR
        deadline = time.monotonic() + OPEN_PROMPT_WAIT
        tapped = False
        while time.monotonic() < (deadline if tapped else appear):
            try:
                nodes = self.nodes()
            except DriverError:
                return
            asking = any(node.text.startswith("Open in ") for node in nodes)
            button = next((node for node in nodes if node.kind.endswith("Button") and node.text == "Open"), None)
            if asking and button is not None:
                time.sleep(POLL)
                self.tap(*button.centre)
                tapped = True
            elif tapped:
                return
            time.sleep(POLL)
        if tapped:
            raise DriverError("navigation", "the system kept asking to open the link in the app")

    def set_permissions(self, policy: str, app: str, permissions: Sequence[str] = ()) -> None:
        if policy not in ("grant", "deny"):
            return
        if self.kind == "android":
            for permission in permissions or self.requested_permissions(app):
                self._adb("shell", "pm", "grant" if policy == "grant" else "revoke", app, permission)
            return
        if not self.udid:
            raise DriverError("native_unavailable", "the simulator udid is not known")
        code, out = self.runner(["xcrun", "simctl", "privacy", self.udid, "grant" if policy == "grant" else "revoke",
                                 "all", app], 30)
        if code != 0:
            raise DriverError("native_unavailable", f"simctl privacy failed: {out[-300:]}")

    def requested_permissions(self, package: str) -> list[str]:
        out = self._adb("shell", "dumpsys", "package", package)
        found: list[str] = []
        section = False
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("runtime permissions:"):
                section = True
                continue
            if section:
                match = re.match(r"([\w.]+): granted=", stripped)
                if not match:
                    if found:
                        break
                    continue
                found.append(match.group(1))
        return found

    def settle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        previous: str | None = None
        steady = 0
        while time.monotonic() < deadline:
            current = self.tree()
            steady = steady + 1 if current == previous else 0
            if steady >= SETTLE_STEADY - 1 and previous is not None:
                return True
            previous = current
            time.sleep(POLL)
        return False

    def foreground(self) -> str:
        if self.kind == "android":
            out = self._adb("shell", "dumpsys", "activity", "activities")
            for line in out.splitlines():
                if "mResumedActivity" in line or "topResumedActivity" in line:
                    match = re.search(r"\s([\w.]+/[\w.$]+)", line)
                    if match:
                        return match.group(1)
            return ""
        return ""

    def crashes_since(self, since: float) -> list[str]:
        if self.kind != "android":
            raise DriverError("native_unavailable", "crash logs are read from logcat on Android only")
        stamp = time.strftime("%m-%d %H:%M:%S.000", time.localtime(since))
        out = self._adb("logcat", "-d", "-b", "crash", "-T", stamp)
        return [line.strip()[:300] for line in out.splitlines() if " E " in line or "FATAL" in line]

    def checkpoint(self, checks: Sequence[Mapping[str, Any]], noise: Sequence[str]) -> list[dict[str, JsonValue]]:
        since = self._checkpoint_at
        self._checkpoint_at = time.time()
        outcomes: list[dict[str, JsonValue]] = []
        for use in checks:
            kind = str(use.get("kind"))
            params = use.get("params") or {}
            if kind == "page_errors" and self.kind == "android":
                ignored = [*noise, *(params.get("noise") or [])]
                found = [line for line in self.crashes_since(since) if not any(n and n in line for n in ignored)]
                outcomes.append({"kind": kind, "ok": not found, "severity": "error",
                                 "message": f"{len(found)} crash line(s): {' | '.join(found[-3:])}" if found
                                 else "no crashes", "detail": {"errors": list(found[-10:])}})
            elif kind == "route" and self.kind == "android":
                current = self.foreground()
                expected = str(params.get("expected") or "")
                pattern = re.compile("^" + ".*".join(re.escape(part) for part in expected.split("*")) + "$")
                ok = bool(pattern.match(current)) or current.endswith(expected)
                outcomes.append({"kind": kind, "ok": ok, "severity": "error",
                                 "message": f"on {current}" if ok else f"on {current}, expected {expected}",
                                 "detail": {"route": current}})
            else:
                outcomes.append({"kind": kind, "ok": False, "severity": "error",
                                 "message": f"the check {kind} is not supported on the {self.kind} native surface",
                                 "detail": {"code": "check_unsupported"}})
        return outcomes

    def dismiss_alerts(self, buttons: Sequence[str] = DISMISS_BUTTONS, *, rounds: int = 5) -> int:
        dismissed = 0
        for _ in range(rounds):
            try:
                nodes = self.nodes()
            except DriverError:
                return dismissed
            found = next((node for name in buttons for node in nodes
                          if node.visible and node.kind.endswith("Button") and (node.text or node.label) == name), None)
            if found is None:
                return dismissed
            self.tap(*found.centre)
            dismissed += 1
            time.sleep(POLL * 2)
        return dismissed

    def tree(self, max_lines: int | None = None) -> str:
        lines = [f"{node.describe()} @({round(node.x)},{round(node.y)} {round(node.width)}x{round(node.height)})"
                 + ("" if node.enabled else " (disabled)") for node in self.nodes()
                 if node.visible and (node.text or node.label or node.ident)]
        if max_lines is not None and len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... {len(lines) - max_lines} more lines"
        return "\n".join(lines)


def _assertion(surface: NativeSurface, target: Mapping[str, Any], assertion: Mapping[str, Any]) -> tuple[str | None, JsonValue]:
    found = surface.candidates(target)
    kind = assertion.get("kind")
    if kind == "visible":
        return (None if found else f"{_describe(target)} is not shown"), len(found)
    if kind == "hidden":
        return (f"{_describe(target)} is shown" if found else None), len(found)
    if kind == "count":
        count = len(found)
        equals, low, high = assertion.get("equals"), assertion.get("at_least"), assertion.get("at_most")
        if equals is not None and count != equals:
            return f"{count} native elements match {_describe(target)}, expected {equals}", count
        if low is not None and count < low:
            return f"{count} native elements match {_describe(target)}, expected at least {low}", count
        if high is not None and count > high:
            return f"{count} native elements match {_describe(target)}, expected at most {high}", count
        return None, count
    node = surface.pick(target)
    if kind in ("enabled", "disabled"):
        return (None if node.enabled == (kind == "enabled") else
                f"{_describe(target)} is {'enabled' if node.enabled else 'disabled'}"), node.enabled
    if kind == "checked":
        want = assertion.get("on", True) is not False
        return (None if bool(node.checked) == want else f"{_describe(target)} is {'on' if node.checked else 'off'}"), node.checked
    if kind in ("text", "value"):
        actual = (node.text or node.label) if kind == "text" else node.value
        equals, contains, matches = assertion.get("equals"), assertion.get("contains"), assertion.get("matches")
        if equals is not None and actual != equals:
            return f"{kind} is {actual!r}, expected {equals!r}", actual
        if contains is not None and str(contains) not in actual:
            return f"{kind} {actual!r} does not contain {contains!r}", actual
        if matches is not None and not re.search(str(matches), actual):
            return f"{kind} {actual!r} does not match /{matches}/", actual
        return None, actual
    raise DriverError("target_unsupported", f"the native surface cannot assert {kind}")


def run_native(surface: NativeSurface, op: str, args: Mapping[str, Any], timeout_ms: int | None) -> JsonValue:
    timeout = (args.get("timeout") or timeout_ms or DEFAULT_TIMEOUT * 1000) / 1000
    found = args.get("target")
    target: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    if op == "ui.settle":
        settled = surface.settle(timeout)
        if not settled and args.get("strict", True) is not False:
            raise DriverError("timeout", f"the screen did not settle within {timeout:.1f} s", retryable=True)
        return None
    if op == "ui.route":
        current = surface.foreground()
        return {"name": current, "params": {}}
    if op == "check.checkpoint":
        checks = surface.checkpoint(list(args.get("checks") or []), list(args.get("noise") or []))
        viewport = args.get("viewport")
        return {"label": args.get("label"), "viewport": viewport.get("name") if isinstance(viewport, Mapping) else None,
                "ok": all(check["ok"] or check["severity"] != "error" for check in checks), "checks": list(checks),
                "shots": []}
    if op in ("ui.deeplink", "ui.open_url"):
        surface.open_url(str(args.get("url")))
        return None
    if op in ("ui.tap", "ui.double_tap", "ui.long_press", "ui.fill", "ui.type", "ui.clear", "ui.wait_for",
              "ui.wait_gone", "expect.target", "ui.exists", "ui.visible", "ui.count") and not target:
        raise DriverError("args_invalid", f"{op} on the native surface needs a target")
    if op == "ui.tap":
        surface.tap(*surface.point(target, timeout))
        return None
    if op == "ui.double_tap":
        x, y = surface.point(target, timeout)
        surface.tap(x, y)
        surface.tap(x, y)
        return None
    if op == "ui.long_press":
        surface.tap(*surface.point(target, timeout), hold_ms=int(args.get("hold") or LONG_PRESS_MS))
        return None
    if op in ("ui.fill", "ui.type", "ui.clear"):
        node = surface.wait(target, timeout)
        surface.tap(*node.centre)
        if op != "ui.type":
            surface.clear_focused(len(node.value))
        if op != "ui.clear":
            surface.type_text(str(args.get("value") or ""))
        if args.get("submit"):
            if surface.kind == "android":
                surface._adb("shell", "input", "keyevent", "KEYCODE_ENTER")
            else:
                surface._idb_run("ui", "key", "40")
        return None
    if op == "ui.swipe":
        width, height = surface.screen_size()
        if target:
            node = surface.wait(target, timeout)
            box = (node.x, node.y, node.width, node.height)
        else:
            box = (0.0, 0.0, width, height)
        fraction = float(args.get("distance") or SWIPE_FRACTION)
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        dx = {"left": -1, "right": 1}.get(str(args.get("direction")), 0) * box[2] * fraction / 2
        dy = {"up": -1, "down": 1}.get(str(args.get("direction")), 0) * box[3] * fraction / 2
        surface.swipe((cx - dx, cy - dy), (cx + dx, cy + dy))
        return None
    if op == "ui.drag":
        surface.swipe(surface.point(args["source"], timeout), surface.point(args["to"], timeout), ms=900)
        return None
    if op in ("ui.exists", "ui.visible", "ui.count"):
        deadline = time.monotonic() + (args.get("timeout") or 0) / 1000
        while True:
            count = len(surface.candidates(target))
            if count or time.monotonic() >= deadline:
                break
            time.sleep(POLL)
        return count if op == "ui.count" else count > 0
    if op == "ui.text":
        if not target:
            return "\n".join(node.text or node.label for node in surface.nodes() if node.visible and (node.text or node.label))
        node = surface.wait(target, timeout)
        return node.text or node.label
    if op == "ui.wait_for":
        if args.get("state") == "hidden":
            surface.wait_gone(target, timeout)
        else:
            surface.wait(target, timeout)
        return None
    if op == "ui.wait_gone":
        surface.wait_gone(target, timeout)
        return None
    if op == "ui.tree":
        return surface.tree(args.get("max_lines"))
    if op == "expect.target":
        assertion = args.get("assertion") or {}
        deadline = time.monotonic() + timeout
        while True:
            try:
                failure, actual = _assertion(surface, target, assertion)
            except DriverError as error:
                if error.code != "target_not_found":
                    raise
                failure, actual = error.message, None
            if failure is None:
                return {"ok": True, "assertion": assertion.get("kind")}
            if args.get("eventually") is False or time.monotonic() >= deadline:
                message = f"{args['message']}: {failure}" if args.get("message") else failure
                raise DriverError("assertion", message, detail={"assertion": dict(assertion), "actual": actual,
                                                                "target": dict(target), "soft": bool(args.get("soft"))})
            time.sleep(POLL)
    raise DriverError("target_unsupported", f"{op} is not supported on the native surface")


__all__ = ["NATIVE_OPS", "NativeNode", "NativeSurface", "parse_android", "parse_ios", "run_native"]
