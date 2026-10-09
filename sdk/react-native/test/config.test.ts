import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_THEME, configure, navigationAdapter, themeOf } from "../src/config";
import { createNfcFake, fakeMediaFile, registeredFakes, resolveFake } from "../src/fakes";
import { harnessEnabled, harnessUrl } from "../src/runtime";

function ref(ready: boolean, name: string) {
  return {
    isReady: () => ready,
    getRootState: () => ({ routes: [{ name }] }),
    getCurrentRoute: () => ({ name }),
    navigate: vi.fn(),
    canGoBack: () => false,
    goBack: vi.fn(),
  };
}

describe("config", () => {
  afterEach(() => configure({ navigation: undefined, theme: undefined, enabled: undefined, url: undefined }));

  it("picks the first ready navigation container by name", () => {
    configure({ navigation: { app: ref(false, "Home"), auth: ref(true, "Login") } });
    expect(navigationAdapter().container()).toBe("auth");
    expect(navigationAdapter().active()?.getCurrentRoute().name).toBe("Login");
    configure({ navigation: ref(true, "Only") });
    expect(navigationAdapter().container()).toBe("app");
    configure({ navigation: () => ({ ref: ref(true, "Fn"), container: "custom" }) });
    expect(navigationAdapter().container()).toBe("custom");
  });

  it("merges the theme over neutral defaults", () => {
    configure({ theme: { accent: "#00FF00" } });
    expect(themeOf().accent).toBe("#00FF00");
    expect(themeOf().surface).toBe(DEFAULT_THEME.surface);
  });

  it("reads enablement and url from options", () => {
    configure({ enabled: true, url: "http://127.0.0.1:9999/" });
    expect(harnessEnabled()).toBe(true);
    expect(harnessUrl()).toBe("http://127.0.0.1:9999");
  });
});

describe("fakes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("resolves armed slots, errors, delays and media payloads", async () => {
    configure({ enabled: true, url: "http://driver" });
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      calls.push(url);
      if (url.includes("/state")) {
        return new Response(JSON.stringify({ nfc: { mode: "success", data: { name: "Ada" } }, card: { mode: "error", error: "boom" }, scan: { media: "a.png", mimeType: "image/png" } }));
      }
      if (url.includes("/media-base64/")) return new Response(JSON.stringify({ base64: "AAAA" }));
      return new Response("{}");
    });
    const nfc = createNfcFake({ defaults: { name: "default", id: "1" } });
    expect(registeredFakes()).toContain("nfc");
    expect(await nfc.read()).toEqual({ success: true, data: { name: "Ada", id: "1" } });
    expect(await nfc.support()).toEqual({ supported: true, enabled: true });
    expect(await resolveFake("card")).toMatchObject({ kind: "error", error: "boom" });
    expect(await resolveFake("missing")).toEqual({ kind: "idle" });
    const scan = await resolveFake("scan");
    expect(scan.kind).toBe("success");
    const file = await fakeMediaFile("scan", (scan as any).state);
    expect(file).toMatchObject({ base64: "AAAA", mimeType: "image/png" });
    expect(file?.fileName.endsWith(".png")).toBe(true);
    expect(calls.some((url) => url.includes("/app-events?platform=ios"))).toBe(true);
  });
});
