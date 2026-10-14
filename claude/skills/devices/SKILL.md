---
name: devices
description: "Manage pools and leases: browser profiles, iOS simulators, Android emulators, how eks runs acquire and release them, queueing, idle release, resets, stray and orphan process sweeps, device naming and pool settings. Use when a lease is stuck, a device misbehaves, or pools need configuring."
---

# Devices, browsers and leases

A run acquires the lease its plan needs (a browser profile for web, a simulator for iOS, an emulator for
Android) while it prepares, heartbeats it while it runs (paused runs included) and releases it when it ends.
You rarely hold a driver lease by hand; `eks-harness run` and `eks_run` do it.

```bash
eks-harness lease list                       # leases, queue and holders (runs show as "run <id>")
eks-harness lease status <sid>
eks-harness lease release [--kind ios]       # release your instance's leases
eks-harness lease idle --grace 120
eks-harness devices list                     # simulators and emulators: state, holder, name
eks-harness profiles list
eks-harness browsers list
eks-harness devices reset ios:2 --confirm "<device name>"
eks-harness config set devices.ios 3
eks-harness reap                             # orphaned workers and dev servers, killed after confirmation
```

- Leases queue when the pool is busy; a run waits in `preparing` and its events show the queue.
- A lease is held while heartbeats arrive; after `lease.idleSeconds` without one it is released and the app
  or profile is cleaned up. A run's lease is tagged `run:<id>`.
- Pool Chrome is shared and windowed; each lease gets an isolated profile. Simulators are named
  `<devices.namePrefix> iOS N` and must show their window (Simulator.app or DeviceHub); emulators copy
  `devices.androidBaseAvd` into `<prefix>_harness_N` with console ports from `devices.androidPortBase`.
- Nothing the harness starts runs headless: pool Chrome refuses `--headless`, emulators start with a window,
  and a simulator lease fails when its window cannot be shown.
- On release and take-over the apps in the project's `apps.iosBundleIds` and `apps.androidPackages`
  settings are removed from the device.
- Every worker and dev server the harness spawns is registered with its owner; the hub kills them when the
  owner dies, and `eks-harness reap` (or `doctor --kill-orphans`) finds leftovers, including other installs
  with `--all-installs`.
- The daemon sweeps strays (`devices.strayProcessPatterns` per project) and retired simulators
  (`devices.retireNamePrefixes`).
- Live views of every device and profile are in the UI (Lab); `eks-harness logs ios:1` reads device logs.

For the app side (installing, launching, the React Native bridge, native dialogs) see skill
`eks-harness:eks-flows` (`references/apps.md`).
