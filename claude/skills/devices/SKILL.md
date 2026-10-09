---
name: devices
description: Manage pools and leases: browser profiles, iOS simulators, Android emulators, queueing, idle release, resets, stray process sweeps, device naming and pool settings. Use when a lease is stuck, a device misbehaves, or pools need configuring.
---

# Devices, browsers and leases

```bash
eks-harness lease list
eks-harness lease status <sid>
eks-harness lease release [--kind ios]
eks-harness lease idle --grace 120
eks-harness devices list
eks-harness profiles list
eks-harness browsers list
eks-harness devices reset ios:2 --confirm "<device name>"
eks-harness config set devices.ios 3
```

- Leases queue when the pool is busy; `--wait` bounds how long a flow waits.
- A lease holds its resource while heartbeats arrive (workers heartbeat every 20 s); after
  `lease.idleSeconds` without one it is released and the app or profile is cleaned up.
- The daemon sweeps strays (`devices.strayProcessPatterns`) and retired simulators
  (`devices.retireNamePrefixes`).
- Live views of every device and profile are in the UI (Lab); `eks-harness logs ios:1` reads device logs.
