import { useQuery } from "@tanstack/react-query";
import { api, encodeSegment, type Query } from "./client";

export interface GpuInfo {
  index: number;
  name: string;
  vendor?: string;
  memoryMb?: number;
  backends?: string[];
}

export interface NodeSlot {
  id: string;
  workers?: number;
  busy?: number;
  backend?: string;
  tags?: string[];
  env?: Record<string, string>;
}

export interface NodeCapabilities {
  os?: string;
  arch?: string;
  hostname?: string;
  cpus?: number;
  memoryMb?: number;
  diskFreeGb?: number;
  diskTotalGb?: number;
  gpus?: GpuInfo[];
  blender?: { path: string; version: string };
  chrome?: string;
  tools?: Record<string, string>;
  wsl?: boolean;
  jobKinds?: string[];
}

export interface NodeOut {
  id: string;
  label: string;
  state: "online" | "offline" | "disabled";
  online: boolean;
  disabled: boolean;
  createdAt: number;
  lastSeenAt: number | null;
  version?: string;
  capabilities: NodeCapabilities;
  config: Record<string, unknown>;
  slots: NodeSlot[];
  running: string[];
  tokenPrefix?: string;
  jobs?: JobOut[];
}

export interface JobOutput {
  name: string;
  hash: string;
  size: number;
}

export interface JobOut {
  id: string;
  kind: string;
  state: "queued" | "assigned" | "running" | "done" | "failed" | "cancelled";
  node: string | null;
  slot: string | null;
  requirements: Record<string, unknown>;
  payload: Record<string, unknown>;
  inputs: { name: string; hash: string }[];
  result: { data?: unknown; outputs?: JobOutput[] } | null;
  error: string | null;
  progress: number | null;
  message: string | null;
  attempts: number;
  priority: number;
  owner: string | null;
  createdAt: number;
  startedAt: number | null;
  finishedAt: number | null;
}

export interface PluginContribution {
  plugin: string;
  type: string;
  id: string;
  entry?: string;
  path?: string;
  slot?: string;
  module?: string;
  title?: string;
  [key: string]: unknown;
}

export interface PluginSettingSpec {
  key: string;
  type: string;
  default: unknown;
  description: string;
  choices: string[] | null;
}

export interface PluginOut {
  id: string;
  kind: "builtin" | "package" | "path" | "repo" | "git";
  origin: string;
  root?: string;
  state: "active" | "pending" | "changed" | "disabled" | "error" | "shadowed";
  reason?: string;
  tree?: string;
  hash?: string;
  name?: string;
  version?: string;
  api?: string;
  description?: string;
  enabledByDefault?: boolean;
  contributes?: Record<string, number>;
  contributions?: PluginContribution[];
  settings?: PluginSettingSpec[];
  values?: Record<string, unknown>;
  error?: string;
}

export interface PluginList {
  items: PluginOut[];
  tree: string | null;
  types: Record<string, string>;
  slots: string[];
}

export interface UiModule extends PluginContribution {
  pluginName: string;
  url: string;
}

export interface ScoreFile {
  path: string;
  name: string;
  relative: string;
  modified: number;
}

export interface ScoreTrack {
  id: string;
  kind: string;
  enabled?: boolean;
  start?: string;
  end?: string;
  label?: string;
  emits?: string[];
  entry?: string;
  script?: string;
  blend?: string;
  platform?: string;
  flow?: string;
  path?: string;
  textures?: Record<string, string>;
  layers?: { source: string; start?: string; end?: string; opacity?: number; blend?: string }[];
  audio?: string[];
  size?: [number, number];
  [key: string]: unknown;
}

export interface ScoreRule {
  id?: string;
  when: { at?: string; event?: string; source?: string; where?: Record<string, unknown>; offset?: number; once?: boolean };
  do: { target: string; verb: string; args?: Record<string, unknown> }[];
}

export interface ScoreIR {
  version: number;
  name: string;
  clock: { fps: number; duration?: number; window?: [number, number]; tempo: Record<string, unknown>; markers: Record<string, number> };
  tracks: ScoreTrack[];
  rules: ScoreRule[];
  bindings: Record<string, string>;
}

export interface PlanEvent {
  time: number;
  frame?: number;
  source: string;
  name: string;
  data?: Record<string, unknown>;
}

export interface PlanAction {
  time: number;
  frame: number;
  target: string;
  verb: string;
  args: Record<string, unknown>;
  rule: string;
  cause?: PlanEvent;
}

export interface ScorePlan {
  fps: number;
  duration: number | null;
  beats: number[];
  downbeats: number[];
  markers: Record<string, number>;
  tracks: Record<string, { start: number; end: number }>;
  events: PlanEvent[];
  actions: PlanAction[];
  warnings: string[];
}

export interface ScorePlanResponse {
  score: ScoreIR;
  plan: ScorePlan;
  base: string;
}

export interface LiveSnapshot {
  id: string;
  name: string;
  state: "stopped" | "playing" | "paused";
  position: number;
  duration: number | null;
  fps: number;
  loop: boolean;
  devices: Record<string, string | null>;
  plan?: ScorePlan;
  audio?: { track: string; url: string; offset: number; gainDb?: number } | null;
  previews?: Record<string, { state: string; url?: string; error?: string; withoutTextures?: string[] }>;
}

