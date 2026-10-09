---
name: react-native
description: Integrate and use @eks-harness/react-native: installing the in-app bridge, adapters (navigation, store, logout, alerts), targets, actions, live UI patches and injected components for scores, app events, and fakes registration. Use when wiring a React Native app into the harness or controlling app state and UI live.
---

# React Native bridge

Install as a git dependency in the app (dev builds only), then start it before the app renders:

```tsx
import { HarnessRoot, startHarnessBridge, registerFake } from "@eks-harness/react-native";

startHarnessBridge({
  navigation: { app: navigationRef },
  store,
  logout: performLogout,
  alert: showAlert,
  closeDevMenu,
  components: { PromoBanner },
});
export default () => <HarnessRoot><App /></HarnessRoot>;
```

Enable it with `EXPO_PUBLIC_EHX_HARNESS=1` and point it at the worker with `EXPO_PUBLIC_EHX_HARNESS_URL`.

Commands (flows: `MobileApp` methods; MCP: `driver_act`): tree, find, press, fill, submit, toggle,
invoke (any prop handler), scroll, navigate, goBack, route, state, dispatch, idle, waitFor, layout,
eval, annotate, pointer, and for scores and live control:

- `patch(target, props=, style=, text=)` changes a component's props, style or text live;
  `unpatch(key)` reverts it.
- `inject(component, props=, target=)` renders a registered component (overlay or next to a target).
- `emit(name, data)` from the app reaches the driver's event stream, flows and scores
  (`app.event("name")` in a score).

Fakes: `registerFake("nfc", handler)` and the helpers in `@eks-harness/react-native` (NFC) and
`@eks-harness/react-native/expo` (image and document pickers) read what the driver armed.
