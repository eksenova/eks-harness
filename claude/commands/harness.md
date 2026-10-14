---
description: Show the eks-harness hub, my leases, runs, backends, workers, nodes and the render queue
argument-hint: "[args]"
---

Run `eks-harness daemon status --json`, `eks-harness lease list --json`, `eks-harness runs list --limit 10 --json`, `eks-harness backend list --json`, `eks-harness backend leases --json`, `eks-harness driver workers`, `eks-harness node list --json` and `eks-harness queue`, then summarize in a short table: hub state and URL, my leases (sid, kind, resource, state, linked backend), recent and running runs (id, plan, status, paused node), backends (id, status, generation, leases and holders, dependencies, idle countdown, URL), workers, nodes (state, GPUs), the render queue. Say what looks wrong. $ARGUMENTS
