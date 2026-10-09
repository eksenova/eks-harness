import { useEffect, useRef, useState, useSyncExternalStore } from "react";

export interface StudioProject {
  id: string;
  name: string;
  path: string;
}

export interface StudioSegment {
  id: string;
  start: number | null;
  end: number | null;
  media: string | null;
  effects: { name?: string; kind?: string; [key: string]: unknown }[];
  transition_in?: { kind?: string; duration?: number } | null;
  transition_out?: { kind?: string; duration?: number } | null;
}

export interface StudioTimeline {
  fps: number;
  resolution: [number, number];
  duration: number;
  tracks: { name: string; z: number; segments: StudioSegment[] }[];
  audio_tracks: { name: string; segments: StudioSegment[] }[];
  markers: Record<string, { kind: string; streams: Record<string, number[]> }>;
  captions: Record<string, unknown> | null;
  transitions: { kind?: string; at?: number; duration?: number; [key: string]: unknown }[];
  used: Record<string, boolean>;
}

export interface StudioJob {
  job_id: string;
  project_id: string;
  mode: string;
  status: string;
  progress: number;
  message: string;
  output_path?: string | null;
  error?: string | null;
  started_at: number;
  updated_at: number;
  current_step?: string | null;
  frame_index?: number;
  frame_total?: number;
  eta_s?: number | null;
}

export interface StudioRender {
  job_id: string;
  mode: string;
  status: string;
  started_at: number | null;
  finished_at: number | null;
  duration_s: number | null;
  output_url: string | null;
  thumbnail_url: string | null;
  total_frames: number | null;
  error_category?: string | null;
  failure_summary?: Record<string, unknown> | null;
}

export interface CatalogEntry {
  name: string;
  kind: "effect" | "transition";
  description?: string | null;
  preview_url?: string | null;
  default_params?: Record<string, unknown> | null;
  previewable_reason?: string | null;
}

export interface MediaItem {
  path: string;
  name: string;
  url?: string | null;
  kind: "video" | "audio" | "image";
  duration?: number | null;
  dims?: [number, number] | null;
  size_bytes?: number | null;
}

type Listener = (type: string, payload: unknown) => void;
type Status = "idle" | "connecting" | "open" | "closed" | "denied";

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: number;
}

class StudioSocket {
  private socket: WebSocket | null = null;
  private pending = new Map<string, Pending>();
  private listeners = new Set<Listener>();
  private statusListeners = new Set<() => void>();
  private topics = new Set<string>();
  private counter = 0;
  private retry = 0;
  private refs = 0;
  status: Status = "idle";
  hello: { project_workspace?: string | null; media_library_root?: string | null; version?: string } | null = null;

  acquire(): () => void {
    this.refs += 1;
    if (this.refs === 1) this.connect();
    return () => {
      this.refs -= 1;
    };
  }

  private setStatus(status: Status): void {
    this.status = status;
    for (const listener of this.statusListeners) listener();
  }

  subscribeStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  private connect(): void {
    if (this.socket && this.socket.readyState <= 1) return;
    this.setStatus("connecting");
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${window.location.host}/api/studio/ws`);
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.retry = 0;
      this.setStatus("open");
      if (this.topics.size) this.send("subscribe", { topics: [...this.topics] }).catch(() => undefined);
    });
    socket.addEventListener("message", (message) => {
      let data: { type: string; id?: string; result?: unknown; payload?: unknown; code?: string; message?: string };
      try {
        data = JSON.parse(String(message.data));
      } catch {
        return;
      }
      if (data.type === "hello") this.hello = (data.payload as typeof this.hello) ?? null;
      if (data.id && this.pending.has(data.id)) {
        const entry = this.pending.get(data.id)!;
        this.pending.delete(data.id);
        window.clearTimeout(entry.timer);
        if (data.type === "error") entry.reject(new Error(data.message || data.code || "studio error"));
        else entry.resolve(data.result);
        return;
      }
      for (const listener of this.listeners) listener(data.type, data.payload ?? data);
    });
    socket.addEventListener("close", (event) => {
      this.socket = null;
      for (const [, entry] of this.pending) entry.reject(new Error("studio connection closed"));
      this.pending.clear();
      if (event.code === 1008 || event.code === 4401 || event.code === 4403) {
        this.setStatus("denied");
        return;
      }
      this.setStatus("closed");
      if (this.refs > 0) {
        this.retry = Math.min(this.retry + 1, 6);
        window.setTimeout(() => this.connect(), 500 * 2 ** this.retry);
      }
    });
  }

  send<T = unknown>(type: string, payload: Record<string, unknown> = {}, timeoutMs = 60_000): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const socket = this.socket;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        const onOpen = () => {
          this.statusListeners.delete(onOpen);
          if (this.status === "open") this.send<T>(type, payload, timeoutMs).then(resolve, reject);
          else if (this.status === "denied") reject(new Error("The studio needs an admin login."));
        };
        this.statusListeners.add(onOpen);
        return;
      }
      const id = `r${++this.counter}`;
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${type} did not answer`));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject, timer });
      socket.send(JSON.stringify({ type, id, payload }));
    });
  }

  subscribe(topics: string[]): void {
    const fresh = topics.filter((topic) => !this.topics.has(topic));
    for (const topic of fresh) this.topics.add(topic);
    if (fresh.length && this.status === "open") this.send("subscribe", { topics: fresh }).catch(() => undefined);
  }

  on(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export const studio = new StudioSocket();

export function useStudioConnection(topics: string[] = []): Status {
  const key = topics.join(",");
  useEffect(() => {
    const release = studio.acquire();
    if (key) studio.subscribe(key.split(","));
    return release;
  }, [key]);
  return useSyncExternalStore(
    (listener) => studio.subscribeStatus(listener),
    () => studio.status,
  );
}

export function useStudioEvent(listener: Listener): void {
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => studio.on((type, payload) => ref.current(type, payload)), []);
}

export function useStudioRequest<T>(type: string, payload: Record<string, unknown> | null, deps: unknown[] = []): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
} {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: payload !== null });
  const [tick, setTick] = useState(0);
  const status = useStudioConnection();
  const payloadKey = JSON.stringify(payload);
  useEffect(() => {
    if (payload === null) return;
    let alive = true;
    setState((current) => ({ ...current, loading: true }));
    studio
      .send<T>(type, payload)
      .then((data) => alive && setState({ data, error: null, loading: false }))
      .catch((error: Error) => alive && setState({ data: null, error: error.message, loading: false }));
    return () => {
      alive = false;
    };
  }, [type, payloadKey, tick, status === "open", ...deps]);
  return { ...state, reload: () => setTick((value) => value + 1) };
}
