type Method = "*" | "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS";

type RouteAction =
  | { kind: "delay"; by: number }
  | { kind: "respond"; status?: number; json?: unknown; body?: string | null; headers?: Record<string, string> | null }
  | { kind: "patch"; path: string; value: unknown }
  | { kind: "abort"; reason?: string }
  | { kind: "passthrough" };

type Route = { id: string; method: Method; pattern: string; matcher: (url: string) => boolean; action: RouteAction; times: number | null; used: number };

type Profile = { latency?: number; down_kbps?: number | null; up_kbps?: number | null; loss?: number; offline?: boolean };

type CassetteEntry = { key: string; method: string; url: string; status: number; headers: Record<string, string>; body: string };

type Cassette = { mode: "record" | "replay"; entries: CassetteEntry[]; queues: Map<string, CassetteEntry[]>; misses: string[] };

type Rx = {
  _method?: string | null;
  _url?: string | null;
  _responseType?: string;
  _requestId?: number | null;
  _sent?: boolean;
  readyState: number;
  OPENED: number;
  __didReceiveResponse: (id: number, status: number, headers: Record<string, string> | null, url?: string) => void;
  __didReceiveData: (id: number, data: unknown) => void;
  __didReceiveIncrementalData: (id: number, text: string, progress: number, total: number) => void;
  __didCompleteResponse: (id: number, error: string, timedOut: boolean) => void;
  [key: string]: unknown;
};

