export { BRIDGE_GLOBAL, HarnessRoot, REPLY_BINDING, emit, startHarnessBridge } from "./bridge";
export { DEFAULT_THEME, configure, harnessOptions, type AlertSpec, type BridgeTheme, type HarnessOptions, type NavigationRefLike, type StoreLike } from "./config";
export { createNfcFake, fakeMediaFile, fakeState, registerFake, registeredFakes, resolveFake, type FakeMediaFile, type FakeOutcome } from "./fakes";
export { HarnessOverlay, overlay, type OverlayItem } from "./overlay";
export { fetchHarnessMediaBase64, getHarnessState, harnessEnabled, harnessReport, harnessUrl, type FakeState, type HarnessState } from "./runtime";
