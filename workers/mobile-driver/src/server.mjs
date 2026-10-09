import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { captureDevice, heartbeatSids, leaseReleased, linksOf, request as daemonRequest } from "../../kit/src/daemon-client.mjs";
import { createRunner, renderText } from "../../kit/src/script-runner.mjs";
import { CLOSE_NEW_DEBUGGER, DevtoolsLink, HARNESS_BINDING, acceptWebSocket, frontendUrl, listPages, runtimePages } from "../../kit/src/devtools-link.mjs";
import { loadWorkerConfig } from "../../kit/src/config.mjs";
import { loadExtensions } from "../../kit/src/extensions.mjs";

export const BRIDGE_GLOBAL = "__ehxBridge";

const CONFIG = loadWorkerConfig();
const PORT = Number(CONFIG.port ?? 0);
const STATE_DIR = CONFIG.stateDir || path.resolve(".ehx-mobile-state");
const FIXTURE_DIRS = (CONFIG.fixtureDirs ?? []).map((dir) => path.resolve(dir));
const LOG_FILE = path.join(STATE_DIR, "app.log");
const EVENT_FILE = path.join(STATE_DIR, "events.log");
const opt = (name, fallback) => (CONFIG.options && CONFIG.options[name] !== undefined ? CONFIG.options[name] : fallback);
let EXTENSIONS = [];
const EXTENSION_ROUTES = {};
fs.mkdirSync(STATE_DIR, { recursive: true });
const PLATFORMS = ["ios", "android"];
const SLOT_NAME = /^[a-z][a-z0-9_.-]{0,63}$/;
const FAKE_SLOTS = new Set(CONFIG.fakeSlots ?? ["nfc", "camera", "gallery", "document"]);
const state = Object.fromEntries([...FAKE_SLOTS].map((slot) => [slot, null]));
const platformFakes = { ios: {}, android: {} };
let lastAppContact = null;

function fakesFor(platform) {
  const own = platformFakes[platform];
  if (!own) return state;
  return Object.fromEntries([...FAKE_SLOTS].map((slot) => [slot, own[slot] !== undefined ? own[slot] : state[slot]]));
}

function armFake(platform, slot, spec) {
  if (!SLOT_NAME.test(String(slot))) throw new Error(`invalid fake slot ${JSON.stringify(slot)}`);
  FAKE_SLOTS.add(slot);
  if (!platform && state[slot] === undefined) state[slot] = null;
  if (platform) platformFakes[platform][slot] = spec;
  else state[slot] = spec;
}

function clearFakes(platform, slot) {
  const slots = slot ? [slot] : [...FAKE_SLOTS];
  if (platform) {
    for (const name of slots) delete platformFakes[platform][name];
    return;
  }
  for (const name of slots) {
    state[name] = null;
    for (const own of Object.values(platformFakes)) delete own[name];
  }
}

function tagOf(platform) {
  return PLATFORMS.includes(platform) ? ` [${platform}]` : "";
}

process.on("unhandledRejection", (error) => process.stdout.write(`unhandled rejection: ${error?.stack ?? error}\n`));
process.on("uncaughtException", (error) => process.stdout.write(`uncaught exception: ${error?.stack ?? error}\n`));

function resetLogs() {
  fs.writeFileSync(LOG_FILE, "");
  fs.writeFileSync(EVENT_FILE, "");
}
resetLogs();

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve({ _raw: raw });
      }
    });
  });
}

function json(res, code, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(code, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function resolveFixtureBase64(name) {
  const candidates = [name, ...FIXTURE_DIRS.map((dir) => path.join(dir, name)), path.join(process.cwd(), name)];
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
        return fs.readFileSync(candidate).toString("base64");
      }
    } catch {
    }
  }
  return null;
}

const METRO_PORT = Number(CONFIG.metroPort || 0);
const SIDS = { ...(CONFIG.sids ?? {}) };
const DISCOVER_MS = 500;
const RECONNECT_WAIT_MS = 15_000;
const DISCOVERY_GRACE_MS = 5000;

const clients = {};
const links = new Map();
const probing = new Set();
const kicked = new Map();
let callCounter = 0;

const isLive = (client) => Boolean(client?.entry?.link.open && client.session);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function sidPlatform(sid) {
  for (const platform of PLATFORMS) if (SIDS[platform] === sid) return platform;
  return null;
}

const AMBIGUOUS = "both the iOS and the Android app are connected to this driver - pass platform ios|android (or a sid)";

function resolvePlatform({ platform, sid } = {}) {
  if (platform && !PLATFORMS.includes(platform)) throw new Error(`unknown platform "${platform}" - pass --platform ios|android`);
  const owner = sid ? sidPlatform(sid) : null;
  if (platform && owner && owner !== platform) throw new Error(`sid ${sid} is this instance's ${owner} lease, not ${platform}`);
  if (platform || owner) return platform || owner;
  const live = PLATFORMS.filter((name) => isLive(clients[name]));
  if (live.length === 1) return live[0];
  if (live.length > 1) throw new Error(AMBIGUOUS);
  const known = PLATFORMS.filter((name) => clients[name] || lanes[name]);
  if (known.length === 1) return known[0];
  if (known.length > 1) throw new Error(AMBIGUOUS);
  return null;
}

function notConnected(platform) {
  const client = clients[platform];
  if (!METRO_PORT) return `the ${platform} app is not connected: this driver was started without metroPort, so it cannot reach React Native DevTools`;
  if (client?.kickedAt) {
    return `the ${platform} app is not connected: React Native DevTools opened directly from Metro (the j key or the dev menu) took its only debugger connection at ${new Date(client.kickedAt).toISOString()} and the driver could not take it back; open DevTools through the driver's /devtools frontend instead, which shares its connection`;
  }
  if (client) return `the ${platform} app is not connected: it is reloading or its DevTools connection dropped and did not come back within ${RECONNECT_WAIT_MS / 1000}s (is the app still running?)`;
  return `the ${platform} app is not connected: no page on Metro :${METRO_PORT} (React Native DevTools) runs a bundle with the @eks-harness/react-native bridge`;
}

