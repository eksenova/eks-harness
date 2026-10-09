import * as React from "react";
import * as ReactDOM from "react-dom";
import * as JsxRuntime from "react/jsx-runtime";
import { Component, createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { usePluginUi, type UiModule } from "../api/system";
import { Button } from "../components/Button";
import { Mono, Section } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { Rack, RunBlock, StateBar, StateLine, Timecode } from "../components/Workbench";
import { subscribeEvents } from "../lib/events";
import { useTheme } from "../lib/theme";
import { useCurrentProject } from "./project";

export interface HostContextValue {
  slot: string;
  plugin: string;
  project: string | null;
  session: { projectId: string | null; slug: string } | null;
  selection: Record<string, unknown> | null;
  theme: "light" | "dark";
  settings: Record<string, unknown>;
}

const HostContext = createContext<HostContextValue | null>(null);

const hostApi = {
  get: <T,>(path: string, query?: Record<string, string | number | boolean | undefined>) => api.get<T>(path, { query }),
  post: <T,>(path: string, body?: unknown) => api.post<T>(path, body),
  put: <T,>(path: string, body?: unknown) => api.put<T>(path, body),
  delete: <T,>(path: string) => api.delete<T>(path),
};

function useHostValue() {
  const value = useContext(HostContext);
  if (!value) throw new Error("useHost is only available inside a plugin slot");
  return { ...value, api: hostApi };
}

function useHostEvents(listener: (event: unknown) => void) {
  const ref = React.useRef(listener);
  ref.current = listener;
  useEffect(() => subscribeEvents((event) => ref.current(event)), []);
}

let installed = false;

export function installSharedModules(): void {
  if (installed) return;
  installed = true;
  (window as unknown as { __EHX_SHARED__: unknown }).__EHX_SHARED__ = {
    react: React,
    reactDom: ReactDOM,
    jsx: JsxRuntime,
    sdk: {
      definePlugin: (slots: unknown) => slots,
      useHost: useHostValue,
      useApi: () => hostApi,
      useEvents: useHostEvents,
      Section,
      StateLine,
      StateBar,
      RunBlock,
      Rack: (props: Parameters<typeof Rack>[0]) => React.createElement(Rack as never, props as never),
      Button,
      Timecode,
      Notice,
      EmptyState,
      Mono,
      tokens: {},
    },
  };
}

type Loaded = { module: UiModule; component: React.ComponentType<Record<string, unknown>> | null; error: string | null };
const cache = new Map<string, Promise<Record<string, unknown>>>();

function load(url: string): Promise<Record<string, unknown>> {
  let entry = cache.get(url);
  if (!entry) {
    entry = import(/* @vite-ignore */ url).then((mod) => (mod.default ?? mod) as Record<string, unknown>);
    cache.set(url, entry);
  }
  return entry;
}

export function usePluginSlot(slot: string, filter?: (module: UiModule) => boolean): Loaded[] {
  const [project] = useCurrentProject();
  const ui = usePluginUi(project);
  const [loaded, setLoaded] = useState<Loaded[]>([]);
  const modules = (ui.data?.items ?? []).filter((item) => item.slot === slot && (!filter || filter(item)));
  const key = modules.map((m) => m.url).join("|");
  useEffect(() => {
    let alive = true;
    installSharedModules();
    Promise.all(
      modules.map(async (module) => {
        try {
          const exports = await load(module.url);
          const component = exports[slot] as React.ComponentType<Record<string, unknown>> | undefined;
          return { module, component: component ?? null, error: component ? null : `exports no ${slot} component` };
        } catch (error) {
          return { module, component: null, error: error instanceof Error ? error.message : String(error) };
        }
      }),
    ).then((items) => alive && setLoaded(items));
    return () => {
      alive = false;
    };
  }, [key, slot]);
  return loaded;
}

class SlotBoundary extends Component<{ name: string; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
  render() {
    if (this.state.error) return <p className="slot-error">{this.props.name} failed: {this.state.error}</p>;
    return this.props.children;
  }
}

function matchesArtifact(module: UiModule, artifact: Record<string, unknown> | undefined): boolean {
  if (!artifact) return true;
  const kinds = (module.kinds as string[] | undefined) ?? [];
  const mimes = (module.mimes as string[] | undefined) ?? [];
  if (!kinds.length && !mimes.length) return true;
  const kind = String(artifact.kind ?? "");
  const mime = String(artifact.mime ?? "");
  if (kinds.includes(kind)) return true;
  return mimes.some((pattern) => (pattern.endsWith("/*") ? mime.startsWith(pattern.slice(0, -1)) : mime === pattern));
}

export function PluginSlot({ slot, props = {}, session = null, selection = null, wrap }: {
  slot: string;
  props?: Record<string, unknown>;
  session?: HostContextValue["session"];
  selection?: Record<string, unknown> | null;
  wrap?: (module: UiModule, node: ReactNode) => ReactNode;
}) {
  const [project] = useCurrentProject();
  const theme = useTheme();
  const resolvedTheme = theme === "system" ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : theme;
  const artifact = slot === "artifact.viewer" ? (props.artifact as Record<string, unknown> | undefined) : undefined;
  const loaded = usePluginSlot(slot, artifact ? (module) => matchesArtifact(module, artifact) : undefined);
  if (!loaded.length) return null;
  return (
    <>
      {loaded.map(({ module, component: Slotted, error }) => {
        const host: HostContextValue = { slot, plugin: module.plugin, project, session, selection, theme: resolvedTheme, settings: {} };
        const node = error || !Slotted ? (
          <p className="slot-error" key={module.url}>
            {module.pluginName}: {error}
          </p>
        ) : (
          <HostContext.Provider key={module.url} value={host}>
            <SlotBoundary name={module.pluginName}>
              <div className="slot-frame" data-plugin={module.plugin}>
                <Slotted {...host} api={hostApi} {...props} />
              </div>
            </SlotBoundary>
          </HostContext.Provider>
        );
        return wrap ? <React.Fragment key={module.url}>{wrap(module, node)}</React.Fragment> : node;
      })}
    </>
  );
}
