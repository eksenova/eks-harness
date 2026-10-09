import type React from "react";

export interface NavigationRefLike {
  isReady(): boolean;
  getRootState(): any;
  getCurrentRoute(): any;
  navigate(...args: any[]): void;
  canGoBack(): boolean;
  goBack(): void;
}

export interface AlertButton {
  text: string;
  cancel?: boolean;
  destructive?: boolean;
  onPress?: () => void;
}

export interface AlertSpec {
  title: string;
  message?: string;
  dismissable?: boolean;
  actions: AlertButton[];
}

export interface StoreLike {
  getState(): unknown;
  dispatch(action: any): unknown;
}

export interface BridgeTheme {
  surface: string;
  surfaceDeep: string;
  onSurface: string;
  onSurfaceMuted: string;
  accent: string;
  accentLight: string;
  captionBackground: string;
  captionBorder: string;
  ringFill: string;
  scrim: string;
  pointerFill: string;
  pointerBorder: string;
  marker: string;
  markerFill: string;
  radius: number;
  fontFamily?: string;
}

export type NavigationOption =
  | NavigationRefLike
  | NavigationRefLike[]
  | Record<string, NavigationRefLike>
  | (() => { ref: NavigationRefLike | null; container: string });

export interface HarnessOptions {
  enabled?: boolean;
  url?: string;
  navigation?: NavigationOption;
  store?: StoreLike;
  logout?: () => unknown;
  signedOutContainer?: string;
  alert?: (spec: AlertSpec) => void;
  closeDevMenu?: () => void;
  theme?: Partial<BridgeTheme>;
  locale?: string;
  ignoreUrls?: (string | RegExp)[];
  components?: Record<string, React.ComponentType<any>>;
}

export const DEFAULT_THEME: BridgeTheme = {
  surface: "#141518",
  surfaceDeep: "#0A0A0B",
  onSurface: "#FFFFFF",
  onSurfaceMuted: "rgba(255,255,255,0.82)",
  accent: "#FF6247",
  accentLight: "#FF8A73",
  captionBackground: "rgba(20,21,24,0.94)",
  captionBorder: "rgba(255,98,71,0.5)",
  ringFill: "rgba(255,98,71,0.12)",
  scrim: "rgba(10,10,11,0.5)",
  pointerFill: "rgba(20,21,24,0.45)",
  pointerBorder: "#FFFFFF",
  marker: "rgba(255,98,71,0.95)",
  markerFill: "rgba(255,98,71,0.18)",
  radius: 4,
};

let options: HarnessOptions = {};
let theme: BridgeTheme = DEFAULT_THEME;

export function configure(next: HarnessOptions) {
  options = { ...options, ...next };
  theme = { ...DEFAULT_THEME, ...(options.theme ?? {}) };
}

export function harnessOptions(): HarnessOptions {
  return options;
}

export function themeOf(): BridgeTheme {
  return theme;
}

export function navigationAdapter(): { active(): NavigationRefLike | null; container(): string } {
  const nav = options.navigation;
  if (!nav) return { active: () => null, container: () => "none" };
  if (typeof nav === "function") {
    return {
      active: () => {
        const found = nav();
        return found.ref && found.ref.isReady() ? found.ref : null;
      },
      container: () => nav().container,
    };
  }
  const entries: [string, NavigationRefLike][] = Array.isArray(nav)
    ? nav.map((ref, index) => [String(index), ref])
    : "isReady" in nav && typeof (nav as NavigationRefLike).isReady === "function"
      ? [["app", nav as NavigationRefLike]]
      : Object.entries(nav as Record<string, NavigationRefLike>);
  const ready = () => entries.find(([, ref]) => ref.isReady()) ?? null;
  return {
    active: () => ready()?.[1] ?? null,
    container: () => ready()?.[0] ?? "none",
  };
}