export interface DriverWorker {
  kind: string;
  sid: string;
  port: number;
  pid: number;
  startedAt: number;
  lease: string | null;
  state: string | null;
}

export const systemKeys = {
  nodes: ["nodes"] as const,
  node: (id: string) => ["node", id] as const,
  jobs: (params: Query) => ["jobs", params] as const,
  jobsAll: ["jobs"] as const,
  plugins: (tree: string | null) => ["plugins", tree ?? ""] as const,
  plugin: (id: string, tree: string | null) => ["plugin", id, tree ?? ""] as const,
  pluginUi: (project: string | null) => ["plugin-ui", project ?? ""] as const,
  scores: (tree: string) => ["scores", tree] as const,
  scorePlan: (path: string) => ["score-plan", path] as const,
  live: ["score-live"] as const,
  drivers: ["drivers"] as const,
  driver: (sid: string) => ["driver", sid] as const,
};

export function useNodes() {
  return useQuery({ queryKey: systemKeys.nodes, queryFn: () => api.get<{ items: NodeOut[] }>("/api/nodes"), refetchInterval: 5000 });
}

export function useNode(id: string) {
  return useQuery({ queryKey: systemKeys.node(id), queryFn: () => api.get<NodeOut>(`/api/nodes/${encodeSegment(id)}`), refetchInterval: 4000 });
}

export function useJobs(params: Query = {}) {
  return useQuery({ queryKey: systemKeys.jobs(params), queryFn: () => api.get<{ items: JobOut[] }>("/api/jobs", { query: { limit: 100, ...params } }), refetchInterval: 3000 });
}

export function usePlugins(tree: string | null) {
  return useQuery({ queryKey: systemKeys.plugins(tree), queryFn: () => api.get<PluginList>("/api/plugins", { query: { tree: tree || undefined } }) });
}

export function usePlugin(id: string, tree: string | null) {
  return useQuery({ queryKey: systemKeys.plugin(id, tree), queryFn: () => api.get<PluginOut>(`/api/plugins/${encodeSegment(id)}`, { query: { tree: tree || undefined } }) });
}

export function usePluginUi(project: string | null) {
  return useQuery({ queryKey: systemKeys.pluginUi(project), queryFn: () => api.get<{ items: UiModule[] }>("/api/plugins/ui", { query: { project: project || undefined } }), staleTime: 60_000 });
}

export function useScores(tree: string | null) {
  return useQuery({
    queryKey: systemKeys.scores(tree ?? ""),
    queryFn: () => api.get<{ tree: string; items: ScoreFile[] }>("/api/scores", { query: { tree: tree ?? "" } }),
    enabled: Boolean(tree),
    retry: false,
  });
}

export function useScorePlan(path: string, analyze = true) {
  return useQuery({
    queryKey: [...systemKeys.scorePlan(path), analyze],
    queryFn: () => api.get<ScorePlanResponse>("/api/scores/plan", { query: { path, analyze }, timeoutMs: 120_000 }),
    enabled: Boolean(path),
    retry: false,
    staleTime: 30_000,
  });
}

export function useLiveSessions() {
  return useQuery({ queryKey: systemKeys.live, queryFn: () => api.get<{ items: LiveSnapshot[] }>("/api/scores/live"), refetchInterval: 5000 });
}

export function useDriverWorkers() {
  return useQuery({ queryKey: systemKeys.drivers, queryFn: () => api.get<{ items: DriverWorker[] }>("/api/drivers/workers"), refetchInterval: 10_000 });
}

export function useDriver(sid: string | null) {
  return useQuery({
    queryKey: systemKeys.driver(sid ?? ""),
    queryFn: () => api.get<{ sid: string; platform: string; lease: string; state: string; worker: { kind: string; port: number } | null }>(`/api/drivers/${encodeSegment(sid ?? "")}`),
    enabled: Boolean(sid),
    retry: false,
  });
}

export function driverAct(sid: string, action: string, params: Record<string, unknown> = {}) {
  return api.post<{ action: string; value: unknown }>(`/api/drivers/${encodeSegment(sid)}/act`, { action, params });
}

export function driverObserve(sid: string, query: string, params: Record<string, unknown> = {}) {
  return api.post<{ query: string; value: unknown }>(`/api/drivers/${encodeSegment(sid)}/observe`, { query, params });
}

export function driverCapture(sid: string, kind: string, params: Record<string, unknown> = {}) {
  return api.post<{ kind: string; value: unknown }>(`/api/drivers/${encodeSegment(sid)}/capture`, { kind, params });
}

export function jobDuration(job: JobOut, now = Date.now() / 1000): number | null {
  if (!job.startedAt && !job.finishedAt) return null;
  const start = job.startedAt ?? job.createdAt;
  const end = job.finishedAt ?? now;
  return Math.max(0, end - start);
}

export function nodeGpuLabel(node: NodeOut): string {
  const gpus = node.capabilities.gpus ?? [];
  if (!gpus.length) return "no GPU";
  const counts = new Map<string, number>();
  for (const gpu of gpus) counts.set(gpu.name, (counts.get(gpu.name) ?? 0) + 1);
  return [...counts.entries()].map(([name, count]) => (count > 1 ? `${count}x ${name}` : name)).join(", ");
}
