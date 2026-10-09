import type { ErrorResponse, ValidationProblem } from "./types";

export const CSRF_HEADER = "X-CSRF-Token";
const TIMEOUT_MS = 10_000;

let csrfToken: string | null = null;
let unauthorizedHandler: ((path: string) => void) | null = null;
const connectionListeners = new Set<(ok: boolean) => void>();

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function onUnauthorized(handler: (path: string) => void): void {
  unauthorizedHandler = handler;
}

export function onConnectionChange(listener: (ok: boolean) => void): () => void {
  connectionListeners.add(listener);
  return () => connectionListeners.delete(listener);
}

function reportConnection(ok: boolean): void {
  for (const listener of connectionListeners) listener(ok);
}

export class ApiError extends Error {
  readonly status: number;
  readonly error: string;
  readonly body: ErrorResponse | null;

  constructor(status: number, error: string, message: string, body: ErrorResponse | null = null) {
    super(message);
    this.status = status;
    this.error = error;
    this.body = body;
  }

  get problems(): ValidationProblem[] {
    const raw = this.body?.problems;
    return Array.isArray(raw) ? (raw as ValidationProblem[]) : [];
  }

  get isNetwork(): boolean {
    return this.status === 0;
  }
}

export type Query = Record<string, string | number | boolean | null | undefined | string[]>;

