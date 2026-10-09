import { useSyncExternalStore } from "react";

const KEY = "ehx.currentProject";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

let current = read();

export function setCurrentProject(project: string | null): void {
  current = project;
  try {
    if (project) window.localStorage.setItem(KEY, project);
    else window.localStorage.removeItem(KEY);
  } catch {
    current = project;
  }
  for (const listener of listeners) listener();
}

export function useCurrentProject(): [string | null, (project: string | null) => void] {
  const value = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
  return [value, setCurrentProject];
}
