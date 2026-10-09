import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";
import type { DeviceDetail, EventOut, ProfileDetail } from "../api/types";
import { keys } from "../api/queries";
import { resourceName } from "./format";
import { announce } from "./hooks";

export type ConnectionState = "idle" | "connected" | "reconnecting" | "disconnected" | "restarting";

interface ConnectionSnapshot {
  state: ConnectionState;
  retryAt: number | null;
  restartedAt: number | null;
}

const CATEGORIES = ["lease", "device", "profile", "browser", "backend", "artifact", "note", "session", "project", "share", "settings", "daemon"];
const BACKOFF = [5_000, 10_000, 30_000];

let snapshot: ConnectionSnapshot = { state: "idle", retryAt: null, restartedAt: null };
const stateListeners = new Set<() => void>();
const eventListeners = new Set<(event: EventOut) => void>();
const deletedArtifacts = new Map<string, { actor: string | null; ts: string }>();
const deletedListeners = new Set<() => void>();

let source: EventSource | null = null;
let client: QueryClient | null = null;
let firstFailure: number | null = null;
let attempt = 0;
let retryTimer: number | null = null;
let restartDeadline: number | null = null;
let wasDisconnected = false;
let lastEventId: number | null = null;
let pending = new Map<string, QueryKey>();
let flushTimer: number | null = null;
let focusedResource: string | null = null;

function setSnapshot(next: Partial<ConnectionSnapshot>): void {
  snapshot = { ...snapshot, ...next };
  for (const listener of stateListeners) listener();
}

export function useConnection(): ConnectionSnapshot {
  return useSyncExternalStore(
    (listener) => {
      stateListeners.add(listener);
      return () => stateListeners.delete(listener);
    },
    () => snapshot,
  );
}

export function subscribeEvents(listener: (event: EventOut) => void): () => void {
  eventListeners.add(listener);
  return () => eventListeners.delete(listener);
}

export function useEventListener(listener: (event: EventOut) => void): void {
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => subscribeEvents((event) => ref.current(event)), []);
}

export function setFocusedResource(resource: string | null): void {
  focusedResource = resource;
}

export function deletedInfo(id: string): { actor: string | null; ts: string } | undefined {
  return deletedArtifacts.get(id);
}

export function useDeletedArtifact(id: string): { actor: string | null; ts: string } | undefined {
  return useSyncExternalStore(
    (listener) => {
      deletedListeners.add(listener);
      return () => deletedListeners.delete(listener);
    },
    () => deletedArtifacts.get(id),
  );
}

function invalidate(key: QueryKey): void {
  pending.set(JSON.stringify(key), key);
  if (flushTimer === null) {
    flushTimer = window.setTimeout(() => {
      const batch = pending;
      pending = new Map();
      flushTimer = null;
      for (const queryKey of batch.values()) void client?.invalidateQueries({ queryKey });
    }, 250);
  }
}

function prependActivity(event: EventOut): void {
  if (!client || !event.resource) return;
  const resource = event.resource;
  const [kind, index, profile] = resource.split(":");
  if ((kind === "ios" || kind === "android") && index) {
    client.setQueryData<DeviceDetail>(keys.device(kind, index), (old) =>
      old ? { ...old, activity: [event, ...old.activity.filter((e) => e.id !== event.id)].slice(0, 200) } : old,
    );
  }
  if (kind === "browser" && index && profile) {
    const id = `${index}.${profile}`;
    for (const candidate of [id, resource]) {
      client.setQueryData<ProfileDetail>(keys.profile(candidate), (old) =>
        old ? { ...old, activity: [event, ...old.activity.filter((e) => e.id !== event.id)].slice(0, 200) } : old,
      );
    }
  }
}

function describeState(event: EventOut): string {
  const status = typeof event.detail?.status === "string" ? event.detail.status : "";
  const words: Record<string, string> = {
    off: "shut down", on: "running", booting: "booting", stopping: "shutting down", failed: "failed",
    running: "running", stopped: "stopped", starting: "starting",
  };
  return words[status] ?? status;
}