function clientFor(platform) {
  if (platform) {
    const client = clients[platform];
    if (!isLive(client)) throw new Error(notConnected(platform));
    return client;
  }
  const live = PLATFORMS.map((name) => clients[name]).filter(isLive);
  if (live.length === 1) return live[0];
  if (live.length === 0) throw new Error("no app is connected to this driver");
  throw new Error("both the iOS and the Android app are connected - pass --platform ios|android");
}

async function until(condition, ms) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (condition()) return true;
    await sleep(100);
  }
  return condition();
}

async function linkedClient(platform) {
  const known = PLATFORMS.filter((each) => clients[each]);
  const name = platform ?? (known.length === 1 ? known[0] : null);
  const client = name ? clients[name] : null;
  if (name && !client && METRO_PORT) await until(() => isLive(clients[name]), DISCOVERY_GRACE_MS);
  if (client && !isLive(client)) {
    if (client.kickedAt && client.url && !client.entry) {
      appendEvent(name, "devtools.retake", { url: client.url, kickedAt: new Date(client.kickedAt).toISOString() });
      kicked.delete(client.url);
      await connectPage(client.url, client.title);
    }
    await until(() => isLive(clients[name]), RECONNECT_WAIT_MS);
  }
  return clientFor(platform);
}

function appendEvent(platform, event, payload) {
  fs.appendFileSync(EVENT_FILE, `${new Date().toISOString()}${tagOf(platform)} ${event} ${JSON.stringify(payload ?? {})}\n`);
}

function remoteText(value) {
  if (!value) return "";
  if (value.type === "string") return value.value;
  if (value.type === "undefined") return "undefined";
  if (value.value !== undefined) return typeof value.value === "string" ? value.value : JSON.stringify(value.value);
  if (value.unserializableValue !== undefined) return String(value.unserializableValue);
  const preview = value.preview;
  if (preview?.properties) {
    const parts = preview.properties.map((property) => (preview.subtype === "array" ? property.value : `${property.name}: ${property.type === "string" ? JSON.stringify(property.value) : property.value}`));
    const body = parts.join(", ") + (preview.overflow ? ", ..." : "");
    return preview.subtype === "array" ? `[${body}]` : `{${body}}`;
  }
  return value.description ?? value.className ?? value.type ?? "";
}

const CONSOLE_LEVELS = { log: "log", info: "info", debug: "debug", warning: "warn", error: "error", assert: "error", trace: "log" };

function appendLog(entry, level, message, at) {
  const line = { level, message, at: at ?? new Date().toISOString() };
  if (!entry.platform) {
    entry.early.push(line);
    if (entry.early.length > 500) entry.early.shift();
    return;
  }
  fs.appendFileSync(LOG_FILE, `${line.at}${tagOf(entry.platform)} [${line.level}] ${line.message}\n`);
}

function register(entry, info) {
  if (!PLATFORMS.includes(info?.platform) || !info.session) return;
  entry.platform = info.platform;
  const early = entry.early.splice(0);
  for (const line of early) appendLog(entry, line.level, line.message, line.at);
  const previous = clients[info.platform];
  if (previous?.entry && previous.entry !== entry) previous.entry.link.close("superseded by a newer page of the same app");
  lastAppContact = new Date().toISOString();
  if (previous && previous.entry === entry && previous.session === info.session) {
    previous.info = info;
    return;
  }
  clients[info.platform] = { platform: info.platform, session: info.session, info, entry, url: entry.url, title: entry.title, lastSeen: Date.now(), kickedAt: null };
  appendEvent(info.platform, "devtools.hello", { page: entry.title, url: entry.url, ...info });
}

async function probeHello(entry) {
  if (!entry.link.open) return;
  try {
    const result = await entry.link.send("Runtime.evaluate", {
      expression: `typeof globalThis.${BRIDGE_GLOBAL} === 'object' && globalThis.${BRIDGE_GLOBAL} ? globalThis.${BRIDGE_GLOBAL}.hello() : null`,
      returnByValue: true,
    }, { timeout: 5000 });
    if (result?.result?.value) register(entry, result.result.value);
  } catch {}
}

function failPending(entry, message) {
  for (const [id, pending] of entry.pending) {
    clearTimeout(pending.timer);
    pending.reject(new Error(`${pending.cmd}: ${message}`));
    entry.pending.delete(id);
  }
}

function onDevtoolsEvent(entry, method, params) {
  if (method === "Runtime.bindingCalled" && params.name === HARNESS_BINDING) {
    let message;
    try {
      message = JSON.parse(params.payload);
    } catch {
      return;
    }
    if (message.type === "hello") return register(entry, message.info);
    if (message.type === "event") return emitEvent(message.name, message.data, entry.platform ?? "app");
    const pending = entry.pending.get(message.id);
    if (!pending) return;
    entry.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (clients[entry.platform]) clients[entry.platform].lastSeen = Date.now();
    if (message.ok) pending.resolve(message.value);
    else pending.reject(new Error(`${pending.cmd}: ${message.error}`));
    return;
  }
  if (method === "Runtime.consoleAPICalled") {
    const level = CONSOLE_LEVELS[params.type] ?? "log";
    const at = typeof params.timestamp === "number" && params.timestamp > 1e12 ? new Date(params.timestamp).toISOString() : undefined;
    appendLog(entry, level, (params.args ?? []).map(remoteText).join(" "), at);
    return;
  }
  if (method === "Runtime.exceptionThrown") {
    const details = params.exceptionDetails ?? {};
    appendLog(entry, "error", `Uncaught ${details.exception ? remoteText(details.exception) : details.text ?? "exception"}`);
    return;
  }
  if (method === "Runtime.executionContextsCleared") {
    failPending(entry, "the app reloaded before answering");
    const client = clients[entry.platform];
    if (client?.entry === entry) client.session = null;
    return;
  }
  if (method === "Runtime.executionContextCreated") {
    setTimeout(() => {
      const client = clients[entry.platform];
      if (!client?.session || client.entry !== entry) probeHello(entry);
    }, 1000).unref();
    return;
  }
  if (method === "Debugger.paused") {
    entry.paused = params.reason ?? "paused";
    failPending(entry, `the app's JS is paused in React Native DevTools (${entry.paused}); resume it there`);
    return;
  }
  if (method === "Debugger.resumed") entry.paused = null;
}

