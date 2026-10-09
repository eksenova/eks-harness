from __future__ import annotations

import json
import time
from typing import Any

from eks_harness.flows.client import Artifact
from eks_harness.flows.core import App, Target, ms, opts

LAYOUT_CHECK = r"""(alerts) => {
  const vw = innerWidth, issues = [];
  const doc = document.documentElement;
  if (doc.scrollWidth > vw + 1) issues.push(`page scrolls horizontally (${doc.scrollWidth}px > ${vw}px)`);
  const root = document.querySelector('main') || document.body;
  for (const el of root.querySelectorAll('*')) {
    if (el.closest('[data-ehx-overlay]')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const style = getComputedStyle(el);
    if (style.position === 'fixed') continue;
    if (r.right > vw + 2) {
      issues.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} "${(el.innerText || '').trim().slice(0, 40)}" spills ${Math.round(r.right - vw)}px past the viewport`);
      if (issues.length >= 6) break;
    }
  }
  const broken = [...document.images].filter((img) => img.complete && img.naturalWidth === 0 && img.getClientRects().length).length;
  if (broken) issues.push(`${broken} broken image(s)`);
  const toasts = [...document.querySelectorAll(alerts)].map((el) => (el.innerText || '').trim()).filter(Boolean).slice(0, 3);
  return { url: location.pathname + location.search, title: document.title, issues, alerts: toasts };
}"""


DEFAULT_NOISE = r"google|facebook|doubleclick|gtag|googletagmanager|analytics|clarity|hotjar|cloudflareinsights|sentry"


