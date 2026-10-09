import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

let minuteTick = Date.now();
const minuteListeners = new Set<() => void>();
let minuteTimer: number | null = null;

function subscribeMinute(listener: () => void): () => void {
  minuteListeners.add(listener);
  if (minuteTimer === null) {
    minuteTimer = window.setInterval(() => {
      minuteTick = Date.now();
      for (const fn of minuteListeners) fn();
    }, 60_000);
  }
  return () => {
    minuteListeners.delete(listener);
    if (!minuteListeners.size && minuteTimer !== null) {
      window.clearInterval(minuteTimer);
      minuteTimer = null;
    }
  };
}

export function useMinute(): Date {
  useSyncExternalStore(subscribeMinute, () => minuteTick);
  return new Date();
}

export function useSecondTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

export function useDelayed(active: boolean, delay = 200): boolean {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!active) {
      setShown(false);
      return;
    }
    const timer = window.setTimeout(() => setShown(true), delay);
    return () => window.clearTimeout(timer);
  }, [active, delay]);
  return shown;
}

export function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function useTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} - eks-harness` : "eks-harness";
  }, [title]);
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    return fallbackCopy(text);
  }
  return fallbackCopy(text);
}

function fallbackCopy(text: string): boolean {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.className = "visually-hidden";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

export function isMac(): boolean {
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

export function useCopy(): [string | null, (text: string, key?: string) => Promise<void>] {
  const [state, setState] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const copy = useCallback(async (text: string, key = "default") => {
    const ok = await copyText(text);
    setState(ok ? key : `fail:${key}`);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState(null), 2000);
  }, []);
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  return [state, copy];
}

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (target as HTMLInputElement).type;
    return !["checkbox", "radio", "button", "submit", "reset", "range"].includes(type);
  }
  return false;
}

export function overlayOpen(): boolean {
  return Boolean(document.querySelector("[data-overlay-open]"));
}

export function shortcutAllowed(event: KeyboardEvent): boolean {
  if (event.defaultPrevented) return false;
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  if (isTypingTarget(event.target)) return false;
  if (overlayOpen()) return false;
  return true;
}

export function useKeydown(handler: (event: KeyboardEvent) => void, enabled = true): void {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    if (!enabled) return;
    const listener = (event: KeyboardEvent) => ref.current(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [enabled]);
}

export function useResultText(duration: number | null = null): [string, (text: string) => void] {
  const [text, setText] = useState("");
  const timer = useRef<number | null>(null);
  const set = useCallback(
    (value: string) => {
      setText(value);
      if (timer.current !== null) window.clearTimeout(timer.current);
      if (duration && value) timer.current = window.setTimeout(() => setText(""), duration);
    },
    [duration],
  );
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  return [text, set];
}

const announceListeners = new Set<(text: string) => void>();

export function announce(text: string): void {
  for (const listener of announceListeners) listener(text);
}

export function useAnnouncer(): string {
  const [text, setText] = useState("");
  useEffect(() => {
    const listener = (value: string) => {
      setText("");
      window.setTimeout(() => setText(value), 30);
    };
    announceListeners.add(listener);
    return () => {
      announceListeners.delete(listener);
    };
  }, []);
  return text;
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (listener: () => void) => {
      const media = window.matchMedia(query);
      media.addEventListener("change", listener);
      return () => media.removeEventListener("change", listener);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches);
}

export const PHONE_MAX = 699;
export const DESKTOP_MIN = 1100;

export function useIsDesktop(): boolean {
  return useMediaQuery(`(min-width: ${DESKTOP_MIN}px)`);
}

export function useIsSmall(): boolean {
  return useMediaQuery(`(max-width: ${PHONE_MAX}px)`);
}

export function useIsTablet(): boolean {
  return useMediaQuery(`(min-width: ${PHONE_MAX + 1}px) and (max-width: ${DESKTOP_MIN - 1}px)`);
}

export function nowTime(): string {
  const d = new Date();
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}
