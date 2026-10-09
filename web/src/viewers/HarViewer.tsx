import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { errorText } from "../api/client";
import { Button, IconButton } from "../components/Button";
import { Select, SearchInput } from "../components/Form";
import { CopyLink } from "../components/Misc";
import { ErrorState, Loading } from "../components/Notice";
import { DataTable, type Column } from "../components/Table";
import { formatMs, formatSize } from "../lib/format";
import { useDebounced, useIsDesktop } from "../lib/hooks";
import { JsonTree } from "./JsonTree";
import { TextContent } from "./TextContent";
import type { ViewerProps } from "./types";

interface HarHeader {
  name: string;
  value: string;
}

interface HarEntry {
  startedDateTime?: string;
  time?: number;
  _resourceType?: string;
  _error?: string;
  request: { method: string; url: string; headers?: HarHeader[]; queryString?: HarHeader[]; postData?: { mimeType?: string; text?: string } };
  response: { status: number; statusText?: string; headers?: HarHeader[]; content?: { size?: number; mimeType?: string; text?: string; encoding?: string }; bodySize?: number; _transferSize?: number; _error?: string };
  timings?: Record<string, number>;
}

interface Row {
  index: number;
  entry: HarEntry;
  method: string;
  status: number;
  failed: boolean;
  url: URL | null;
  rawUrl: string;
  type: string;
  size: number;
  time: number;
  start: number;
}

const TYPES: [string, string][] = [
  ["document", "Document"],
  ["xhr", "XHR and fetch"],
  ["script", "Script"],
  ["stylesheet", "Stylesheet"],
  ["image", "Image"],
  ["font", "Font"],
  ["media", "Media"],
  ["websocket", "WebSocket"],
  ["other", "Other"],
];

function typeOf(entry: HarEntry): string {
  const raw = (entry._resourceType ?? "").toLowerCase();
  if (raw === "fetch" || raw === "xhr") return "xhr";
  if (["document", "script", "stylesheet", "image", "font", "media", "websocket"].includes(raw)) return raw;
  const mime = (entry.response.content?.mimeType ?? "").toLowerCase();
  if (mime.includes("html")) return "document";
  if (mime.includes("javascript")) return "script";
  if (mime.includes("css")) return "stylesheet";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("font/") || mime.includes("woff")) return "font";
  if (mime.startsWith("video/") || mime.startsWith("audio/")) return "media";
  if (mime.includes("json")) return "xhr";
  return "other";
}

function typeLabel(type: string): string {
  if (type === "xhr") return "XHR";
  return TYPES.find(([key]) => key === type)?.[1] ?? "Other";
}

const MB = 1000 * 1000;

function Body({ text, mime, encoding, missing }: { text: string | undefined; mime: string; encoding?: string; missing: string }) {
  const [force, setForce] = useState(false);
  const [find, setFind] = useState("");
  const [wrap, setWrap] = useState(true);
  if (text === undefined || text === null || text === "") return <p className="muted">{missing}</p>;
  const size = encoding === "base64" ? Math.floor((text.length * 3) / 4) : text.length;
  if (size > MB && !force) {
    return (
      <p>
        Body is {formatSize(size)}.{" "}
        <Button onClick={() => setForce(true)}>Show anyway</Button>
      </p>
    );
  }
  if (mime.startsWith("image/") && encoding === "base64") {
    return (
      <div className="har-image">
        <img src={`data:${mime};base64,${text}`} alt="Response body" />
      </div>
    );
  }
  let decoded = text;
  if (encoding === "base64") {
    try {
      decoded = new TextDecoder().decode(Uint8Array.from(atob(text), (c) => c.charCodeAt(0)));
    } catch {
      decoded = text;
    }
  }
  if (mime.includes("json")) {
    let parsed: unknown = undefined;
    try {
      parsed = JSON.parse(decoded);
    } catch {
      parsed = undefined;
    }
    if (parsed !== undefined) return <JsonTree data={parsed} find={find} onFind={setFind} />;
  }
  return <TextContent text={decoded} find={find} onFind={setFind} wrap={wrap} onWrap={setWrap} compact height="calc(var(--s-7) * 8)" label="Body" />;
}