function onDevtoolsClose(entry, reason) {
  links.delete(entry.url);
  failPending(entry, `the app's DevTools connection closed (${reason || "no reason"})`);
  const client = clients[entry.platform];
  const takenOver = String(reason).includes(CLOSE_NEW_DEBUGGER);
  if (takenOver) kicked.set(entry.url, Date.now());
  if (client?.entry === entry) {
    client.entry = null;
    if (takenOver) client.kickedAt = Date.now();
  }
  appendEvent(entry.platform, takenOver ? "devtools.taken-over" : "devtools.closed", { url: entry.url, reason });
}

async function connectPage(url, title) {
  if (probing.has(url) || links.has(url)) return;
  probing.add(url);
  try {
    const entry = { url, title, platform: null, pending: new Map(), paused: null, early: [] };
    entry.link = await DevtoolsLink.open(url, {
      onEvent: (method, params) => onDevtoolsEvent(entry, method, params),
      onClose: (code, reason) => onDevtoolsClose(entry, reason),
    });
    links.set(url, entry);
    await entry.link.send("ReactNativeApplication.enable", {}, { timeout: 5000 }).catch(() => {});
    await entry.link.send("Runtime.enable", {}, { timeout: 5000 });
    await entry.link.send("Runtime.addBinding", { name: HARNESS_BINDING }, { timeout: 5000 });
    await probeHello(entry);
  } catch (error) {
    process.stdout.write(`devtools: ${url}: ${error?.message ?? error}\n`);
  } finally {
    probing.delete(url);
  }
}

async function discover() {
  if (!METRO_PORT) return;
  const pages = await listPages(METRO_PORT);
  const present = new Set(pages.map((page) => page.webSocketDebuggerUrl));
  for (const url of kicked.keys()) if (!present.has(url)) kicked.delete(url);
  for (const { page, pages: devicePages } of runtimePages(pages)) {
    const busy = devicePages.some(({ webSocketDebuggerUrl: url }) => links.has(url) || probing.has(url) || kicked.has(url));
    if (!busy) connectPage(page.webSocketDebuggerUrl, page.title);
  }
}

if (METRO_PORT) {
  const loop = async () => {
    try {
      await discover();
    } finally {
      setTimeout(loop, DISCOVER_MS).unref();
    }
  };
  loop();
}

async function bridgeCall(cmd, args = {}, { platform, timeout = 20_000 } = {}) {
  const client = await linkedClient(platform);
  const entry = client.entry;
  if (entry.paused) throw new Error(`${cmd}: the ${client.platform} app's JS is paused in React Native DevTools (${entry.paused}); resume it there`);
  const id = `c${++callCounter}`;
  const payload = JSON.stringify({ id, cmd, args });
  return new Promise((resolve, reject) => {
    const fail = (error) => {
      const pending = entry.pending.get(id);
      if (!pending) return;
      clearTimeout(pending.timer);
      entry.pending.delete(id);
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error(`${cmd}: the ${client.platform} app did not answer within ${timeout}ms (JS thread busy or app reloading)`)), timeout);
    entry.pending.set(id, { resolve, reject, timer, cmd });
    entry.link
      .send("Runtime.evaluate", { expression: `globalThis.${BRIDGE_GLOBAL}.dispatch(${JSON.stringify(payload)})`, returnByValue: true }, { timeout })
      .then((result) => {
        if (result?.exceptionDetails) {
          const text = result.exceptionDetails.exception ? remoteText(result.exceptionDetails.exception) : result.exceptionDetails.text;
          fail(new Error(`${cmd}: the ${client.platform} app's harness bridge rejected the command (${text})`));
        }
      })
      .catch(fail);
  });
}