const routes: Route[] = [];
let profile: Profile | null = null;
let lossSeen = 0;
let cassette: Cassette | null = null;
let fakeIds = 0;
let ignored: (url: string) => boolean = () => false;
let installed = false;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function globToRegExp(glob: string): RegExp {
  let source = "";
  for (let index = 0; index < glob.length; index++) {
    const char = glob[index];
    if (char === "*") {
      source += ".*";
      while (glob[index + 1] === "*") index++;
    } else if (char === "?") {
      source += ".";
    } else {
      source += char.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`);
}

export function urlMatcher(pattern: string): (url: string) => boolean {
  const regex = globToRegExp(pattern);
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(pattern)) return (url) => regex.test(url);
  if (pattern.startsWith("/")) {
    return (url) => {
      const match = url.match(/^[a-z][a-z0-9+.-]*:\/\/[^/]+(\/[^#]*)?/i);
      const rest = match?.[1] ?? "/";
      return regex.test(rest) || regex.test(rest.split("?")[0]);
    };
  }
  const loose = globToRegExp(`*${pattern}*`);
  return (url) => regex.test(url) || loose.test(url);
}

function setPath(root: any, dotted: string, value: unknown): any {
  const parts = String(dotted).match(/[^.[\]]+/g) ?? [];
  if (!parts.length) return value;
  let node = root;
  for (let index = 0; index < parts.length - 1; index++) {
    const key: string | number = /^\d+$/.test(parts[index]) ? Number(parts[index]) : parts[index];
    if (node[key] === null || typeof node[key] !== "object") node[key] = /^\d+$/.test(parts[index + 1]) ? [] : {};
    node = node[key];
  }
  const last = parts[parts.length - 1];
  node[/^\d+$/.test(last) ? Number(last) : last] = value;
  return root;
}

function hash(text: string): string {
  let h = 2166136261;
  for (let index = 0; index < text.length; index++) {
    h ^= text.charCodeAt(index);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}

function requestKey(method: string, url: string, body: unknown): string {
  return `${method} ${url} ${typeof body === "string" && body ? hash(body) : ""}`;
}

function textual(xhr: Rx): boolean {
  const type = xhr._responseType ?? "";
  return type === "" || type === "text" || type === "json" || type === "blob";
}

function blobManager(): any {
  return require("react-native/Libraries/Blob/BlobManager").default ?? require("react-native/Libraries/Blob/BlobManager");
}

function payload(xhr: Rx, text: string, type: string): unknown {
  if (xhr._responseType !== "blob") return text;
  return blobManager().createFromParts([text], { type }).data;
}

function readText(data: unknown): Promise<string> {
  if (typeof data === "string") return Promise.resolve(data);
  if (!data || typeof data !== "object") return Promise.resolve("");
  const blob = blobManager().createFromOptions(data);
  return new Promise((resolve) => {
    const reader = new (globalThis as any).FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => resolve("");
    reader.readAsText(blob);
  });
}

function fakeReply(xhr: Rx, reply: { status: number; headers: Record<string, string>; body: string } | { error: string; timedOut?: boolean }, delay: number) {
  const id = -(++fakeIds);
  xhr._sent = true;
  xhr._requestId = id;
  const url = xhr._url ?? "";
  setTimeout(() => {
    if ("error" in reply) {
      xhr.__didCompleteResponse(id, reply.error, Boolean(reply.timedOut));
      return;
    }
    xhr.__didReceiveResponse(id, reply.status, reply.headers, url);
    const type = reply.headers["content-type"] ?? reply.headers["Content-Type"] ?? "application/octet-stream";
    xhr.__didReceiveData(id, payload(xhr, reply.body, type));
    xhr.__didCompleteResponse(id, "", false);
  }, Math.max(0, delay));
}

function matchRoute(method: string, url: string): Route | null {
  for (let index = routes.length - 1; index >= 0; index--) {
    const route = routes[index];
    if (route.method !== "*" && route.method !== method) continue;
    if (!route.matcher(url)) continue;
    if (route.times !== null && route.used >= route.times) continue;
    return route;
  }
  return null;
}

function transferDelay(bytes: number, kbps: number | null | undefined): number {
  return kbps ? Math.round((bytes * 8) / kbps) : 0;
}

function wrap(xhr: Rx, method: string, url: string, body: unknown, patch: { path: string; value: unknown } | null) {
  const record = cassette?.mode === "record" ? cassette : null;
  if (!patch && !record && !profile?.down_kbps) return;
  const original = {
    response: xhr.__didReceiveResponse,
    data: xhr.__didReceiveData,
    incremental: xhr.__didReceiveIncrementalData,
    complete: xhr.__didCompleteResponse,
  };
  let status = 0;
  let headers: Record<string, string> = {};
  const chunks: unknown[] = [];
  const buffering = Boolean(patch);
  xhr.__didReceiveResponse = function (id, st, h, u) {
    status = st;
    headers = { ...(h ?? {}) };
    return original.response.call(this, id, st, h, u);
  };
  xhr.__didReceiveData = function (id, data) {
    chunks.push(data ?? "");
    if (!buffering) return original.data.call(this, id, data);
  };
  xhr.__didReceiveIncrementalData = function (id, text, progress, total) {
    chunks.push(String(text ?? ""));
    if (!buffering) return original.incremental.call(this, id, text, progress, total);
  };
  xhr.__didCompleteResponse = function (id, error, timedOut) {
    const self = this;
    const done = async () => {
      const parts = await Promise.all(chunks.map(readText));
      let text = parts.join("");
      if (patch && !error) {
        try {
          text = JSON.stringify(setPath(JSON.parse(text), patch.path, patch.value));
        } catch {}
        const type = headers["content-type"] ?? headers["Content-Type"] ?? "application/json";
        original.data.call(self, id, payload(xhr, text, type));
      }
      if (record && !error) record.entries.push({ key: requestKey(method, url, body), method, url, status, headers, body: text });
      const finish = () => original.complete.call(self, id, error, timedOut);
      const wait = transferDelay(text.length, profile?.down_kbps);
      if (wait > 0) setTimeout(finish, wait);
      else finish();
    };
    void done();
  };
}

async function sendThrough(xhr: Rx, send: (data: unknown) => void, data: unknown) {
  const method = String(xhr._method ?? "GET").toUpperCase();
  const url = String(xhr._url ?? "");
  if (ignored(url)) return send.call(xhr, data);
  const latency = (profile?.latency ?? 0) + transferDelay(typeof data === "string" ? data.length : 0, profile?.up_kbps);
  if (profile?.offline) return fakeReply(xhr, { error: "The Internet connection appears to be offline." }, latency);
  if (profile?.loss && profile.loss > 0) {
    lossSeen++;
    if (lossSeen % Math.max(1, Math.round(1 / profile.loss)) === 0) return fakeReply(xhr, { error: "The network connection was lost." }, latency);
  }
  if (cassette?.mode === "replay" && textual(xhr)) {
    const key = requestKey(method, url, data);
    const queue = cassette.queues.get(key);
    const entry = queue && queue.length > 1 ? queue.shift() : queue?.[0];
    if (!entry) {
      cassette.misses.push(key);
      return fakeReply(xhr, { error: `net.cassette: ${key} is not in the cassette` }, latency);
    }
    return fakeReply(xhr, { status: entry.status, headers: entry.headers, body: entry.body }, latency);
  }
  const route = matchRoute(method, url);
  if (route) route.used++;
  const action = route?.action;
  let delay = latency;
  let patch: { path: string; value: unknown } | null = null;
  if (action?.kind === "delay") delay += action.by;
  if (action?.kind === "respond" && textual(xhr)) {
    const body = action.json !== undefined && action.json !== null ? JSON.stringify(action.json) : String(action.body ?? "");
    const headers = { ...(action.json !== undefined && action.json !== null ? { "content-type": "application/json" } : {}), ...(action.headers ?? {}) };
    return fakeReply(xhr, { status: action.status ?? 200, headers, body }, delay);
  }
  if (action?.kind === "abort") return fakeReply(xhr, { error: `net.route: aborted (${action.reason ?? "failed"})`, timedOut: action.reason === "timedout" }, delay);
  if (action?.kind === "patch" && textual(xhr)) patch = { path: action.path, value: action.value };
  wrap(xhr, method, url, data, patch);
  if (delay > 0) await sleep(delay);
  return send.call(xhr, data);
}

class NetworkFailure extends TypeError {
  constructor(message: string) {
    super(`Network request failed: ${message}`);
  }
}

function responseOf(body: string, status: number, headers: Record<string, string>): Response {
  const ResponseClass = (globalThis as any).Response;
  return new ResponseClass(body, { status, headers });
}

function headersOf(response: Response): Record<string, string> {
  const out: Record<string, string> = {};
  response.headers?.forEach?.((value: string, key: string) => {
    out[key] = value;
  });
  return out;
}

async function fetchThrough(original: typeof fetch, input: any, init: any): Promise<Response> {
  const url = typeof input === "string" ? input : String(input?.url ?? input);
  const method = String(init?.method ?? input?.method ?? "GET").toUpperCase();
  const body = init?.body;
  if (ignored(url)) return original(input, init);
  const latency = (profile?.latency ?? 0) + transferDelay(typeof body === "string" ? body.length : 0, profile?.up_kbps);
  if (latency > 0) await sleep(latency);
  if (profile?.offline) throw new NetworkFailure("The Internet connection appears to be offline.");
  if (profile?.loss && profile.loss > 0) {
    lossSeen++;
    if (lossSeen % Math.max(1, Math.round(1 / profile.loss)) === 0) throw new NetworkFailure("The network connection was lost.");
  }
  if (cassette?.mode === "replay") {
    const key = requestKey(method, url, body);
    const queue = cassette.queues.get(key);
    const entry = queue && queue.length > 1 ? queue.shift() : queue?.[0];
    if (!entry) {
      cassette.misses.push(key);
      throw new NetworkFailure(`net.cassette: ${key} is not in the cassette`);
    }
    return responseOf(entry.body, entry.status, entry.headers);
  }
  const route = matchRoute(method, url);
  if (route) route.used++;
  const action = route?.action;
  if (action?.kind === "delay") await sleep(action.by);
  if (action?.kind === "respond") {
    const text = action.json !== undefined && action.json !== null ? JSON.stringify(action.json) : String(action.body ?? "");
    const headers = { ...(action.json !== undefined && action.json !== null ? { "content-type": "application/json" } : {}), ...(action.headers ?? {}) };
    return responseOf(text, action.status ?? 200, headers);
  }
  if (action?.kind === "abort") throw new NetworkFailure(`net.route aborted (${action.reason ?? "failed"})`);
  const record = cassette?.mode === "record" ? cassette : null;
  if (action?.kind !== "patch" && !record && !profile?.down_kbps) return original(input, init);
  const real = await original(input, init);
  let text = await real.text();
  if (action?.kind === "patch") {
    try {
      text = JSON.stringify(setPath(JSON.parse(text), action.path, action.value));
    } catch {}
  }
  const headers = headersOf(real);
  if (record) record.entries.push({ key: requestKey(method, url, body), method, url, status: real.status, headers, body: text });
  const wait = transferDelay(text.length, profile?.down_kbps);
  if (wait > 0) await sleep(wait);
  return responseOf(text, real.status, headers);
}

function installFetch() {
  const original = (globalThis as any).fetch as typeof fetch | undefined;
  if (typeof original !== "function" || (original as any).__ehxNetwork) return;
  const wrapped = function (input: any, init?: any) {
    if (!routes.length && !profile && !cassette) return original(input, init);
    return fetchThrough(original, input, init);
  };
  (wrapped as any).__ehxNetwork = true;
  (globalThis as any).fetch = wrapped;
}

export function installNetwork(isIgnored: (url: string) => boolean) {
  ignored = isIgnored;
  installFetch();
  const proto = (globalThis as any).XMLHttpRequest?.prototype;
  if (!proto || installed) return;
  installed = true;
  const send = proto.send;
  proto.send = function (this: Rx, data: unknown) {
    if (!routes.length && !profile && !cassette) return send.call(this, data);
    void sendThrough(this, send, data);
  };
}

let routeCounter = 0;

export function networkCommand(args: any): unknown {
  switch (args?.op) {
    case "route": {
      const route: Route = {
        id: `r${++routeCounter}`,
        method: (args.method ?? "*") as Method,
        pattern: String(args.pattern),
        matcher: urlMatcher(String(args.pattern)),
        action: args.action,
        times: typeof args.times === "number" ? args.times : null,
        used: 0,
      };
      routes.push(route);
      return { id: route.id };
    }
    case "unroute": {
      const before = routes.length;
      for (let index = routes.length - 1; index >= 0; index--) if (!args.pattern || routes[index].pattern === args.pattern) routes.splice(index, 1);
      return { removed: before - routes.length };
    }
    case "shape":
      profile = args.profile ?? null;
      lossSeen = 0;
      return { shaped: Boolean(profile) };
    case "cassette.start": {
      if (cassette) throw new Error("a cassette is already open");
      const queues = new Map<string, CassetteEntry[]>();
      for (const entry of (args.entries ?? []) as CassetteEntry[]) {
        if (!queues.has(entry.key)) queues.set(entry.key, []);
        queues.get(entry.key)!.push(entry);
      }
      cassette = { mode: args.mode === "replay" ? "replay" : "record", entries: [], queues, misses: [] };
      return { mode: cassette.mode };
    }
    case "cassette.stop": {
      const closed = cassette;
      cassette = null;
      return closed ? { mode: closed.mode, entries: closed.entries, misses: closed.misses } : { mode: null, entries: [], misses: [] };
    }
    case "reset":
      routes.length = 0;
      profile = null;
      cassette = null;
      return true;
    default:
      throw new Error(`unknown net op ${args?.op}`);
  }
}
