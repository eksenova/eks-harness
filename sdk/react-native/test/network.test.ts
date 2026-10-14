import { installNetwork, networkCommand, urlMatcher } from "../src/network";

type Reply = { status: number; headers: Record<string, string>; body: string };

class FakeXhr {
  OPENED = 1;
  readyState = 0;
  _method: string | null = null;
  _url: string | null = null;
  _responseType = "";
  _requestId: number | null = null;
  _sent = false;
  status = 0;
  response = "";
  error = "";
  done = false;
  static server: (method: string, url: string) => Reply = () => ({ status: 200, headers: {}, body: "{}" });
  static sent = 0;

  open(method: string, url: string) {
    this._method = method;
    this._url = url;
    this.readyState = this.OPENED;
  }

  send(_data: unknown) {
    FakeXhr.sent++;
    this._sent = true;
    const id = 1000 + FakeXhr.sent;
    this._requestId = id;
    const reply = FakeXhr.server(this._method!, this._url!);
    setTimeout(() => {
      this.__didReceiveResponse(id, reply.status, reply.headers, this._url!);
      this.__didReceiveData(id, reply.body);
      this.__didCompleteResponse(id, "", false);
    }, 1);
  }

  __didReceiveResponse(id: number, status: number) {
    if (id === this._requestId) this.status = status;
  }

  __didReceiveData(id: number, data: string) {
    if (id === this._requestId) this.response = data;
  }

  __didReceiveIncrementalData() {}

  __didCompleteResponse(id: number, error: string) {
    if (id !== this._requestId) return;
    this.error = error;
    this.done = true;
  }
}

async function request(method: string, url: string): Promise<FakeXhr> {
  const xhr = new FakeXhr();
  xhr.open(method, url);
  xhr.send(null);
  for (let i = 0; i < 200 && !xhr.done; i++) await new Promise((resolve) => setTimeout(resolve, 2));
  return xhr;
}

beforeAll(() => {
  (globalThis as any).XMLHttpRequest = FakeXhr;
  installNetwork((url) => url.includes(":8081/"));
});

beforeEach(() => {
  networkCommand({ op: "reset" });
  FakeXhr.server = () => ({ status: 200, headers: {}, body: JSON.stringify({ value: 1, items: [{ rate: 2 }] }) });
});

test("url globs match paths, full urls and fragments", () => {
  expect(urlMatcher("/api/*")("https://x.test/api/items?a=1")).toBe(true);
  expect(urlMatcher("/api/items")("https://x.test/api/items?a=1")).toBe(true);
  expect(urlMatcher("https://x.test/**")("https://x.test/a/b")).toBe(true);
  expect(urlMatcher("/other/*")("https://x.test/api/items")).toBe(false);
});

test("routes respond, patch, abort and stop after their count", async () => {
  networkCommand({ op: "route", method: "GET", pattern: "/api/canned", action: { kind: "respond", status: 201, json: { ok: true } } });
  const canned = await request("GET", "https://x.test/api/canned");
  expect([canned.status, canned.response]).toEqual([201, '{"ok":true}']);

  networkCommand({ op: "route", method: "*", pattern: "/api/data", action: { kind: "patch", path: "items[0].rate", value: 9 } });
  const patched = await request("GET", "https://x.test/api/data");
  expect(JSON.parse(patched.response).items[0].rate).toBe(9);

  networkCommand({ op: "route", method: "POST", pattern: "/api/fail", action: { kind: "abort", reason: "failed" }, times: 1 });
  expect((await request("POST", "https://x.test/api/fail")).error).toMatch(/aborted/);
  expect((await request("POST", "https://x.test/api/fail")).error).toBe("");
  expect((await request("GET", "https://x.test/api/fail")).error).toBe("");
});

test("offline shaping fails app requests but not harness traffic", async () => {
  networkCommand({ op: "shape", profile: { latency: 0, offline: true } });
  expect((await request("GET", "https://x.test/api/data")).error).toMatch(/offline/);
  expect((await request("GET", "http://localhost:8081/status")).error).toBe("");
});

test("cassettes record then replay without the server", async () => {
  networkCommand({ op: "cassette.start", mode: "record" });
  await request("GET", "https://x.test/api/data");
  const recorded = networkCommand({ op: "cassette.stop" }) as any;
  expect(recorded.entries).toHaveLength(1);

  FakeXhr.server = () => ({ status: 500, headers: {}, body: "down" });
  const before = FakeXhr.sent;
  networkCommand({ op: "cassette.start", mode: "replay", entries: recorded.entries });
  const replayed = await request("GET", "https://x.test/api/data");
  const missed = await request("GET", "https://x.test/api/other");
  const closed = networkCommand({ op: "cassette.stop" }) as any;
  expect([replayed.status, JSON.parse(replayed.response).value]).toEqual([200, 1]);
  expect(missed.error).toMatch(/not in the cassette/);
  expect(closed.misses).toHaveLength(1);
  expect(FakeXhr.sent).toBe(before);
});

test("the fetch wrapper answers routes and cassettes without XHR", async () => {
  const calls: string[] = [];
  (globalThis as any).fetch = async (input: string) => {
    calls.push(input);
    return new Response(JSON.stringify({ gold: 1 }), { status: 200, headers: { "content-type": "application/json" } });
  };
  installNetwork(() => false);
  networkCommand({ op: "route", method: "GET", pattern: "/api/rates", action: { kind: "respond", json: { gold: 3100 } } });
  const routed = await fetch("https://x.test/api/rates");
  expect(await routed.json()).toEqual({ gold: 3100 });
  networkCommand({ op: "reset" });
  networkCommand({ op: "route", method: "GET", pattern: "/api/rates", action: { kind: "patch", path: "gold", value: 7 } });
  expect(await (await fetch("https://x.test/api/rates")).json()).toEqual({ gold: 7 });
  networkCommand({ op: "reset" });
  networkCommand({ op: "cassette.start", mode: "record" });
  await fetch("https://x.test/api/rates");
  const recorded = networkCommand({ op: "cassette.stop" }) as any;
  networkCommand({ op: "cassette.start", mode: "replay", entries: recorded.entries });
  const before = calls.length;
  expect(await (await fetch("https://x.test/api/rates")).json()).toEqual({ gold: 1 });
  await expect(fetch("https://x.test/api/other")).rejects.toThrow(/not in the cassette/);
  networkCommand({ op: "cassette.stop" });
  expect(calls.length).toBe(before);
});