export function buildQuery(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    if (Array.isArray(value)) {
      if (value.length) params.set(key, value.join(","));
      continue;
    }
    params.set(key, value === true ? "1" : String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function isUnsafe(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

export function authHeaders(method: string): Record<string, string> {
  const headers: Record<string, string> = {};
  if (isUnsafe(method) && csrfToken) headers[CSRF_HEADER] = csrfToken;
  return headers;
}

async function parseError(response: Response): Promise<ApiError> {
  let body: ErrorResponse | null = null;
  try {
    const text = await response.text();
    if (text) {
      const parsed = JSON.parse(text) as unknown;
      if (parsed && typeof parsed === "object") body = parsed as ErrorResponse;
    }
  } catch {
    body = null;
  }
  const error = typeof body?.error === "string" ? body.error : `http_${response.status}`;
  const message = typeof body?.message === "string" && body.message ? body.message : response.statusText || `HTTP ${response.status}`;
  return new ApiError(response.status, error, message, body);
}

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number | null;
  handleUnauthorized?: boolean;
  headers?: Record<string, string>;
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const url = path + buildQuery(options.query);
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders(method), ...options.headers };
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs === undefined ? TIMEOUT_MS : options.timeoutMs;
  let timedOut = false;
  const timer = timeoutMs
    ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs)
    : null;
  const outer = options.signal;
  const forward = () => controller.abort();
  outer?.addEventListener("abort", forward);
  let response: Response;
  try {
    response = await fetch(url, { method, headers, body, credentials: "same-origin", signal: controller.signal });
  } catch (err) {
    if (outer?.aborted) throw err;
    if (timedOut) {
      reportConnection(false);
      throw new ApiError(0, "timeout", "The daemon did not answer within 10 s. Try again.");
    }
    reportConnection(false);
    throw new ApiError(0, "network", "Could not reach the daemon. Check that it is running with eks-harness daemon status.");
  } finally {
    if (timer !== null) window.clearTimeout(timer);
    outer?.removeEventListener("abort", forward);
  }
  reportConnection(true);
  if (!response.ok) {
    const error = await parseError(response);
    if (response.status === 401 && options.handleUnauthorized !== false && unauthorizedHandler) {
      unauthorizedHandler(window.location.pathname + window.location.search);
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>("GET", path, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("POST", path, { ...options, body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("PUT", path, { ...options, body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>("PATCH", path, { ...options, body }),
  delete: <T>(path: string, options?: RequestOptions) => request<T>("DELETE", path, options),
};

export async function fetchText(url: string, maxBytes: number, signal?: AbortSignal): Promise<{ text: string; total: number; truncated: boolean }> {
  const head = await fetch(url, { method: "HEAD", credentials: "same-origin", signal }).catch(() => null);
  const total = Number(head?.headers.get("content-length") ?? "0") || 0;
  if (total > maxBytes) {
    const response = await fetch(url, { credentials: "same-origin", signal, headers: { Range: `bytes=${total - maxBytes}-` } });
    if (!response.ok && response.status !== 206) throw await parseError(response);
    let text = await response.text();
    const firstBreak = text.indexOf("\n");
    if (firstBreak >= 0) text = text.slice(firstBreak + 1);
    return { text, total, truncated: true };
  }
  const response = await fetch(url, { credentials: "same-origin", signal });
  if (!response.ok) throw await parseError(response);
  const text = await response.text();
  return { text, total: total || text.length, truncated: false };
}

export interface UploadHandle<T> {
  promise: Promise<T>;
  abort: () => void;
}

export function uploadForm<T>(path: string, form: FormData, onProgress: (loaded: number, total: number) => void): UploadHandle<T> {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<T>((resolve, reject) => {
    xhr.open("POST", path);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    if (csrfToken) xhr.setRequestHeader(CSRF_HEADER, csrfToken);
    xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : 0);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const err = (body ?? {}) as ErrorResponse;
      if (xhr.status === 401 && unauthorizedHandler) unauthorizedHandler(window.location.pathname + window.location.search);
      reject(new ApiError(xhr.status, typeof err.error === "string" ? err.error : `http_${xhr.status}`, typeof err.message === "string" ? err.message : `HTTP ${xhr.status}`, err));
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "the connection closed"));
    xhr.onabort = () => reject(new ApiError(0, "aborted", "stopped"));
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

export function errorText(err: unknown, verb: string, object: string, projectId?: string): string {
  if (!(err instanceof ApiError)) {
    return `Could not ${verb} ${object}: ${err instanceof Error ? err.message : String(err)}.`;
  }
  if (err.error === "network") return "Could not reach the daemon. Check that it is running with eks-harness daemon status.";
  if (err.error === "timeout") return "The daemon did not answer within 10 s. Try again.";
  if (err.status === 403 && (err.error === "csrf" || err.error === "csrf_failed")) {
    return "The page was open too long and its security token expired. Reload the page and try again.";
  }
  if (err.status === 403) {
    return projectId
      ? `You do not have permission to ${verb} in ${projectId}. Ask an admin for editor access.`
      : `You do not have permission to ${verb} ${object}. Ask an admin for access.`;
  }
  if (err.status === 404) return `${capitalize(object)} no longer exists. It may have been deleted.`;
  if (err.status === 410 && err.error === "lease_released") {
    const reacquire = typeof err.body?.reacquire === "string" ? err.body.reacquire : "";
    const sid = typeof err.body?.sid === "string" ? err.body.sid : "";
    return `The lease ${sid} was released. Reacquire with: ${reacquire}`;
  }
  if (err.status === 413) return `${capitalize(object)} is larger than the upload limit (storage.maxUploadMb).`;
  if (err.status >= 500) return `The daemon failed to ${verb} ${object} (${err.status}). Details are in eks-harness daemon logs.`;
  const message = err.message.replace(/\.$/, "");
  return `Could not ${verb} ${object}: ${message} (${err.status}).`;
}

function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

export function encodeSegment(value: string): string {
  return encodeURIComponent(value);
}

export function projectPath(projectId: string): string {
  const [owner, name] = splitProject(projectId);
  return `/api/projects/${encodeSegment(owner)}/${encodeSegment(name)}`;
}

export function splitProject(projectId: string): [string, string] {
  const index = projectId.indexOf("/");
  return index < 0 ? [projectId, ""] : [projectId.slice(0, index), projectId.slice(index + 1)];
}