function handle(event: EventOut): void {
  if (event.id) lastEventId = Math.max(lastEventId ?? 0, event.id);
  const category = event.type.split(".")[0];
  const projectId = event.projectId;
  switch (category) {
    case "lease":
      invalidate(keys.leases);
      invalidate(keys.devices);
      invalidate(["device"]);
      invalidate(keys.profiles);
      invalidate(["profile"]);
      invalidate(keys.browsers);
      invalidate(keys.status);
      invalidate(keys.backends);
      if (projectId) {
        invalidate(keys.sessions(projectId));
        invalidate(["session", projectId]);
        invalidate(["timeline", projectId]);
      }
      prependActivity(event);
      break;
    case "device":
      invalidate(keys.devices);
      invalidate(keys.status);
      if (event.resource) {
        const [kind, index] = event.resource.split(":");
        invalidate(keys.device(kind, index));
      }
      prependActivity(event);
      if (event.resource && event.resource === focusedResource && event.type === "device.status") {
        const word = describeState(event);
        if (word) announce(`${resourceName(event.resource)} is now ${word}.`);
      }
      break;
    case "profile":
    case "browser":
      invalidate(keys.profiles);
      invalidate(keys.browsers);
      invalidate(["profile"]);
      invalidate(keys.status);
      prependActivity(event);
      if (event.resource && event.resource === focusedResource) {
        const word = describeState(event);
        if (word) announce(`${resourceName(event.resource)} is now ${word}.`);
      }
      break;
    case "backend":
      invalidate(keys.backends);
      invalidate(["backend"]);
      invalidate(keys.status);
      if (projectId) invalidate(["timeline", projectId]);
      else invalidate(["timeline"]);
      break;
    case "artifact": {
      const artifactId = typeof event.detail?.id === "string" ? event.detail.id : typeof event.detail?.artifactId === "string" ? event.detail.artifactId : null;
      invalidate(keys.artifactsAll);
      invalidate(keys.projects);
      invalidate(["search"]);
      invalidate(["tags"]);
      if (projectId) {
        invalidate(keys.project(projectId));
        invalidate(keys.sessions(projectId));
        invalidate(["session", projectId]);
        invalidate(["timeline", projectId]);
      }
      if (artifactId) {
        if (event.type === "artifact.deleted") {
          deletedArtifacts.set(artifactId, { actor: event.actor, ts: event.ts });
          for (const listener of deletedListeners) listener();
        } else {
          invalidate(keys.artifact(artifactId));
        }
      }
      break;
    }
    case "tag":
      invalidate(keys.tagCatalog);
      invalidate(["tags"]);
      break;
    case "note":
      if (projectId) invalidate(["timeline", projectId]);
      else invalidate(["timeline"]);
      break;
    case "session":
    case "project":
      invalidate(keys.projects);
      if (projectId) {
        invalidate(keys.project(projectId));
        invalidate(keys.sessions(projectId));
        invalidate(["session", projectId]);
      }
      break;
    case "share":
      invalidate(["shares"]);
      invalidate(keys.artifactsAll);
      break;
    case "settings":
      invalidate(keys.settings);
      invalidate(keys.status);
      break;
    case "daemon": {
      const status = typeof event.detail?.status === "string" ? event.detail.status : "";
      if (status === "restarting" || status === "stopping") markRestarting();
      break;
    }
    default:
      break;
  }
  for (const listener of eventListeners) listener(event);
}

function clearRetry(): void {
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function open(): void {
  clearRetry();
  source?.close();
  const url = lastEventId ? `/api/events/stream?lastEventId=${lastEventId}` : "/api/events/stream";
  const es = new EventSource(url, { withCredentials: true });
  source = es;
  es.onopen = () => {
    const recovered = wasDisconnected || snapshot.state === "restarting";
    const restarted = snapshot.state === "restarting";
    firstFailure = null;
    attempt = 0;
    wasDisconnected = false;
    restartDeadline = null;
    setSnapshot({ state: "connected", retryAt: null, restartedAt: restarted ? Date.now() : snapshot.restartedAt });
    if (recovered) void client?.invalidateQueries();
  };
  const onMessage = (message: MessageEvent<string>) => {
    try {
      const parsed = JSON.parse(message.data) as EventOut;
      if (parsed && typeof parsed.type === "string") handle(parsed);
    } catch {
      return;
    }
  };
  es.onmessage = onMessage;
  for (const category of CATEGORIES) es.addEventListener(category, onMessage as EventListener);
  es.onerror = () => {
    es.close();
    if (source === es) source = null;
    scheduleRetry();
  };
}

function scheduleRetry(): void {
  const now = Date.now();
  if (firstFailure === null) firstFailure = now;
  if (restartDeadline !== null && now < restartDeadline) {
    setSnapshot({ state: "restarting", retryAt: now + 2000 });
    retryTimer = window.setTimeout(open, 2000);
    return;
  }
  restartDeadline = null;
  if (now - firstFailure < 10_000) {
    setSnapshot({ state: "reconnecting", retryAt: now + 2000 });
    retryTimer = window.setTimeout(open, 2000);
    return;
  }
  wasDisconnected = true;
  const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)];
  attempt += 1;
  setSnapshot({ state: "disconnected", retryAt: now + delay });
  retryTimer = window.setTimeout(open, delay);
}

export function markRestarting(): void {
  restartDeadline = Date.now() + 60_000;
  setSnapshot({ state: "restarting" });
}

export function retryNow(): void {
  open();
}

export function startEvents(queryClient: QueryClient): () => void {
  client = queryClient;
  if (!source) open();
  return () => {
    clearRetry();
    source?.close();
    source = null;
    setSnapshot({ state: "idle", retryAt: null });
  };
}
