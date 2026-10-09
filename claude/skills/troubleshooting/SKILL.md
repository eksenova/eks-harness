---
name: troubleshooting
description: Diagnose eks-harness problems: hub not reachable, auth, stuck leases, devices not booting, workers not starting, failing flows, backend boot failures, failing renders, offline nodes, plugin errors, logs and doctor. Use when something in the harness misbehaves.
---

# Troubleshooting

| Symptom | Check |
| --- | --- |
| exit 3, hub unreachable | `eks-harness daemon status`; `eks-harness daemon start`; `eks-harness logs daemon` |
| exit 4 or 5 | `eks-harness whoami`; `eks-harness login` with an API key |
| lease never granted | `eks-harness lease list` (queue, holders); `lease release` stale leases of your instance |
| exit 7, lease gone | the lease idled out: rerun the flow (it reacquires) |
| device will not boot | `eks-harness devices list`; `devices reset <id> --confirm <name>`; `logs ios:1` |
| worker will not start | `eks-harness driver workers`; `driver install web` (Playwright); the worker log path in its record |
| flow fails at a step | the report's failure block (url, errors, alerts) and the failure screenshot; rerun with `-v` |
| backend fails | `eks-harness backend logs --definition <d>` and `--process <p>`; `backend stop --final`, then ensure |
| render fails | `eks-harness video doctor`; Blender: `EKS_HARNESS_BLENDER` and the plugin enabled; WebScene: `driver install web` |
| node offline | `eks-harness node show <id>`; on the node run `eks-harness node serve` in the foreground |
| plugin missing | `eks-harness plugin list` (pending approval, changed, error) and `plugin show <id>` |
| UI blank | `eks-harness version` (web build); reinstall with Node.js and pnpm on PATH |

`eks-harness doctor` checks the hub, paths, config, services, tools (ffmpeg, node, Blender, adb, Xcode),
plugins and nodes in one go.
