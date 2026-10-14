---
name: troubleshooting
description: "Diagnose eks-harness problems: hub not reachable, auth, plans that do not build or validate (exit 2 and 1, issue codes), runs stuck preparing, lease and backend acquisition failures, seed restore or build failures, driver errors (target_not_found, target_ambiguous, op_unsupported, native_unavailable, login_unsupported), devices not booting, orphaned workers and dev servers, failing renders, offline nodes, plugin errors, logs and doctor. Use when something in the harness misbehaves."
---

# Troubleshooting

`eks-harness doctor` checks the hub, paths, config, services, tools (ffmpeg, node, Blender, adb, Xcode, idb),
plugins and nodes in one go. Start there, then follow the symptom.

## Hub and access

| Symptom | Check |
| --- | --- |
| exit 3, hub unreachable | `eks-harness daemon status`; `eks-harness daemon start`; `eks-harness daemon logs` |
| exit 4 or 5 | `eks-harness whoami`; `eks-harness login` with an API key |
| UI blank | `eks-harness version` (web build); reinstall with Node.js and pnpm on PATH |

## Plans

| Symptom | Check |
| --- | --- |
| exit 2 from `plan` or `run` | the plan did not build: a Python error, an unknown op or macro (the message suggests close names), a bad `Time` (`"2m"` has no unit; write `t("120s")`), a `Ref` used as a bool, `s.hold` outside a recording |
| exit 1 with issues before the run starts | validation: each issue has a code, the node and the source line. Common: `target.surface` (a `css` target on mobile), `persona.seed` (the persona is not in the seed), `seed.unknown`, `plugin.missing`, `clock.now`, `time.unresolvable` (beats without a song or `bpm=`), `ref.type` |
| `nothing selected` (exit 11) or several entries | name one: `file.py::name` |
| a mobile flow needs `--platform` | the app runs on iOS and Android; pass `--platform ios` or `android` |
| plugin op unknown | the plugin is not trusted or failed to import: `eks-harness plugin list`, `plugin show <id>`, `eks-harness ops --family <id>` |

## Runs

| Symptom | Check |
| --- | --- |
| stuck in `preparing` | resources: `eks-harness runs show <id> --events` shows which lease, backend or seed it waits for; `eks-harness lease list` (queue, holders), `eks-harness backend list` |
| `resource_error` | the backend or lease failed: `eks-harness backend logs --id <backend> --process <p>`; a seed restore or build failure names the resource path and kind |
| `target_not_found` | the element never appeared within the timeout: Read the failure screenshot, run with `--break-on-error` and `do ui.tree {}` in the paused session |
| `target_ambiguous` | several elements match: the detail lists candidates; narrow with `.within`, `.near`, `.nth`, `.first` |
| `assertion` | the failure block has expected and actual values; `expect.eventually` if the state arrives late |
| `check_failed` | a checkpoint check failed: the checkpoint table lists kind and message; tune `noise` or `ignore` instead of dropping the check |
| `op_unsupported` | the op is not available on that platform or worker (`ui.state` on web without `window.__ehxState`, bridge-only ops on a native app, `score.take`, `check.explore`) |
| `check_unsupported` | the worker has no implementation for that check kind (`visual`, `perf`, plugin checks) |
| `native_unavailable` | the native surface needs `idb` (iOS) or ASCII text (Android `adb input text`) |
| `login_unsupported` | the app runtime has no `login` hook for `as_=` |
| `driver_error` at the start | the app runtime failed in `prepare` or `boot`: the message names the step; check the dev server log under the hub state's `procs/` folder |
| `time_unresolved` | a beat, cue or marker time has no clock or analysis behind it |
| `resolution_unavailable` | a pinned engine or package is missing (OCR engine, faster-whisper, Demucs, TTS) |
| run paused (exit 12) | `eks-harness debug <id>` to attach, or `eks-harness runs control <id> continue` |
| flaky | `eks-harness run <file> --repeat 5` and read the diverging nodes |

## Devices, workers and processes

| Symptom | Check |
| --- | --- |
| lease never granted | `eks-harness lease list`; release stale leases of your instance with `eks-harness lease release` |
| exit 7, lease gone | the lease idled out or was released; rerun |
| device will not boot | `eks-harness devices list`; `eks-harness devices reset <id> --confirm "<name>"`; the device log in the UI |
| worker will not start | `eks-harness driver workers`; `eks-harness driver install web` (Playwright) |
| leftover node servers, `next dev`, `expo start`, `vite` | `eks-harness reap` lists orphans (`--all-installs` also looks at other installs); it kills only after confirmation |

## Media, nodes, plugins

| Symptom | Check |
| --- | --- |
| render fails | the failing node's message; `eks-harness video doctor` for ffmpeg and encoders; `font_unavailable` lists the weights a family has |
| render waits | `eks-harness queue` (one render at a time on the hub) |
| node offline | `eks-harness node show <id>`; on the node run `eks-harness node serve` in the foreground |
| plugin missing or changed | `eks-harness plugin list` (pending approval, changed, error), `plugin show <id>`, `plugin trust <id>` |