function HeaderTable({ title, headers }: { title: string; headers: HarHeader[] | undefined }) {
  return (
    <section className="har-section">
      <h4 className="section-title">{title}</h4>
      {headers && headers.length ? (
        <table className="kv-table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Value</th>
            </tr>
          </thead>
          <tbody>
            {headers.map((header, index) => (
              <tr key={`${header.name}-${index}`}>
                <td className="mono">{header.name}</td>
                <td className="mono wrap-anywhere">{header.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">None.</p>
      )}
    </section>
  );
}

function Detail({ row, tab, onTab, onClose }: { row: Row; tab: string; onTab: (tab: string) => void; onClose: () => void }) {
  const { entry } = row;
  const timings = entry.timings ?? {};
  const phases: [string, number | undefined][] = [
    ["Blocked", timings.blocked],
    ["DNS", timings.dns],
    ["Connect", timings.connect],
    ["TLS", timings.ssl],
    ["Send", timings.send],
    ["Wait", timings.wait],
    ["Receive", timings.receive],
    ["Total", entry.time],
  ];
  const tabs: [string, string][] = [
    ["headers", "Headers"],
    ["request", "Request"],
    ["response", "Response"],
    ["timing", "Timing"],
  ];
  return (
    <aside className="har-detail" aria-label="Request detail">
      <div className="har-detail-head">
        <span className="strong">
          {row.method} {row.failed ? "Failed" : row.status}
        </span>
        <IconButton icon="close" label="Close request detail" shortcut="Escape" onClick={onClose} />
      </div>
      <p className="mono wrap-anywhere">
        {row.rawUrl} <CopyLink value={row.rawUrl} />
      </p>
      <div className="tabs tabs-local" role="tablist" aria-label="Request detail">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" role="tab" className="tab" aria-selected={tab === key} aria-current={tab === key ? "page" : undefined} onClick={() => onTab(key)}>
            {label}
          </button>
        ))}
      </div>
      <div className="har-detail-body">
        {tab === "headers" ? (
          <>
            <HeaderTable title="Request headers" headers={entry.request.headers} />
            <HeaderTable title="Response headers" headers={entry.response.headers} />
          </>
        ) : null}
        {tab === "request" ? (
          <>
            {entry.request.queryString && entry.request.queryString.length ? <HeaderTable title="Query parameters" headers={entry.request.queryString} /> : null}
            <section className="har-section">
              <h4 className="section-title">Body</h4>
              <Body text={entry.request.postData?.text} mime={(entry.request.postData?.mimeType ?? "").toLowerCase()} missing="No request body." />
            </section>
          </>
        ) : null}
        {tab === "response" ? (
          <section className="har-section">
            <Body text={entry.response.content?.text} mime={(entry.response.content?.mimeType ?? "").toLowerCase()} encoding={entry.response.content?.encoding} missing="The HAR does not include this body." />
          </section>
        ) : null}
        {tab === "timing" ? (
          <table className="kv-table">
            <tbody>
              {phases.map(([label, value]) => (
                <tr key={label}>
                  <td>{label}</td>
                  <td className="align-right">{value === undefined || value < 0 ? "" : formatMs(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </aside>
  );
}

export function HarViewer({ rawUrl, params, setParams }: ViewerProps) {
  const query = useQuery({
    queryKey: ["raw-har", rawUrl],
    queryFn: async ({ signal }) => {
      const response = await fetch(rawUrl, { credentials: "same-origin", signal });
      if (!response.ok) throw new Error(`the daemon returned ${response.status}`);
      const data = (await response.json()) as { log?: { entries?: HarEntry[] } };
      return data.log?.entries ?? [];
    },
    staleTime: Infinity,
  });
  const desktop = useIsDesktop();
  const [text, setText] = useState(params.hq ?? "");
  const debounced = useDebounced(text, 200);
  const status = params.hstatus ?? "";
  const type = params.htype ?? "";
  const selected = params.req ? Number(params.req) : null;
  const tab = params.htab ?? "headers";

  const onText = useCallback(
    (value: string) => {
      setText(value);
    },
    [],
  );
  useEffect(() => {
    if ((params.hq ?? "") !== debounced) setParams({ hq: debounced || null }, { replace: true });
  }, [debounced]);

  const rows = useMemo<Row[]>(() => {
    const entries = query.data ?? [];
    const first = entries.reduce((min, e) => {
      const t = e.startedDateTime ? Date.parse(e.startedDateTime) : NaN;
      return Number.isFinite(t) && t < min ? t : min;
    }, Number.POSITIVE_INFINITY);
    return entries.map((entry, i) => {
      let url: URL | null = null;
      try {
        url = new URL(entry.request.url);
      } catch {
        url = null;
      }
      const started = entry.startedDateTime ? Date.parse(entry.startedDateTime) : NaN;
      const statusCode = entry.response?.status ?? 0;
      return {
        index: i + 1,
        entry,
        method: entry.request.method,
        status: statusCode,
        failed: !statusCode || Boolean(entry._error || entry.response?._error),
        url,
        rawUrl: entry.request.url,
        type: typeOf(entry),
        size: entry.response?._transferSize ?? (entry.response?.bodySize && entry.response.bodySize > 0 ? entry.response.bodySize : entry.response?.content?.size ?? 0),
        time: entry.time ?? 0,
        start: Number.isFinite(started) && Number.isFinite(first) ? started - first : 0,
      };
    });
  }, [query.data]);

  const filtered = useMemo(() => {
    const needle = debounced.toLowerCase();
    return rows.filter((row) => {
      if (needle && !row.rawUrl.toLowerCase().includes(needle)) return false;
      if (type && row.type !== type) return false;
      if (status) {
        if (status === "failed") return row.failed;
        if (row.failed) return false;
        return String(row.status).startsWith(status[0]);
      }
      return true;
    });
  }, [rows, debounced, type, status]);

  if (query.isPending) return <Loading what="requests" />;
  if (query.isError) return <ErrorState message={errorText(query.error, "load", "the HAR file")} onRetry={() => void query.refetch()} />;

  const current = selected !== null ? rows.find((r) => r.index === selected) ?? null : null;
  const select = (row: Row | null) => setParams({ req: row ? String(row.index) : null }, { replace: true });

  const columns: Column<Row>[] = [
    { key: "index", header: "#", align: "right", width: "56px", render: (row) => row.index },
    { key: "method", header: "Method", width: "80px", render: (row) => row.method },
    {
      key: "status",
      header: "Status",
      align: "right",
      width: "72px",
      render: (row) => (row.failed ? <span className="strong danger">Failed</span> : row.status >= 500 ? <span className="strong danger">{row.status}</span> : row.status >= 400 ? <span className="strong">{row.status}</span> : row.status),
    },
    {
      key: "url",
      header: "URL",
      title: (row) => row.rawUrl,
      render: (row) =>
        row.url ? (
          <span className="har-url">
            <span>{row.url.pathname + row.url.search}</span>
            <span className="muted">{row.url.host}</span>
          </span>
        ) : (
          row.rawUrl
        ),
    },
    { key: "type", header: "Type", width: "96px", render: (row) => typeLabel(row.type) },
    { key: "size", header: "Size", align: "right", width: "80px", render: (row) => (row.size > 0 ? formatSize(row.size) : "") },
    { key: "time", header: "Time", align: "right", width: "80px", render: (row) => formatMs(row.time) },
    { key: "start", header: "Start", align: "right", width: "80px", render: (row) => `+${formatMs(row.start) || "0 ms"}` },
  ];

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <SearchInput value={text} onValueChange={onText} label="Filter URL" placeholder="Filter URL" className="find-input" />
        <Select value={status} onChange={(event) => setParams({ hstatus: event.target.value || null })} aria-label="Status">
          <option value="">All statuses</option>
          <option value="2xx">2xx</option>
          <option value="3xx">3xx</option>
          <option value="4xx">4xx</option>
          <option value="5xx">5xx</option>
          <option value="failed">Failed</option>
        </Select>
        <Select value={type} onChange={(event) => setParams({ htype: event.target.value || null })} aria-label="Type">
          <option value="">All types</option>
          {TYPES.map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </Select>
        <span className="muted">
          {filtered.length.toLocaleString("en-US")} of {rows.length.toLocaleString("en-US")} requests
        </span>
      </div>
      <div className="har-frame">
      <div className={`har-split ${current && desktop ? "has-detail" : ""}`}>
        <div className="har-table">
          <DataTable
            rows={filtered}
            columns={columns}
            getId={(row) => String(row.index)}
            label="Requests"
           
            isSelectedRow={(row) => row.index === selected}
            onOpen={(row) => select(row)}
            onRowKey={(row, key) => {
              if (key === "Escape" && current) {
                select(null);
                return true;
              }
              if ((key === "ArrowDown" || key === "ArrowUp") && current) {
                const position = filtered.findIndex((r) => r.index === row.index);
                const next = filtered[position + (key === "ArrowDown" ? 1 : -1)];
                if (next) select(next);
                return false;
              }
              return false;
            }}
            empty={
              <div className="state">
                <p className="state-sentence">No requests match these filters.</p>
                <div className="button-row">
                  <Button
                    onClick={() => {
                      setText("");
                      setParams({ hq: null, hstatus: null, htype: null }, { replace: true });
                    }}
                  >
                    Clear filters
                  </Button>
                </div>
              </div>
            }
          />
        </div>
        {current ? <Detail row={current} tab={tab} onTab={(value) => setParams({ htab: value === "headers" ? null : value }, { replace: true })} onClose={() => select(null)} /> : null}
      </div>
      </div>
    </div>
  );
}