function scriptSid(lane) {
  if (!lane.sid) throw new Error(`this script captures media: run it with the ${lane.platform} lease sid`);
  return lane.sid;
}
const PACE_DEFAULTS = { moveMs: 300, dwellMs: 400, typeMsPerChar: 40, holdMs: 700, screenHoldMs: 1500, mobilePressMs: 250 };
const PACE_FAST = { moveMs: 0, dwellMs: 100, typeMsPerChar: 0, holdMs: 150, screenHoldMs: 400, mobilePressMs: 0 };
const paceEnv = (name, fallback) => {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer of ms (got ${raw})`);
  return value;
};
const PACE_ENV = {
  moveMs: paceEnv("EHX_PACE_MOVE_MS", PACE_DEFAULTS.moveMs),
  dwellMs: paceEnv("EHX_PACE_DWELL_MS", PACE_DEFAULTS.dwellMs),
  typeMsPerChar: paceEnv("EHX_PACE_TYPE_MS_PER_CHAR", PACE_DEFAULTS.typeMsPerChar),
  holdMs: paceEnv("EHX_PACE_HOLD_MS", PACE_DEFAULTS.holdMs),
  screenHoldMs: paceEnv("EHX_PACE_SCREEN_HOLD_MS", PACE_DEFAULTS.screenHoldMs),
  mobilePressMs: paceEnv("EHX_PACE_MOBILE_PRESS_MS", PACE_DEFAULTS.mobilePressMs),
  ...(CONFIG.pace ?? {}),
};
const PACE_KEYS = [["moveMs", "move_ms"], ["dwellMs", "dwell_ms"], ["typeMsPerChar", "type_ms_per_char"],
  ["holdMs", "hold_ms"], ["screenHoldMs", "screen_hold_ms"], ["mobilePressMs", "mobile_press_ms"]];
function parsePace(spec) {
  if (spec === undefined || spec === null || spec === "" || spec === "demo") return { name: "demo", ...PACE_ENV };
  if (spec === "fast") return { name: "fast", ...PACE_FAST };
  let object;
  try {
    object = typeof spec === "string" ? JSON.parse(spec) : spec;
  } catch {
    throw new Error(`--pace takes demo, fast or a JSON object (got ${JSON.stringify(spec)?.slice(0, 80)})`);
  }
  if (!object || typeof object !== "object" || Array.isArray(object)) {
    throw new Error("--pace takes demo, fast or a JSON object");
  }
  const known = new Set(PACE_KEYS.flat());
  for (const key of Object.keys(object)) {
    if (!known.has(key)) throw new Error(`unknown pace key ${JSON.stringify(key)}`);
  }
  const merged = { ...PACE_ENV };
  for (const [camel, snake] of PACE_KEYS) {
    const value = object[camel] ?? object[snake];
    if (value === undefined) continue;
    if (!Number.isInteger(value) || value < 0) throw new Error(`pace.${camel} must be a non-negative integer of ms`);
    merged[camel] = value;
  }
  return { name: typeof spec === "string" ? spec : "custom", ...merged };
}
const paceSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const pressArgs = (lane) => (lane.rec?.pace ? { pressMs: lane.rec.pace.mobilePressMs } : {});

async function pacedBridge(lane, cmd, args, { target, timeout = 20_000, screenChange = false, quiet = false, dwell = true } = {}) {
  const rec = lane.rec;
  const pace = rec?.pace;
  const started = Date.now();
  if (dwell && pace?.dwellMs) await paceSleep(pace.dwellMs);
  if (rec?.noPointer && (cmd === "press" || cmd === "fill") && args) args = { ...args, mark: false };
  else if (pace && (cmd === "press" || cmd === "fill") && args && args.glideMs === undefined) args = { ...args, glideMs: pace.moveMs };
  const touchedAt = Date.now();
  let value;
  let ok = true;
  let error;
  try {
    value = await bridgeCall(cmd, args, { platform: lane.platform, timeout });
  } catch (e) {
    ok = false;
    error = e?.message ?? String(e);
  }
  if (pace) await paceSleep(screenChange ? pace.screenHoldMs : pace.holdMs);
  if (rec && !quiet) {
    const entry = {
      t: Number((Math.max(0, touchedAt - rec.startedAt) / 1000).toFixed(2)),
      action: cmd,
      target: target ?? args?.target ?? null,
      page: rec.platform,
      ok,
      error: error?.slice(0, 300),
      ms: Date.now() - started,
    };
    rec.steps.push(entry);
    if (rec.steps.length > 500) rec.steps.shift();
    rec.enrich.push((async () => {
      try {
        const box = await bridgeCall("layout", { target: entry.target }, { platform: lane.platform, timeout: 8000 });
        if (box && typeof box.x === "number") {
          entry.boxes = [{ x: box.x, y: box.y, w: box.width, h: box.height, scale: 1 }];
        }
      } catch {}
    })());
  }
  if (!ok) throw new Error(error);
  return value;
}

const TYPE_MARK_MS = 1000;

function typingMs(lane, pace, { byDefault = false } = {}) {
  if (pace !== undefined && pace !== null && pace !== "") return parsePace(pace).typeMsPerChar;
  if (lane.rec?.typeByChar) return lane.rec.pace.typeMsPerChar;
  return byDefault ? PACE_ENV.typeMsPerChar : 0;
}

async function typeByCharacter(lane, target, characters, perCharMs) {
  const rec = lane.rec;
  if (rec?.pace?.dwellMs) await paceSleep(rec.pace.dwellMs);
  let marked = 0;
  for (let n = 1; n < characters.length; n++) {
    if (rec && Date.now() - marked >= TYPE_MARK_MS) {
      marked = Date.now();
      rec.steps.push({ t: Number(((marked - rec.startedAt) / 1000).toFixed(2)), action: "type", target, page: rec.platform, ok: true });
      if (rec.steps.length > 500) rec.steps.shift();
    }
    await bridgeCall("fill", { target, text: characters.slice(0, n).join(""), ...(n === 1 && rec?.pace && !rec.noPointer ? { glideMs: rec.pace.moveMs } : { mark: false }) }, { platform: lane.platform });
    await paceSleep(perCharMs);
  }
}

async function fillInput(lane, target, text, { submit = false, blur = false } = {}, perCharMs = 0) {
  const characters = typeof text === "string" ? Array.from(text) : [];
  if (perCharMs > 0 && characters.length > 1) {
    await typeByCharacter(lane, target, characters, perCharMs);
    return pacedBridge(lane, "fill", { target, text, submit, blur, glideMs: -1, mark: false }, { target, dwell: false });
  }
  return pacedBridge(lane, "fill", { target, text, submit, blur }, { target });
}

async function deviceShot(lane, name, { caption, tags = [], meta = {} } = {}) {
  const artifact = await captureDevice(scriptSid(lane), "screenshot", {
    caption: caption ?? name ?? "",
    tags,
    meta: { name: name ?? null, platform: lane.platform, ...meta },
    source: "agent",
  });
  return linksOf(artifact);
}

const call = (lane, cmd) => (args) => bridgeCall(cmd, args, { platform: lane.platform });

function readMs(...texts) {
  const chars = texts.filter(Boolean).join(" ").length;
  return Math.max(1600, Math.min(6000, 1000 + chars * 50));
}

let annoCounter = 0;

function annotationHelpers(lane) {
  const bc = (args) => bridgeCall("annotate", args, { platform: lane.platform, timeout: 10_000 });
  const note = (entry) => {
    if (!lane.rec) return;
    (lane.rec.annotations ??= []).push({ at: Number(((Date.now() - lane.rec.startedAt) / 1000).toFixed(2)), ...entry });
  };
  const show = async ({ op, key, args, hold, keep, text }) => {
    await bc({ op, key, ...args });
    note({ kind: op, key, text: text ?? null });
    const wait = hold ?? (lane.rec ? readMs(text, args.subtitle) : 0);
    if (wait > 0) await paceSleep(wait);
    if (!keep) {
      await bc({ op: "remove", key }).catch(() => {});
      if (lane.rec) await paceSleep(220);
    }
    return key;
  };
  return {
    grouped: true,
    beacon: async (color, { size, key = "ehx-beacon" } = {}) =>
      color ? bc({ op: "beacon", key, color, size }) : bc({ op: "remove", key }).catch(() => false),
    title: async (title, { subtitle, kicker, hold, keep = false, key = "title" } = {}) =>
      show({ op: "title", key, args: { title, subtitle, kicker }, hold: hold ?? (lane.rec ? Math.max(2200, readMs(title, subtitle)) : 0), keep, text: title }),
    caption: async (text, { step, position, hold, keep = true, key = "caption" } = {}) => {
      if (text === null || text === undefined || text === "") return bc({ op: "remove", key });
      return show({ op: "caption", key, args: { text, step, position }, hold: hold ?? (lane.rec ? 900 : 0), keep, text });
    },
    callout: async (target, text, { placement, hold, keep = false, key } = {}) =>
      show({ op: "callout", key: key ?? `c${++annoCounter}`, args: { target, text, placement }, hold, keep, text }),
    highlight: async (target, { hold, keep = false, key } = {}) =>
      show({ op: "highlight", key: key ?? `h${++annoCounter}`, args: { target }, hold: hold ?? (lane.rec ? 1600 : 0), keep }),
    spotlight: async (target, { text, placement, hold, keep = false, key } = {}) => {
      const name = key ?? `s${++annoCounter}`;
      await bc({ op: "spotlight", key: name, target });
      if (text) await bc({ op: "callout", key: `${name}-text`, target, text, placement });
      note({ kind: "spotlight", key: name, text: text ?? null });
      const wait = hold ?? (lane.rec ? readMs(text) : 0);
      if (wait > 0) await paceSleep(wait);
      if (!keep) {
        await bc({ op: "remove", key: name }).catch(() => {});
        if (text) await bc({ op: "remove", key: `${name}-text` }).catch(() => {});
        if (lane.rec) await paceSleep(220);
      }
      return name;
    },
    remove: async (key) => bc({ op: "remove", key }),
    clear: async (kind) => bc({ op: "clear", kind }),
    list: async () => bc({ op: "list" }),
  };
}

function scriptHelpers(lane) {
  const bc = (cmd, args, options = {}) => bridgeCall(cmd, args, { platform: lane.platform, ...options });
  const idle = (options) => bc("idle", options ?? {}, { timeout: (options?.timeout ?? 8000) + 5000 });
  const settled = async (value) => {
    await idle();
    return value;
  };
  return {
    tree: async (options = {}) => (await bc("tree", options)).tree,
    find: async (target) => bc("find", { target }),
    exists: async (target) => bc("exists", { target }),
    text: async (target) => bc("text", { target }),
    press: async (target, { long = false } = {}) => settled(await pacedBridge(lane, "press", { target, long, ...pressArgs(lane) }, { target })),
    longPress: async (target) => settled(await pacedBridge(lane, "press", { target, long: true, ...pressArgs(lane) }, { target })),
    fill: async (target, text, { pace, ...options } = {}) => settled(await fillInput(lane, target, text, options, typingMs(lane, pace))),
    type: async (target, text, { pace, ...options } = {}) => settled(await fillInput(lane, target, text, options, typingMs(lane, pace, { byDefault: true }))),
    submit: async (target) => settled(await pacedBridge(lane, "submit", { target }, { target })),
    toggle: async (target, value) => settled(await pacedBridge(lane, "toggle", { target, value, ...pressArgs(lane) }, { target })),
    invoke: async (target, prop, ...args) => settled(await bc("invoke", { target, prop, args })),
    scroll: async (target, options = {}) => settled(await bc("scroll", typeof target === "object" && target && !target.testID && !target.text ? target : { target, ...options })),
    navigate: async (name, params) => settled(await pacedBridge(lane, "navigate", { name, params }, { screenChange: true })),
    goBack: async () => settled(await pacedBridge(lane, "goBack", {}, { screenChange: true })),
    route: call(lane, "route"),
    routeNames: call(lane, "routeNames"),
    state: async (statePath) => bc("state", { path: statePath }),
    dispatch: async (action) => settled(await bc("dispatch", { action })),
    layout: async (target) => bc("layout", { target }),
    idle,
    waitFor: async (target, { timeout = 10_000, gone = false } = {}) => bc("waitFor", { target, timeout, gone }, { timeout: timeout + 5000 }),
    waitGone: async (target, { timeout = 10_000 } = {}) => bc("waitFor", { target, timeout, gone: true }, { timeout: timeout + 5000 }),
    evaluate: async (code) => bc("eval", { code: typeof code === "function" ? `return (${code.toString()})(ctx)` : code }),
    shot: async (name, { caption, tags } = {}) => {
      await idle({ timeout: 4000 });
      const client = clientFor(lane.platform);
      return deviceShot(lane, name || "shot", { caption, tags: [client.platform, ...(tags ?? [])] });
    },
    video: {
      grouped: true,
      start: async (name = "recording", { hold = 1000, caption, tags, pace, fromHere, noPointer } = {}) => {
        const client = await linkedClient(lane.platform);
        const resolved = parsePace(pace);
        const notes = [];
        if (!fromHere) {
          for (let attempt = 0; attempt < 10; attempt++) {
            const before = (await bc("route", {}).catch(() => null))?.name;
            if (before === undefined || before === null) break;
            try {
              await bc("goBack", {});
            } catch {
              break;
            }
            const after = (await bc("route", {}).catch(() => null))?.name;
            if (after === before) break;
          }
          await bc("idle", {}, { timeout: 15000 }).catch(() => {});
          await paceSleep(Number(opt('videoLeadMs', 250)));
          notes.push("recording starts from the home screen");
        } else {
          notes.push("recording starts here (--from-here)");
        }
        const rec = {
          name, platform: client.platform, startedAt: Date.now(),
          pace: resolved, paceName: resolved.name, fromHere: Boolean(fromHere), noPointer: Boolean(noPointer),
          typeByChar: pace !== undefined && pace !== null && pace !== "" && resolved.typeMsPerChar > 0,
          steps: [], enrich: [],
        };
        const previous = lane.rec;
        lane.rec = rec;
        let started;
        try {
          started = await captureDevice(scriptSid(lane), "video-start", {
            caption: caption ?? name,
            tags: [client.platform, ...(tags ?? [])],
            meta: { name, platform: client.platform, pace: resolved.name, fromHere: Boolean(fromHere) },
            source: "agent",
          });
        } catch (error) {
          if (lane.rec === rec) lane.rec = previous;
          throw error;
        }
        const recorderStart = Date.parse(started?.startedAt ?? "");
        if (Number.isFinite(recorderStart)) rec.startedAt = recorderStart;
        await new Promise((resolve) => setTimeout(resolve, hold));
        return { ...started, pace: resolved.name, fromHere: Boolean(fromHere), notes };
      },
      stop: async ({ trim, hold = 1000 } = {}) => {
        await idle({ timeout: 4000 });
        await new Promise((resolve) => setTimeout(resolve, hold));
        const rec = lane.rec;
        if (trim === undefined || trim === null) trim = !(rec?.annotations?.length);
        lane.rec = null;
        let steps = [];
        if (rec) {
          await Promise.allSettled(rec.enrich.splice(0));
          steps = rec.steps.map((entry) => ({
            t: entry.t,
            action: entry.action,
            target: entry.target && typeof entry.target === "object" ? entry.target : { value: entry.target },
            page: entry.page,
            ok: entry.ok !== false,
            ...(entry.error ? { error: String(entry.error).slice(0, 300) } : {}),
            ...(entry.ms !== undefined ? { ms: entry.ms } : {}),
            ...(entry.boxes ? { boxes: entry.boxes } : {}),
          }));
          try {
            fs.writeFileSync(path.join(STATE_DIR, `steps-${lane.platform}-${Date.now()}.jsonl`),
              steps.map((entry) => JSON.stringify(entry)).join("\n") + "\n");
          } catch {}
        }
        const artifact = await captureDevice(scriptSid(lane), "video-stop", {
          meta: { trim, pace: rec?.paceName ?? "demo", fromHere: Boolean(rec?.fromHere), steps, annotations: rec?.annotations ?? [] },
          source: "agent",
        });
        await bc("annotate", { op: "clear" }).catch(() => {});
        await bc("pointer", { op: "hide" }).catch(() => {});
        return { ...linksOf(artifact), annotations: rec?.annotations ?? [], steps: steps.length };
      },
    },
    closeDevMenu: async () => bc("closeDevMenu", {}),
    annotate: annotationHelpers(lane),
    pointer: async (target, { ms } = {}) => bc("pointer", { op: target ? "move" : "hide", target, ms }),
    logout: async () => {
      const container = await bc("logout", {}, { timeout: 15_000 });
      await idle();
      return container;
    },
    arm: async (slot, spec = {}) => {
      armFake(lane.platform, slot, { mode: "success", ...spec });
      return { armed: slot };
    },
    clearFakes: async () => {
      clearFakes(lane.platform);
      return true;
    },
    logs: async (limit = 60) => {
      try {
        return fs.readFileSync(LOG_FILE, "utf8").trim().split("\n").slice(-limit);
      } catch {
        return [];
      }
    },
    platform: async () => (await linkedClient(lane.platform)).platform,
    emit: async (name, data) => emitEvent(name, data, lane.platform),
    patch: async (target, change = {}) => bc("patch", { target, ...change }),
    unpatch: async (key) => bc("unpatch", { key }),
    inject: async (spec = {}) => bc("inject", spec),
    reload: async () => {
      const client = await linkedClient(lane.platform);
      const before = client.session;
      await client.entry.link.send("Page.reload", {}, { timeout: 10_000 });
      const back = await until(() => {
        const current = clients[client.platform];
        return isLive(current) && current.session !== before;
      }, 30_000);
      if (!back) throw new Error("the app did not come back within 30s after Page.reload");
      await bc("idle", {}, { timeout: 15_000 });
      return "reloaded";
    },
    ...extensionHelpers(lane),
  };
}

function logSize() {
  try {
    return fs.statSync(LOG_FILE).size;
  } catch {
    return 0;
  }
}

const OTHER_PLATFORM_LINE = /^\S+ \[(ios|android)\] /;

async function appSnapshot(lane, { reason, shot, label } = {}) {
  const client = await linkedClient(lane.platform);
  const out = { platform: client.platform };
  try {
    const info = await bridgeCall("snapshot", {}, { platform: lane.platform, timeout: 8000 });
    out.route = info.route;
    if (info.params) out.params = JSON.stringify(info.params).slice(0, 300);
    out.tree = info.tree;
  } catch (error) {
    out.tree = `(tree unavailable: ${error.message})`;
  }
  try {
    const size = logSize();
    if (size > lane.logMark) {
      const fd = fs.openSync(LOG_FILE, "r");
      const buffer = Buffer.alloc(Math.min(size - lane.logMark, 200_000));
      fs.readSync(fd, buffer, 0, buffer.length, size - buffer.length);
      fs.closeSync(fd);
      const counts = new Map();
      for (const line of buffer.toString("utf8").split("\n")) {
        const tagged = line.match(OTHER_PLATFORM_LINE);
        if (tagged && tagged[1] !== lane.platform) continue;
        const match = line.match(/\[(error|warn)\]\s*(.*)$/);
        if (!match) continue;
        const message = `[${match[1]}] ${match[2].replace(/^\[[0-9T:.\-Z]+\]\s*/, "").slice(0, 280)}`;
        counts.set(message, (counts.get(message) ?? 0) + 1);
      }
      if (counts.size) out.appErrors = [...counts].slice(-6).map(([message, count]) => (count > 1 ? `${message} (x${count})` : message));
    }
  } catch {
    out.appErrors = undefined;
  }
  if (shot || reason === "error") {
    if (!lane.sid) {
      out.shots = ["(no screenshot: run the script with --sid to capture pause and error shots)"];
    } else {
      try {
        out.shots = [await deviceShot(lane, label || reason || "pause", { tags: [client.platform, "pause"] })];
      } catch (error) {
        out.shots = [`(screenshot failed: ${error.message})`];
      }
    }
  }
  return out;
}

const lanes = {};

function laneFor(platform) {
  if (!lanes[platform]) {
    const lane = { platform, sid: null, rec: null, logMark: 0 };
    lane.runner = createRunner({
      helpers: () => scriptHelpers(lane),
      snapshot: (options) => appSnapshot(lane, options),
      log: (line) => process.stdout.write(`[runner ${platform}] ${line}\n`),
      idPrefix: `${platform}-`,
    });
    lanes[platform] = lane;
  }
  return lanes[platform];
}

function laneOf(body) {
  const platform = resolvePlatform(body);
  if (!platform) throw new Error("no app is connected to this driver");
  const lane = laneFor(platform);
  if (body.sid) lane.sid = body.sid;
  else if (!lane.sid && SIDS[platform]) lane.sid = SIDS[platform];
  return lane;
}

function existingLane(body) {
  const platform = resolvePlatform(body);
  return platform ? laneFor(platform) : null;
}

const RUN_ROUTES = {
  "/run": (body) => {
    const lane = laneOf(body);
    lane.sid = body.sid || SIDS[lane.platform] || null;
    lane.logMark = logSize();
    return lane.runner.start(String(body.script ?? ""), {
      breakBefore: body.breakBefore ?? [],
      pauseOnError: body.pauseOnError,
      snapshot: body.snapshot,
      shotAtEnd: body.shotAtEnd,
      waitMs: body.waitMs,
      force: body.force,
    });
  },
  "/continue": (body) => {
    const lane = existingLane(body);
    if (!lane) throw new Error("no run to continue");
    return lane.runner.resume(body.action ?? "continue", { waitMs: body.waitMs, breakBefore: body.breakBefore });
  },
  "/run-wait": (body) => existingLane(body)?.runner.wait(body.waitMs) ?? { status: "idle" },
  "/abort": (body) => existingLane(body)?.runner.abort() ?? { status: "idle" },
  "/run-status": (body) => existingLane(body)?.runner.status({ full: Boolean(body.full) }) ?? { status: "idle" },
  "/exec": (body) => laneOf(body).runner.exec(String(body.script ?? "")),
  "/tree": async (body) => {
    const lane = laneOf(body);
    const info = await bridgeCall("tree", { all: Boolean(body.all) }, { platform: lane.platform });
    return { ok: true, raw: `route: ${info.route}\n${info.tree}`, elapsedMs: 0, steps: [], logs: [] };
  },
};

async function handleBridge(req, res, pathName, url) {
  const route = RUN_ROUTES[pathName] ?? EXTENSION_ROUTES[pathName];
  if (route && req.method === "POST") {
    const body = await readBody(req);
    const asText = url.searchParams.get("format") === "text";
    try {
      const result = await route(body);
      if (!asText) return json(res, 200, result), true;
      res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(renderText(result));
    } catch (error) {
      if (!asText) return json(res, 500, { error: error?.message ?? String(error) }), true;
      res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(`error: ${error?.message ?? String(error)}\n`);
    }
    return true;
  }
  return false;
}

function leaseSids() {
  return [...new Set([...Object.values(SIDS), ...Object.values(lanes).map((lane) => lane.sid)].filter(Boolean))];
}

const beat = () => {
  const sids = leaseSids();
  if (!sids.length) return;
  heartbeatSids(sids).then(() => {
    for (const sid of sids) if (leaseReleased(sid)) process.stdout.write(`lease ${sid} is no longer held\n`);
  }).catch(() => {});
};
beat();
setInterval(beat, Number(opt("heartbeatMs", 20_000))).unref();

const server = http.createServer((req, res) => {
  handle(req, res).catch((error) => {
    process.stdout.write(`request ${req.url} failed: ${error?.stack ?? error}\n`);
    if (!res.headersSent) json(res, 500, { error: error?.message ?? String(error) });
  });
});

async function handle(req, res) {
  const url = new URL(req.url, "http://127.0.0.1");
  const pathName = url.pathname;

  if (await handleBridge(req, res, pathName, url)) return;

  if (req.method === "GET" && pathName === "/events") return streamEvents(req, res);

  if (req.method === "GET" && pathName === "/state") {
    if (url.searchParams.get("all")) {
      return json(res, 200, { shared: state, ios: fakesFor("ios"), android: fakesFor("android") });
    }
    lastAppContact = new Date().toISOString();
    return json(res, 200, fakesFor(url.searchParams.get("platform")));
  }

  if (req.method === "GET" && pathName.startsWith("/media-base64/")) {
    const name = decodeURIComponent(pathName.slice("/media-base64/".length));
    const base64 = resolveFixtureBase64(name);
    if (!base64) return json(res, 404, { error: "fixture not found", name });
    return json(res, 200, { base64 });
  }

  if (req.method === "POST" && pathName === "/app-events") {
    lastAppContact = new Date().toISOString();
    const body = await readBody(req);
    fs.appendFileSync(
      EVENT_FILE,
      `${body.at || new Date().toISOString()}${tagOf(url.searchParams.get("platform"))} ${body.event || "event"} ${JSON.stringify(body.payload ?? {})}\n`
    );
    emitEvent(body.event || "event", body.payload ?? null, url.searchParams.get("platform") || "app");
    return json(res, 200, { ok: true });
  }

  if (req.method === "POST" && pathName === "/arm") {
    const body = await readBody(req);
    const { slot, platform, ...rest } = body;
    if (!SLOT_NAME.test(String(slot ?? ""))) {
      return json(res, 400, { error: "invalid slot", slot });
    }
    if (platform && !PLATFORMS.includes(platform)) return json(res, 400, { error: "invalid platform", platform });
    armFake(platform, slot, rest);
    return json(res, 200, { ok: true, slot, platform: platform ?? "both", state: rest });
  }

  if (req.method === "POST" && pathName === "/clear") {
    const body = await readBody(req);
    if (body.platform && !PLATFORMS.includes(body.platform)) return json(res, 400, { error: "invalid platform", platform: body.platform });
    clearFakes(body.platform, FAKE_SLOTS.has(body.slot) ? body.slot : null);
    return json(res, 200, { ok: true, state: body.platform ? fakesFor(body.platform) : state });
  }

  if (req.method === "POST" && pathName === "/reset") {
    clearFakes(null);
    resetLogs();
    return json(res, 200, { ok: true });
  }

  if (req.method === "GET" && pathName === "/health") {
    const now = Date.now();
    const devtools = Object.fromEntries(Object.values(clients).map((client) => [client.platform, {
      connected: isLive(client),
      lastSeenMs: now - client.lastSeen,
      session: client.session,
      page: client.entry?.title ?? null,
      paused: client.entry?.paused ?? null,
      frontends: client.entry?.link.frontends.size ?? 0,
      takenOverAt: client.kickedAt ? new Date(client.kickedAt).toISOString() : null,
    }]));
    const runs = Object.fromEntries(Object.values(lanes).map((lane) => {
      const status = lane.runner.status();
      return [lane.platform, { id: status.id, status: status.status, sid: lane.sid, recording: Boolean(lane.rec) }];
    }));
    return json(res, 200, { ok: true, port: server.address().port, stateDir: STATE_DIR, lastAppContact, metroPort: METRO_PORT || null, devtools, runs, sids: SIDS, slots: [...FAKE_SLOTS], extensions: EXTENSIONS.map((ext) => ext.id) });
  }

  if (req.method === "GET" && pathName === "/devtools") {
    const pages = METRO_PORT ? await listPages(METRO_PORT) : [];
    const apps = Object.fromEntries(Object.values(clients).map((client) => [client.platform, {
      connected: isLive(client),
      page: client.entry?.title ?? null,
      frontendUrl: frontendUrl(METRO_PORT, `127.0.0.1:${server.address().port}/devtools/${client.platform}`),
      frontends: client.entry?.link.frontends.size ?? 0,
      paused: client.entry?.paused ?? null,
    }]));
    return json(res, 200, { metroPort: METRO_PORT || null, apps, pages: pages.map((page) => ({ title: page.title, runtime: page.description ?? null, url: page.webSocketDebuggerUrl, held: links.has(page.webSocketDebuggerUrl) })) });
  }

  return json(res, 404, { error: "not found", path: pathName });
}
server.on("upgrade", (req, socket, head) => {
  const match = new URL(req.url, "http://127.0.0.1").pathname.match(/^\/devtools\/(ios|android)$/);
  const client = match ? clients[match[1]] : null;
  if (!client?.entry?.link.open) {
    socket.end("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
    return;
  }
  const peer = acceptWebSocket(req, socket, head);
  if (!peer) return;
  client.entry.link.attachFrontend(peer);
  appendEvent(client.platform, "devtools.frontend", { frontends: client.entry.link.frontends.size, userAgent: req.headers["user-agent"] ?? null });
});

const eventClients = new Set();
const eventLog = [];

function emitEvent(name, data, source = "app") {
  const event = { at: new Date().toISOString(), t: Date.now() / 1000, source, name, data: data ?? null };
  eventLog.push(event);
  if (eventLog.length > 1000) eventLog.shift();
  const line = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of eventClients) res.write(line);
  for (const ext of EXTENSIONS) if (ext.onEvent) Promise.resolve(ext.onEvent(event)).catch(() => {});
  return event;
}

function streamEvents(req, res) {
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
  res.write(": connected\n\n");
  for (const event of eventLog.slice(-20)) res.write(`data: ${JSON.stringify(event)}\n\n`);
  eventClients.add(res);
  req.on("close", () => eventClients.delete(res));
}

const KIT = {
  config: CONFIG,
  stateDir: STATE_DIR,
  log: (line) => process.stdout.write(`${line}\n`),
  bridgeCall,
  pacedBridge,
  fillInput,
  laneFor,
  armFake,
  clearFakes,
  emit: emitEvent,
  daemon: daemonRequest,
  captureDevice,
};

EXTENSIONS = await loadExtensions(CONFIG.extensions ?? [], KIT, KIT.log);
for (const ext of EXTENSIONS) {
  for (const [route, fn] of Object.entries(ext.routes ?? {})) {
    EXTENSION_ROUTES[route.startsWith("/") ? route : `/${route}`] = async (body) => fn(body ?? {});
  }
}

function extensionHelpers(lane) {
  const merged = {};
  for (const ext of EXTENSIONS) {
    const helpers = typeof ext.helpers === "function" ? ext.helpers(lane) : ext.helpers;
    Object.assign(merged, helpers ?? {});
  }
  return merged;
}

server.listen(PORT, "127.0.0.1", () => {
  const bound = server.address().port;
  const ready = { ready: true, port: bound, pid: process.pid, metroPort: METRO_PORT || null, sids: SIDS, extensions: EXTENSIONS.map((ext) => ext.id) };
  fs.writeFileSync(path.join(STATE_DIR, "worker.json"), JSON.stringify(ready));
  process.stdout.write(`${JSON.stringify(ready)}\n`);
});
