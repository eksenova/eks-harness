import { Platform } from "react-native";
import { harnessOptions } from "./config";

const DEFAULT_URL = "http://127.0.0.1:8099";

export type FakeState = {
  mode?: string;
  delayMs?: number;
  error?: string;
  data?: Record<string, unknown>;
  media?: string;
  mediaBase64?: string;
  mimeType?: string;
  fileName?: string;
  width?: number;
  height?: number;
  [key: string]: unknown;
};

export type HarnessState = Record<string, FakeState | null | undefined>;

export function harnessEnabled(): boolean {
  const configured = harnessOptions().enabled;
  if (typeof configured === "boolean") return configured;
  return process.env.EXPO_PUBLIC_EHX_HARNESS === "1";
}

export function harnessUrl(): string {
  return (harnessOptions().url?.trim() || process.env.EXPO_PUBLIC_EHX_HARNESS_URL?.trim() || DEFAULT_URL).replace(/\/+$/, "");
}

async function harnessFetch(path: string, init?: RequestInit, timeoutMs = 4000): Promise<Response | null> {
  if (!harnessEnabled()) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${harnessUrl()}${path}`, { ...init, signal: controller.signal });
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function getHarnessState(): Promise<HarnessState | null> {
  const response = await harnessFetch(`/state?platform=${Platform.OS}`);
  if (!response || !response.ok) return null;
  try {
    return (await response.json()) as HarnessState;
  } catch {
    return null;
  }
}

export async function fetchHarnessMediaBase64(name: string): Promise<string | null> {
  const response = await harnessFetch(`/media-base64/${encodeURIComponent(name)}`, undefined, 15000);
  if (!response || !response.ok) return null;
  try {
    const payload = (await response.json()) as { base64?: string };
    return payload.base64 ?? null;
  } catch {
    return null;
  }
}

export function harnessReport(event: string, payload?: unknown): void {
  if (!harnessEnabled()) return;
  void harnessFetch(`/app-events?platform=${Platform.OS}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, payload, at: new Date().toISOString() }),
  });
}
