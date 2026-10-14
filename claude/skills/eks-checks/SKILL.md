---
name: eks-checks
description: "Checks in eks plans: checkpoint checks (console and page errors, failed requests, layout spill, horizontal scroll, broken images, error toasts, route) with their defaults, parameters and noise lists; media checks on renders and recordings (cut, beats, onset, loudness, text OCR, motion, black, frozen, safe area, template); review checks derived from an edit; soft failures. Use when deciding what a checkpoint verifies, tuning noise, or checking a video's timing and content inside a plan."
---

# Checks

Checks fail the run by default. `soft=True` (on a checkpoint, a media check or `expect.soft`) records the
failure in the report without failing the run.

## Checkpoint checks

`s.checkpoint(label, viewport=None, checks=None, shot=True, full=False, soft=False)` runs a set of checks on
the current screen, over the window since the previous checkpoint of the session, and takes a screenshot.

| Kind | Platforms | Default | Parameters |
| --- | --- | --- | --- |
| `console_errors` | web | yes | `noise` (message substrings to ignore) |
| `page_errors` | web, iOS, Android | yes | `noise` |
| `failed_requests` | web | yes | `noise` (URL substrings), `allow_status` |
| `layout_spill` | web | yes | `ignore` (targets allowed to spill past the viewport edge) |
| `horizontal_scroll` | web | yes | |
| `broken_images` | web | yes | |
| `error_toasts` | web, iOS, Android | yes | `selector` (a target for the app's error toasts) |
| `route` | web, iOS, Android | no | `expected` (path on web, route name on React Native; globs work) |

```python
from eks.checks import use

s.checkpoint("list")                                             # the default set for the platform
s.checkpoint("list-phone", viewport="phone")
s.checkpoint("saved", checks=["page_errors", use("route", expected="/tenant/invoice/list")])
s.checkpoint("noisy", checks=[use("console_errors", noise=("ResizeObserver loop",)),
                              use("layout_spill", ignore=("#toast-region",))])
```

- `checks=None` means the defaults above plus the app profile's `[defaults] checks` and the flow's
  `checks=`. The plan pins the resolved set, so a run checks exactly what was planned.
- `checks=[...]` replaces the set for that checkpoint. Items are kind names, `use(kind, **params)` or a
  registered check's params model.
- `noise=` on the flow and `noise` in app.toml (`[defaults]` or `[checks]`) feed every check that filters
  messages.
- On a native app (`[mobile] bridge = false`), `page_errors` reads the crash log and `route` the resumed
  activity (`expected="*MainActivity"`).
- A failing checkpoint fails the run unless `soft=True`; the report's checkpoint table lists the problems.

## Media checks

On an edit or score, `e.expect.<kind>(...)` queues a `check.media` on the rendered output (skill
`eks-harness:eks-edit`):

```python
e.expect.cut(at=t("beat:8"))
e.expect.beats("beat", at=t.span(t("0s"), t("8s")), min_ratio=0.8)
e.expect.onset(at=t("1s"))
e.expect.loudness(-14, true_peak=-1)
e.expect.text("Anlık kur", at=t.span(t("2s"), t("5s")), engine="vision")
e.expect.motion(at=t.span(t("0s"), t("3s")), min=0.5)
e.expect.black(longest=t("2f")); e.expect.frozen(longest=t("1s"))
e.expect.safe_area(margins=(0.1, 0.05, 0.2, 0.05))
e.expect.template("assets/logo.png", at=t("5s"), threshold=0.8)
e.expect.derive(cuts=True, captions=True, beats=True, loudness=True)
```

- `at` is a `Time` or `Span`; `tolerance` a length (`t("2f")`). OCR (`text`, `safe_area`) uses macOS
  Vision or tesseract; `engine="auto"` picks per machine, so pin `vision` or `tesseract` when results must
  match across machines.
- `e.expect.derive(...)` generates checks from the edit: a `cut` at every hard cut of the lowest track, a
  soft `text` check in the middle of every caption and title window, `beats` over the cuts when clips start
  on beats, and `loudness` from the master's target. Derived checks are labelled `derived:<kind>:<n>`.
- For a recording or render outside a plan, use `eks-harness video check` (skill `eks-harness:video-review`).

## Assertions

`expect` (skill `eks-harness:eks-flows`) covers element state and values; `expect.eventually` waits,
`expect.soft` records without failing.

## Not available yet

These plan and validate but do not check anything yet: `visual` (golden screenshots per checkpoint and
viewport), `perf` and `perf_rn` (web vitals, request and byte budgets, React Native frame drops) report
`check_unsupported`; `s.explore(...)` (`check.explore`) answers `op_unsupported`. Plugin checkpoint checks
that the web or mobile worker does not implement also report `check_unsupported`; plugin media checks with
an `impl` run on the hub.

Generated reference: `docs/reference/checks.md`.
