---
name: android
description: Run and drive apps on pooled Android emulators: leases, AVDs, adb reverse, install and launch, the React Native bridge, fakes, screenshots and screenrecord captures. Use for Android verification, Android flows and emulator problems.
---

# Android

- Lease an emulator with a flow (`--platform android`) or `eks-harness lease acquire --kind android`.
- Pool AVDs copy `devices.androidBaseAvd` and are named `<prefix>_harness_N`; console ports start at
  `devices.androidPortBase`. `eks-harness devices list` shows state and holders.
- `App.boot()` installs the APK, sets up `adb reverse` for Metro, the worker and the local API, grants
  permissions and launches the dev client.
- Recordings use `adb shell screenrecord`, re-encoded to constant 30 fps and aligned to the step log.
- Same bridge, targets, fakes and annotations as iOS (`eks-harness:react-native`).
- Emulator stuck or offline: `eks-harness devices reset android:1 --confirm "<name>"`, then rerun the flow.
