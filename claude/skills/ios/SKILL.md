---
name: ios
description: Run and drive React Native or native apps on pooled iOS simulators: leases, install and launch, Metro, the in-app bridge, fakes (camera, NFC, documents), device screenshots and recordings. Use for iOS verification, iOS flows and iOS device problems.
---

# iOS

- Lease a simulator: flows do it for you (`eks-harness flow run ... --platform ios`); by hand
  `eks-harness lease acquire --kind ios --shell` prints `EKS_LEASE_SID`.
- Pool simulators are named `<devices.namePrefix> iOS N` and stay windowed; `eks-harness devices list`
  shows them, `eks-harness devices reset ios:1 --confirm "<name>"` rebuilds one.
- The app is built and installed by the repo's `App.boot()` (fingerprint-cached native builds, Metro on
  the port in `app.toml`). React Native apps embed `@eks-harness/react-native`; the mobile worker reaches it
  through Metro's DevTools.
- Recordings come from `simctl io recordVideo` and are re-encoded to constant 30 fps; steps are stamped
  against the recorder's start, so trims and step logs line up.
- Fakes: `app.arm("camera", media="fixtures/id-front.png")`, `app.arm("nfc", mode="success", data={...})`,
  `app.clear_fakes()`; the slots are whatever the app registered.
- OS-owned surfaces (permission prompts, share sheets) are outside the bridge; grant permissions in
  `boot()` (`xcrun simctl privacy`).
- Device logs: `eks-harness capture log --sid <sid>`; app console and exceptions are in the worker log.
