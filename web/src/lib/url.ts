import { useNavigate, useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { useDebounced } from "./hooks";

export type SearchParams = Record<string, string>;

export const PARAM_ORDER = [
  "next", "method",
  "q", "kind", "tag", "unseen", "project", "sort", "dir", "view", "cursor", "sel",
  "new", "upload", "types", "at", "lease", "tab", "since",
  "zoom", "t", "vw", "path", "find", "line", "wrap",
  "hq", "hstatus", "htype", "req", "htab",
  "grep", "lines", "follow", "process", "user", "notice",
];

export function parseSearch(search: string): SearchParams {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const out: SearchParams = {};
  params.forEach((value, key) => {
    out[key] = value;
  });
  return out;
}

export function stringifySearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams();
  const keys = Object.keys(search).filter((key) => {
    const value = search[key];
    return value !== undefined && value !== null && value !== "";
  });
  keys.sort((a, b) => {
    const ia = PARAM_ORDER.indexOf(a);
    const ib = PARAM_ORDER.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib) || a.localeCompare(b);
  });
  for (const key of keys) params.set(key, String(search[key]));
  const text = params.toString().replace(/%2C/g, ",").replace(/%2F/g, "/");
  return text ? `?${text}` : "";
}

export function useSearchParams(): SearchParams {
  return useRouterState({ select: (state) => state.location.search as SearchParams });
}

export function usePathname(): string {
  return useRouterState({ select: (state) => state.location.pathname });
}

export type ParamPatch = Record<string, string | number | boolean | null | undefined>;

export function applyPatch(current: SearchParams, patch: ParamPatch): SearchParams {
  const next: SearchParams = { ...current };
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined || value === "" || value === false) delete next[key];
    else next[key] = value === true ? "1" : String(value);
  }
  return next;
}

export function useSetParams(): (patch: ParamPatch, options?: { replace?: boolean; path?: string }) => void {
  const navigate = useNavigate();
  const pathname = usePathname();
  const current = useSearchParams();
  return useCallback(
    (patch, options) => {
      const next = applyPatch(current, patch);
      void navigate({ to: options?.path ?? pathname, search: next as never, replace: options?.replace ?? false });
    },
    [navigate, pathname, current],
  );
}

export function hrefWith(path: string, params: SearchParams | ParamPatch): string {
  const clean: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "" || value === false) continue;
    clean[key] = value === true ? "1" : value;
  }
  return path + stringifySearch(clean);
}

export function pick(params: SearchParams, keys: string[]): SearchParams {
  const out: SearchParams = {};
  for (const key of keys) if (params[key]) out[key] = params[key];
  return out;
}

export function projectUrl(projectId: string): string {
  const index = projectId.indexOf("/");
  const owner = index < 0 ? projectId : projectId.slice(0, index);
  const name = index < 0 ? "" : projectId.slice(index + 1);
  return `/p/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
}

export function sharedSessionUrl(slug: string): string {
  return `/sessions/${encodeURIComponent(slug)}`;
}

export function sessionUrl(projectId: string | null, slug: string | null | undefined): string {
  if (projectId === null) return slug ? sharedSessionUrl(slug) : "/sessions";
  if (!slug || slug === "_project") return `${projectUrl(projectId)}/files`;
  return `${projectUrl(projectId)}/s/${encodeURIComponent(slug)}`;
}

export function sharedArtifactUrl(slug: string, id: string): string {
  return `${sharedSessionUrl(slug)}/a/${encodeURIComponent(id)}`;
}

export function artifactUrl(projectId: string, slug: string | null | undefined, id: string): string {
  if (!slug || slug === "_project") return `${projectUrl(projectId)}/a/${encodeURIComponent(id)}`;
  return `${projectUrl(projectId)}/s/${encodeURIComponent(slug)}/a/${encodeURIComponent(id)}`;
}

export function localPath(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.pathname + parsed.search;
  } catch {
    return url;
  }
}

export function absoluteUrl(path: string): string {
  try {
    return new URL(path, window.location.origin).toString();
  } catch {
    return path;
  }
}

export function safeNext(next: string | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login")) return "/";
  return next;
}

export const LIST_CONTEXT_KEYS = ["q", "kind", "tag", "unseen", "sort", "dir"];

export const SHARED_CONTEXT_KEYS = [...LIST_CONTEXT_KEYS, "project"];

export function useSyncedText(name: string, delay = 200, extra: ParamPatch = {}): [string, (value: string) => void] {
  const params = useSearchParams();
  const setParams = useSetParams();
  const urlValue = params[name] ?? "";
  const [text, setText] = useState(urlValue);
  const debounced = useDebounced(text, delay);
  const lastUrl = useRef(urlValue);
  useEffect(() => {
    if (urlValue !== lastUrl.current) {
      lastUrl.current = urlValue;
      setText(urlValue);
    }
  }, [urlValue]);
  useEffect(() => {
    if (debounced !== urlValue && debounced === text) {
      lastUrl.current = debounced;
      setParams({ [name]: debounced || null, ...extra }, { replace: true });
    }
  }, [debounced]);
  return [text, setText];
}
