import crypto from "node:crypto";

export const HARNESS_BINDING = "__ehxReply";
export const CLOSE_NEW_DEBUGGER = "NEW_DEBUGGER_OPENED";

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

function frame(opcode, payload) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload ?? "", "utf8");
  let header;
  if (body.length < 126) {
    header = Buffer.from([0x80 | opcode, body.length]);
  } else if (body.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(body.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(body.length), 2);
  }
  return Buffer.concat([header, body]);
}

export function acceptWebSocket(req, socket, head) {
  const key = req.headers["sec-websocket-key"];
  if (!key || String(req.headers.upgrade).toLowerCase() !== "websocket") {
    socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
    return null;
  }
  const accept = crypto.createHash("sha1").update(key + WS_GUID).digest("base64");
  socket.write(
    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
  );
  socket.setNoDelay(true);

  const listeners = { message: [], close: [] };
  let buffer = head?.length ? Buffer.from(head) : Buffer.alloc(0);
  let fragments = [];
  let closed = false;

  const peer = {
    get open() {
      return !closed;
    },
    on(event, fn) {
      listeners[event]?.push(fn);
      return peer;
    },
    send(text) {
      if (!closed) socket.write(frame(0x1, text));
    },
    close(code = 1000, reason = "") {
      if (closed) return;
      const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
      payload.writeUInt16BE(code, 0);
      payload.write(reason, 2);
      socket.write(frame(0x8, payload));
      finish(code, reason);
      socket.end();
    },
  };

  function finish(code, reason) {
    if (closed) return;
    closed = true;
    for (const fn of listeners.close) fn(code, reason);
  }

  function parse() {
    for (;;) {
      if (buffer.length < 2) return;
      const fin = (buffer[0] & 0x80) !== 0;
      const opcode = buffer[0] & 0x0f;
      const masked = (buffer[1] & 0x80) !== 0;
      let length = buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffer.length < 4) return;
        length = buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (buffer.length < 10) return;
        length = Number(buffer.readBigUInt64BE(2));
        offset = 10;
      }
      const maskAt = offset;
      if (masked) offset += 4;
      if (buffer.length < offset + length) return;
      const payload = Buffer.from(buffer.subarray(offset, offset + length));
      if (masked) {
        for (let i = 0; i < payload.length; i++) payload[i] ^= buffer[maskAt + (i % 4)];
      }
      buffer = buffer.subarray(offset + length);
      if (opcode === 0x8) {
        const code = payload.length >= 2 ? payload.readUInt16BE(0) : 1005;
        const reason = payload.length > 2 ? payload.subarray(2).toString("utf8") : "";
        if (!closed) socket.write(frame(0x8, payload.subarray(0, 2)));
        finish(code, reason);
        socket.end();
        return;
      }
      if (opcode === 0x9) {
        socket.write(frame(0xa, payload));
        continue;
      }
      if (opcode === 0xa) continue;
      if (opcode === 0x1 || opcode === 0x2 || opcode === 0x0) {
        fragments.push(payload);
        if (!fin) continue;
        const text = Buffer.concat(fragments).toString("utf8");
        fragments = [];
        for (const fn of listeners.message) fn(text);
      }
    }
  }

  socket.on("data", (chunk) => {
    buffer = buffer.length ? Buffer.concat([buffer, chunk]) : chunk;
    parse();
  });
  socket.on("close", () => finish(1006, "socket closed"));
  socket.on("error", () => finish(1006, "socket error"));
  if (buffer.length) queueMicrotask(parse);
  return peer;
}