class WebApp(App):
    kind = "web"
    platform = "web"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.since = int(time.time() * 1000)

    def healthy(self) -> bool:
        if self.worker is None:
            return False
        status, res = self.worker.post("/status", {}, 3)
        return status == 200 and isinstance(res, dict) and not res.get("error")

    def goto(self, path: str) -> str:
        return self.call("goto", path)

    def nav(self, path: str) -> str:
        return self.call("nav", path)

    def reload(self) -> str:
        return self.call("reload")

    def back(self) -> str:
        return self.call("back")

    def url(self) -> str:
        return self.call("url")

    def click(self, target: Target, *, force: bool | None = None, real: bool | None = None, wait: float | None = None) -> Any:
        return self.call("click", target, opts(force=force, real=real, wait=ms(wait)))

    def fill(self, target: Target, value: str, *, real: bool | None = None) -> Any:
        return self.call("fill", target, value, opts(real=real))

    def type(self, target: Target, value: str, *, delay: int | None = None, real: bool | None = None) -> Any:
        return self.call("type", target, value, opts(delay=delay, real=real))

    def press(self, key: str, target: Target | None = None, *, real: bool | None = None) -> Any:
        return self.call("press", key, target, opts(real=real))

    def choose(self, trigger: Target, option: str) -> Any:
        return self.call("choose", trigger, option)

    def select(self, target: Target, value: str) -> Any:
        return self.call("select", target, value)

    def check(self, target: Target, on: bool = True) -> Any:
        return self.call("check", target, on)

    def hover(self, target: Target) -> Any:
        return self.call("hover", target)

    def wheel(self, target: Target, delta_y: int = 400, delta_x: int = 0) -> Any:
        return self.call("wheel", target, {"deltaX": delta_x, "deltaY": delta_y})

    def wait_for(self, target: Target, *, timeout: float = 15) -> Any:
        return self.call("waitFor", target, {"timeout": int(timeout * 1000)})

    def wait_gone(self, target: Target, *, timeout: float = 15) -> Any:
        return self.call("waitGone", target, {"timeout": int(timeout * 1000)})

    def wait_url(self, pattern: str, *, timeout: float = 15) -> str:
        return self.call("waitUrl", pattern, {"timeout": int(timeout * 1000)})

    def settle(self) -> Any:
        return self.call("settle")

    def text(self, target: Target | None = None) -> str:
        return self.call("text", target)

    def value(self, target: Target) -> str:
        return self.call("value", target)

    def count(self, target: Target) -> int:
        return self.call("count", target)

    def exists(self, target: Target) -> bool:
        return self.call("exists", target)

    def visible(self, target: Target) -> bool:
        return self.call("visible", target)

    def tree(self, target: Target | None = None) -> str:
        return self.call("tree", target)

    def js(self, function_source: str, arg: Any = None) -> Any:
        tail = "" if arg is None else ", " + __import__("json").dumps(arg, ensure_ascii=False)
        return self.exec(f"return await evaluate({function_source}{tail})", label="js")

    def api(self, method: str, path: str, body: Any = None) -> Any:
        return self.call("api", method, path, body)

    def login(self, *args: Any, **kwargs: Any) -> Any:
        return self.call("login", *args, opts(**kwargs))

    def shot(self, name: str, *, phone: bool = False, full: bool = False, focus: Target | None = None,
             element: Target | None = None, caption: str | None = None, **_: Any) -> list[Artifact]:
        value = self.call("shot", name, opts(viewport="both" if phone else "desktop", full=full or None, focus=focus,
                                             element=element, caption=caption))
        arts: list[Artifact] = []
        for entry in value if isinstance(value, list) else [value]:
            if not isinstance(entry, dict):
                continue
            art = Artifact(kind="screenshot", name=f"{name}-{entry.get('viewport', 'desktop')}", id=entry.get("id"),
                           url=entry.get("url"), raw=entry.get("rawUrl"), session=entry.get("sessionUrl"),
                           local=entry.get("file") if not entry.get("id") else None)
            self.artifacts.append(art)
            arts.append(art)
        return arts

    def checks(self) -> dict:
        since = self.since
        self.since = int(time.time() * 1000)
        script = (
            "const since = %d;"
            "const noise = new RegExp(%s, 'i');"
            "const fresh = (list) => (list || []).filter((e) => !e.at || Date.parse(e.at) >= since);"
            "const uniq = (items) => { const seen = new Map(); for (const i of items) seen.set(i, (seen.get(i) || 0) + 1);"
            " return [...seen].map(([t, n]) => n > 1 ? `${t} (x${n})` : t).slice(-5); };"
            "const errors = uniq(fresh(await logs('errors', 20)).map((e) => String(e.message || e.text || '').slice(0, 200)).filter((t) => !noise.test(t)));"
            "const consoleErrors = uniq(fresh(await logs('console', 80)).filter((e) => e.type === 'error').map((e) => String(e.text).slice(0, 200)).filter((t) => !noise.test(t)));"
            "const network = uniq(fresh(await logs('network', 40)).filter((e) => !noise.test(String(e.url || ''))).map((e) => `${e.status || ''} ${e.method || ''} ${String(e.url || '').split('?')[0].slice(0, 160)}`.trim()));"
            "const layout = await evaluate(%s, %s);"
            "return { ...layout, errors, consoleErrors, network };"
        ) % (since, json.dumps(self.profile.checks.get("noise") or DEFAULT_NOISE), LAYOUT_CHECK,
               json.dumps(self.profile.checks.get("alerts") or "[role=alert]"))
        result = self.exec(script, label="checks")
        return {k: v for k, v in (result or {}).items() if v not in (None, [], "")}

    def start_recording(self, name: str, *, logged_out: bool | None = None, pace: str | dict | None = None,
                        phone: bool = False, from_here: bool | None = None, focus: Target | None = None,
                        hide: list[str] | None = None, pointer: bool = True, hold: int | None = None, **_: Any) -> Any:
        return self.call("video.start", name, opts(loggedOut=logged_out, pace=pace, viewport="mobile" if phone else None,
                                                  fromHere=from_here, focus=focus, hide=hide, hold=hold,
                                                  noPointer=None if pointer else True))

    def stop_recording(self, name: str, *, trim: bool | None = None) -> Artifact | None:
        value = self.call("video.stop", opts(trim=trim), timeout=900)
        art = Artifact(kind="video", name=name, id=None, url=None, raw=None, session=None)
        store = (value or {}).get("store") or {}
        video = store.get("video") or {}
        art.id, art.url, art.raw, art.session = video.get("id"), video.get("url"), video.get("rawUrl"), video.get("sessionUrl")
        if not art.id:
            art.local = (value or {}).get("file")
        art.extra = {k: v for k, v in {
            "durationSeconds": (value or {}).get("durationSeconds"),
            "warnings": (value or {}).get("warnings"),
            "failedInteractions": (value or {}).get("failedInteractions"),
            "annotations": len((value or {}).get("annotations") or []),
        }.items() if v}
        art.marks = (value or {}).get("annotations") or []
        return art
