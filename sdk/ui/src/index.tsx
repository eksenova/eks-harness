import type { ComponentType, ReactNode } from "react";

export type SlotName =
  | "nav.section"
  | "route"
  | "project.tab"
  | "session.panel"
  | "artifact.viewer"
  | "device.controls"
  | "backend.inspector"
  | "node.inspector"
  | "score.track"
  | "score.inspector"
  | "settings.page"
  | "command";

export type Theme = "light" | "dark";

export interface HostEvent {
  id: number;
  ts: string;
  type: string;
  resource: string | null;
  projectId: string | null;
  detail: Record<string, unknown>;
}

export interface ApiClient {
  get<T>(path: string, query?: Record<string, string | number | boolean | undefined>): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  put<T>(path: string, body?: unknown): Promise<T>;
  delete<T>(path: string): Promise<T>;
}

export interface SlotContext {
  slot: SlotName;
  plugin: string;
  project: string | null;
  session: { projectId: string | null; slug: string } | null;
  selection: Record<string, unknown> | null;
  theme: Theme;
  api: ApiClient;
  settings: Record<string, unknown>;
}

export interface SlotProps extends SlotContext {
  project: string | null;
  artifact?: Record<string, unknown>;
  device?: { kind: string; index: number; sid: string | null; platform: string };
  backend?: Record<string, unknown>;
  node?: Record<string, unknown>;
  track?: Record<string, unknown>;
  score?: Record<string, unknown>;
}

export type SlotComponent = ComponentType<SlotProps>;
export type PluginModule = Partial<Record<SlotName, SlotComponent>>;

export interface SectionProps {
  title: ReactNode;
  count?: number | string | null;
  actions?: ReactNode;
  children?: ReactNode;
}

export interface StateLineProps {
  state: string;
  tone?: "ok" | "busy" | "wait" | "fail" | "idle";
  detail?: ReactNode;
  children?: ReactNode;
}

export interface RunBlockProps {
  title: ReactNode;
  state: string;
  tone?: StateLineProps["tone"];
  facts?: ReactNode[];
  progress?: number | null;
  children?: ReactNode;
  defaultOpen?: boolean;
}

export interface RackColumn<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  width?: string;
  align?: "end";
}

export interface RackProps<T> {
  rows: T[];
  columns: RackColumn<T>[];
  rowKey: (row: T) => string;
  label: string;
  empty?: ReactNode;
}

export interface ButtonProps {
  children?: ReactNode;
  variant?: "primary" | "secondary" | "quiet" | "danger";
  size?: "sm" | "md";
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
}

interface Shared {
  definePlugin: (slots: PluginModule) => PluginModule;
  useHost: () => SlotContext;
  useApi: () => ApiClient;
  useEvents: (listener: (event: HostEvent) => void) => void;
  Section: ComponentType<SectionProps>;
  StateLine: ComponentType<StateLineProps>;
  StateBar: ComponentType<{ tone: NonNullable<StateLineProps["tone"]> }>;
  RunBlock: ComponentType<RunBlockProps>;
  Rack: <T>(props: RackProps<T>) => ReactNode;
  Button: ComponentType<ButtonProps>;
  Timecode: ComponentType<{ seconds: number | null; fps?: number; frames?: boolean }>;
  Notice: ComponentType<{ variant?: "info" | "attention" | "error"; title?: ReactNode; children?: ReactNode }>;
  EmptyState: ComponentType<{ children: ReactNode; action?: ReactNode }>;
  Mono: ComponentType<{ children: ReactNode; title?: string }>;
  tokens: Record<string, string>;
}

function shared(): Shared {
  const value = (globalThis as { __EHX_SHARED__?: { sdk?: Shared } }).__EHX_SHARED__?.sdk;
  if (!value) throw new Error("@eks-harness/ui-sdk runs inside the eks-harness app only");
  return value;
}

export function definePlugin(slots: PluginModule): PluginModule {
  return slots;
}

export function useHost(): SlotContext {
  return shared().useHost();
}

export function useApi(): ApiClient {
  return shared().useApi();
}

export function useEvents(listener: (event: HostEvent) => void): void {
  shared().useEvents(listener);
}

export const Section: ComponentType<SectionProps> = (props) => {
  const Inner = shared().Section;
  return <Inner {...props} />;
};

export const StateLine: ComponentType<StateLineProps> = (props) => {
  const Inner = shared().StateLine;
  return <Inner {...props} />;
};

export const RunBlock: ComponentType<RunBlockProps> = (props) => {
  const Inner = shared().RunBlock;
  return <Inner {...props} />;
};

export function Rack<T>(props: RackProps<T>): ReactNode {
  return shared().Rack(props);
}

export const Button: ComponentType<ButtonProps> = (props) => {
  const Inner = shared().Button;
  return <Inner {...props} />;
};

export const Timecode: ComponentType<{ seconds: number | null; fps?: number; frames?: boolean }> = (props) => {
  const Inner = shared().Timecode;
  return <Inner {...props} />;
};

export const tokens = {
  paper: "var(--paper)",
  canvas: "var(--canvas)",
  well: "var(--well)",
  stage: "var(--stage)",
  sheet: "var(--sheet)",
  ink: "var(--ink)",
  ink2: "var(--ink-2)",
  ink3: "var(--ink-3)",
  rule: "var(--rule)",
  mark: "var(--mark)",
  markWash: "var(--mark-wash)",
  stateOk: "var(--state-ok)",
  stateBusy: "var(--state-busy)",
  stateWait: "var(--state-wait)",
  trackEdit: "var(--track-edit)",
  trackBlender: "var(--track-blender)",
  trackWeb: "var(--track-web)",
  trackDevice: "var(--track-device)",
  trackAudio: "var(--track-audio)",
  fontText: "var(--font-text)",
  fontMono: "var(--font-mono)",
} as const;
