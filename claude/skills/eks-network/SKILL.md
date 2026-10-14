---
name: eks-network
description: "Control the network and inject code in eks flows: s.net.route with delay, respond, patch, abort and passthrough; s.net.unroute; deterministic shaping profiles (wifi, 4g, 3g, slow-3g, offline, custom NetProfile presets); record and replay cassettes for hermetic runs; s.inject.script, s.inject.style and s.eval on web and React Native. Use to reproduce slow or failing APIs, pin responses, make runs repeatable, or patch page behaviour."
---

# Network and injection

The same calls work on web (Playwright routing, CDP network conditions) and React Native (the bridge's
`fetch` and `XMLHttpRequest` shim). Routes apply to requests the app makes after the route node runs.

```python
import eks
from eks import t


@eks.flow(app="web-app", seed="standard")
def network(s: eks.Session):
    s.net.route("GET", "/api/bank-transactions*").delay(t("800ms"))
    s.net.route("POST", "/api/invoices").respond(status=500, json={"error": "x"})
    s.net.route("GET", "/api/rates").patch(lambda_path="items[0].rate", value="2450.10")
    s.net.route("GET", "/api/branches", times=1).abort("timedout")
    s.net.shape("3g")
    with s.net.cassette("bank-search", mode="replay"):
        s.ui.nav("/tenant/bank/transactions")
    s.inject.script("window.__flags = {beta: true}", when="init")
```

## Routes

`s.net.route(method, pattern, times=None)` takes a method (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`,
`OPTIONS` or `*`) and a URL glob, then exactly one action:

| Action | Does |
| --- | --- |
| `.delay(t("800ms"))` | holds each matching request |
| `.respond(status=200, json=None, body=None, headers=None)` | answers with a canned response |
| `.patch(lambda_path="items[0].rate", value=...)` | changes one value in the real JSON response |
| `.abort(reason="failed")` | fails it: `failed`, `aborted`, `timedout`, `connectionrefused`, `internetdisconnected` |
| `.passthrough()` | lets it through, overriding a broader route |

`times=N` limits the route to the first N matches. A route left without an action is a plan error naming
its line. `s.net.unroute(pattern)` removes one route, `s.net.unroute()` all of them.

## Shaping

`s.net.shape(profile)` applies a deterministic profile: `wifi`, `4g`, `3g`, `slow-3g`, `offline`, or an
inline `eks.NetProfile(latency=t("400ms"), down_kbps=240, up_kbps=120, loss=0.02)`. Plugins add named
profiles with `@eks.preset` returning a `NetProfile` (skill `eks-harness:eks-extending`).

## Cassettes

`with s.net.cassette(name, mode=...):` records or replays the session's traffic inside the block. Modes:
`record`, `replay` (fails with `cassette_missing` when there is none), `auto` (replay when it exists, else
record). Cassettes are keyed by plan, node and backend fingerprint, so a backend change records a new one.
One cassette at a time; nesting is a plan error.

## Injection

| Call | Platforms | Does |
| --- | --- | --- |
| `s.inject.script(js, when="now")` | web, iOS, Android | runs once now; `when="init"` (web only) runs before every document |
| `s.inject.style(css)` | web | adds a stylesheet |
| `s.eval(source, arg=None)` | web, iOS, Android | evaluates JavaScript and returns its JSON result as a `Ref[Json]` (`await` works on mobile) |

```python
flags = s.eval("() => window.__flags")
s.inject.style("[data-testid='clock'] { visibility: hidden; }")
```

On React Native, `s.mobile.patch(target, props=, style=, text=, key=)` and `s.mobile.mount(component,
props=, key=)` change the UI live through the bridge (skill `eks-harness:eks-flows`).

Argument schemas: `eks-harness ops net.route`, `docs/reference/ops.md` (family `net` and `inject`).
