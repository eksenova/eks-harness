import { fetchHarnessMediaBase64, getHarnessState, harnessEnabled, harnessReport, type FakeState } from "./runtime";

export type FakeOutcome =
  | { kind: "idle" }
  | { kind: "cancel"; state: FakeState }
  | { kind: "error"; error: string; state: FakeState }
  | { kind: "success"; state: FakeState };

const registry = new Map<string, { description?: string }>();

export function registerFake(slot: string, info: { description?: string } = {}) {
  registry.set(slot, info);
}

export function registeredFakes(): string[] {
  return [...registry.keys()];
}

function delay(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

export async function fakeState(slot: string): Promise<FakeState | null> {
  if (!harnessEnabled()) return null;
  const state = await getHarnessState();
  return state?.[slot] ?? null;
}

export async function resolveFake(slot: string, { report = true } = {}): Promise<FakeOutcome> {
  const state = await fakeState(slot);
  if (!state) return { kind: "idle" };
  if (report) harnessReport("harness.fake.intercepted", { slot, mode: state.mode ?? "success" });
  if (state.delayMs && state.delayMs > 0) await delay(Math.min(state.delayMs, 30000));
  if (state.mode === "cancel") return { kind: "cancel", state };
  if (state.mode && state.mode !== "success") return { kind: "error", error: state.error ?? `${slot}: ${state.mode}`, state };
  return { kind: "success", state };
}

export interface FakeMediaFile {
  base64: string;
  mimeType: string;
  fileName: string;
  width: number;
  height: number;
}

export async function fakeMediaFile(slot: string, state: FakeState): Promise<FakeMediaFile | null> {
  const base64 = state.mediaBase64 ?? (state.media ? await fetchHarnessMediaBase64(state.media) : null);
  if (!base64) return null;
  const mimeType = state.mimeType ?? "image/jpeg";
  const extension = mimeType.includes("pdf") ? "pdf" : mimeType.includes("png") ? "png" : "jpg";
  return {
    base64,
    mimeType,
    fileName: state.fileName ?? `harness-${slot}-${Date.now()}.${extension}`,
    width: state.width ?? 1280,
    height: state.height ?? 800,
  };
}

export interface NfcFakeOptions<T extends Record<string, unknown>> {
  slot?: string;
  defaults?: T;
  merge?: (data: T, input: Record<string, unknown> | undefined) => T;
}

export function createNfcFake<T extends Record<string, unknown>>(options: NfcFakeOptions<T> = {}) {
  const slot = options.slot ?? "nfc";
  registerFake(slot, { description: "NFC reads" });
  return {
    async support(): Promise<{ supported: boolean; enabled: boolean } | null> {
      const state = await fakeState(slot);
      if (!state) return null;
      if (state.mode === "unsupported") return { supported: false, enabled: false };
      if (state.mode === "disabled") return { supported: true, enabled: false };
      return { supported: true, enabled: true };
    },
    async read(input?: Record<string, unknown>): Promise<{ success: boolean; data?: T; error?: string } | null> {
      const outcome = await resolveFake(slot);
      if (outcome.kind === "idle") return null;
      if (outcome.kind !== "success") {
        return { success: false, error: outcome.kind === "error" ? outcome.error : `${slot}: canceled` };
      }
      const base = { ...(options.defaults ?? ({} as T)), ...((outcome.state.data as T) ?? {}) } as T;
      return { success: true, data: options.merge ? options.merge(base, input) : base };
    },
  };
}
