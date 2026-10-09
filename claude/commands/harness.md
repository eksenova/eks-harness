---
description: Show the eks-harness hub, my leases, running workers, nodes and recent jobs
argument-hint: "[args]"
---

Run `eks-harness daemon status --json`, `eks-harness lease list --json`, `eks-harness driver workers` and `eks-harness node list --json`, then summarize in a short table: hub state and URL, my leases (sid, kind, resource, state), workers, nodes (state, GPUs), running jobs. Say what looks wrong. $ARGUMENTS