export async function listPages(metroPort, { timeout = 2000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(`http://127.0.0.1:${metroPort}/json/list`, { signal: controller.signal });
    if (!response.ok) return [];
    const pages = await response.json();
    return Array.isArray(pages) ? pages.filter((page) => page?.webSocketDebuggerUrl && !/Improved Chrome Reloads/.test(page.title ?? "")) : [];
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}

function deviceOf(page) {
  try {
    return page.reactNative?.logicalDeviceId ?? new URL(page.webSocketDebuggerUrl).searchParams.get("device") ?? page.webSocketDebuggerUrl;
  } catch {
    return page.webSocketDebuggerUrl;
  }
}

function pageNumber(page) {
  try {
    return Number(new URL(page.webSocketDebuggerUrl).searchParams.get("page")) || 0;
  } catch {
    return 0;
  }
}

export function runtimePages(pages) {
  const byDevice = new Map();
  for (const page of pages) {
    const device = deviceOf(page);
    byDevice.set(device, [...(byDevice.get(device) ?? []), page]);
  }
  return [...byDevice.entries()].map(([device, candidates]) => {
    const preferred = candidates.filter((page) => page.reactNative?.capabilities?.prefersFuseboxFrontend);
    const pool = preferred.length ? preferred : candidates;
    return { device, page: pool.reduce((best, page) => (pageNumber(page) > pageNumber(best) ? page : best)), pages: candidates };
  });
}

export function frontendUrl(metroPort, relayHostPath) {
  const params = new URLSearchParams([["ws", relayHostPath], ["sources.hide_add_folder", "true"]]);
  return `http://127.0.0.1:${metroPort}/debugger-frontend/rn_fusebox.html?${params}`;
}

const SWALLOWED_FROM_FRONTEND = new Set(["Runtime.disable", "Log.disable"]);

export class DevtoolsLink {
  static open(url, options = {}) {
    return new Promise((resolve, reject) => {
      const link = new DevtoolsLink(url, options);
      const timer = setTimeout(() => {
        link.close();
        reject(new Error(`could not open the DevTools connection ${url} within ${options.connectTimeout ?? 5000}ms`));
      }, options.connectTimeout ?? 5000);
      link.socket.addEventListener("open", () => {
        clearTimeout(timer);
        resolve(link);
      });
      link.socket.addEventListener("close", (event) => {
        clearTimeout(timer);
        reject(new Error(`the DevTools connection ${url} closed before it opened (${event.code} ${event.reason})`));
      });
    });
  }

  constructor(url, { onEvent, onClose } = {}) {
    this.url = url;
    this.onEvent = onEvent ?? (() => {});
    this.onClose = onClose ?? (() => {});
    this.nextId = 1;
    this.pending = new Map();
    this.frontends = new Set();
    this.relayed = new Map();
    this.closed = false;
    this.closeReason = null;
    const { protocol, host } = new URL(url);
    const origin = `${protocol === "wss:" ? "https:" : "http:"}//${host}`;
    this.socket = new WebSocket(url, { headers: { Origin: origin } });
    this.socket.addEventListener("message", (event) => this.receive(event.data));
    this.socket.addEventListener("close", (event) => this.finish(event.code, event.reason));
    this.socket.addEventListener("error", () => {});
  }

  get open() {
    return !this.closed && this.socket.readyState === WebSocket.OPEN;
  }

  send(method, params = {}, { timeout = 10_000 } = {}) {
    if (!this.open) return Promise.reject(new Error(`${method}: the DevTools connection is closed${this.closeReason ? ` (${this.closeReason})` : ""}`));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: no answer from the app within ${timeout}ms`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  receive(raw) {
    let message;
    try {
      message = JSON.parse(typeof raw === "string" ? raw : Buffer.from(raw).toString("utf8"));
    } catch {
      return;
    }
    if (message.id !== undefined) {
      const own = this.pending.get(message.id);
      if (own) {
        this.pending.delete(message.id);
        clearTimeout(own.timer);
        if (message.error) own.reject(new Error(`${own.method}: ${message.error.message ?? JSON.stringify(message.error)}`));
        else own.resolve(message.result ?? {});
        return;
      }
      const relayed = this.relayed.get(message.id);
      if (relayed) {
        this.relayed.delete(message.id);
        relayed.peer.send(JSON.stringify({ ...message, id: relayed.id }));
      }
      return;
    }
    if (!message.method) return;
    this.onEvent(message.method, message.params ?? {});
    if (message.method === "Runtime.bindingCalled" && message.params?.name === HARNESS_BINDING) return;
    const text = JSON.stringify(message);
    for (const frontend of this.frontends) frontend.peer.send(text);
  }

  attachFrontend(peer) {
    const frontend = { peer, debugger: false };
    this.frontends.add(frontend);
    peer.on("message", (raw) => {
      let request;
      try {
        request = JSON.parse(raw);
      } catch {
        return;
      }
      if (request.id === undefined || !request.method) return;
      const answerLocally =
        SWALLOWED_FROM_FRONTEND.has(request.method) ||
        (request.method === "Runtime.removeBinding" && request.params?.name === HARNESS_BINDING);
      if (answerLocally || !this.open) {
        peer.send(JSON.stringify(answerLocally ? { id: request.id, result: {} } : { id: request.id, error: { code: -32000, message: "the app is not connected" } }));
        return;
      }
      if (request.method === "Debugger.enable") frontend.debugger = true;
      const id = this.nextId++;
      this.relayed.set(id, { peer, id: request.id });
      this.socket.send(JSON.stringify({ ...request, id }));
    });
    peer.on("close", () => {
      this.frontends.delete(frontend);
      for (const [id, entry] of this.relayed) if (entry.peer === peer) this.relayed.delete(id);
      if (frontend.debugger && this.open) this.send("Debugger.disable", {}, { timeout: 5000 }).catch(() => {});
    });
    return frontend;
  }

  close(reason = "closed by the harness") {
    if (this.closed) return;
    try {
      this.socket.close(1000, reason);
    } catch {}
    this.finish(1000, reason);
  }

  finish(code, reason) {
    if (this.closed) return;
    this.closed = true;
    this.closeReason = reason || `code ${code}`;
    for (const [, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.reject(new Error(`${entry.method}: the DevTools connection closed (${this.closeReason})`));
    }
    this.pending.clear();
    for (const frontend of this.frontends) frontend.peer.close(1001, "the app's DevTools connection closed");
    this.frontends.clear();
    this.relayed.clear();
    this.onClose(code, reason ?? "");
  }
}
