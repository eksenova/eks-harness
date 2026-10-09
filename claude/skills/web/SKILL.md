---
name: web
description: Drive the web driver: leased Chrome profiles over CDP with Playwright, target grammar, synthetic and real input, settle, captures (screenshot, DOM, MHTML, accessibility, HAR, console), screencast recordings with pacing and live annotations, extensions for app logins. Use for interactive browser work and for writing web flows or web driver extensions.
---

# Web driver

The web worker drives one leased Chrome profile (desktop and phone viewports) over CDP. Flows use it
through `WebApp`; agents can drive it directly with the MCP driver tools.

```text
driver_workers()                                   running workers and their lease sids
driver_act(sid, "goto", {"route": "/orders"})
driver_act(sid, "click", {"target": "role=button:New order"})
driver_act(sid, "fill", {"target": "label=Customer", "text": "Acme"})
driver_observe(sid, "tree")                        cheap: do this before any screenshot
driver_capture(sid, "screenshot", {"name": "orders-new"})
driver_capture(sid, "video.start", {"name": "new-order"}), then driver_capture(sid, "video.stop")
```

- Input is synthetic DOM events by default (fast, works behind overlays); pass `"real": true` for trusted
  Playwright input (file pickers, native behaviors).
- `settle` waits for hydration, network quiet and DOM quiet; pages that never stop changing count as live.
- Captures upload to the artifact store and return `url` (page) and `rawUrl` (file). Read what you capture.
- Worker config comes from `<app>/.harness/app.toml` `[web]`: url, api_url, locale, timezone, nav
  (`location`, `next`, `hook`), hide selectors, network ignore patterns, viewports, theme.
- App-specific logins and storage contracts are `driver_extension` modules in the repo plugin
  (`export default function extend(kit) { return { helpers: { login, as } } }`).
- Logs: `driver_observe(sid, "logs")` returns console errors, page errors, failed requests and WebSocket frames.
