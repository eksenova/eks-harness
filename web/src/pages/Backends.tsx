import { PluginSlot } from "../shell/plugins";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorText } from "../api/client";
import { backendPath, fetchers, keys, useBackend, useBackendDefinitions, useBackends } from "../api/queries";
import type { BackendDefinitionOut, BackendInfoOut, BackendLeaseOut, BackendOut } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Checkbox, SearchInput, Select } from "../components/Form";
import { OverflowMenu } from "../components/Menu";
import { AbsTime, CopyLink, Forbidden, Mono, PageHeader, queryState, Tabs } from "../components/Misc";
import { EmptyState, ErrorState, Notice } from "../components/Notice";
import { DataTable, sortPatch, sortRows, useSort, type Column, type Natural } from "../components/Table";
import { useAuth } from "../lib/auth";
import { formatClock, formatFuture, plural, toDate } from "../lib/format";
import { useMinute, useTitle } from "../lib/hooks";
import { sessionUrl, useSearchParams, useSetParams, useSyncedText } from "../lib/url";
import { saveBlob } from "../lib/download";
import { TextContent } from "../viewers/TextContent";

const STATUS: Record<string, string> = { stopped: "Stopped", preparing: "Starting", starting: "Starting", running: "Running", stopping: "Stopping", failed: "Failed" };
const HOLD_SECONDS = 3600;

export function backendLeases(backend: BackendOut): BackendLeaseOut[] {
  return Array.isArray(backend.leases) ? backend.leases : [];
}

function epochIso(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value) ? new Date(value * 1000).toISOString() : null;
}

export function describeSeconds(value: number | "never" | null | undefined): string {
  if (value === "never") return "never";
  if (value === null || value === undefined || !Number.isFinite(value)) return "";
  if (value < 120) return `${Math.round(value)} s`;
  if (value < 7200) return `${Math.round(value / 60)} min`;
  return `${Math.round((value / 3600) * 10) / 10} h`;
}

export function BackendState({ backend }: { backend: BackendOut }) {
  const now = useMinute();
  const stopsAt = typeof backend.stopsAt === "number" ? new Date(backend.stopsAt * 1000) : null;
  const leases = backendLeases(backend);
  if ((backend.status === "running" || backend.status === "failed") && !leases.length) {
    if (stopsAt && stopsAt.getTime() > now.getTime()) {
      return <span className="state-word" data-state={backend.status === "failed" ? "failed" : "idle"}>{`${backend.status === "failed" ? "Failed, cleanup" : "Idle, stops"} ${formatFuture(stopsAt, now).replace(/^In/, "in")}`}</span>;
    }
    if (backend.idleTimeout === "never") return <span className="state-word" data-state="idle">Idle, kept</span>;
  }
  const word = STATUS[backend.status] ?? backend.status;
  return (
    <span className="state-word" data-state={backend.status === "failed" ? "failed" : backend.status === "running" ? "active" : backend.status} title={backend.error ?? undefined}>
      {word}
    </span>
  );
}

export function Ports({ ports }: { ports: Record<string, number> }) {
  const entries = Object.entries(ports);
  if (!entries.length) return null;
  return (
    <span className="cluster">
      {entries.map(([name, port]) => (
        <span key={name}>
          {name} <Mono>{port}</Mono>
        </span>
      ))}
    </span>
  );
}

const NATURAL: Natural = { id: "asc", definition: "asc", state: "asc", since: "desc", leases: "desc", generation: "desc" };

export function backendHref(id: string): string {
  return `/backends/${backendPath(id)}`;
}

export function BackendsPage({ tab }: { tab: "instances" | "definitions" }) {
  useTitle("Backends");
  const backends = useBackends();
  const definitions = useBackendDefinitions();
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const [stopping, setStopping] = useState<BackendOut | null>(null);
  const { sort, dir } = useSort(params, "id", "asc", NATURAL);
  const rows = useMemo(() => {
    const accessors: Record<string, (b: BackendOut) => unknown> = {
      id: (b) => b.id,
      definition: (b) => b.definition,
      state: (b) => b.status,
      since: (b) => (b.startedAt ? Date.parse(b.startedAt) : 0),
      leases: (b) => backendLeases(b).length,
      generation: (b) => b.generation ?? 0,
    };
    return sortRows(backends.data?.items ?? [], accessors[sort] ?? accessors.id, dir);
  }, [backends.data, sort, dir]);

  const columns: Column<BackendOut>[] = [
    { key: "id", header: "Backend", sortKey: "id", naturalDir: "asc", render: (b) => <Link to={backendHref(b.id)} className="link-quiet mono">{b.id}</Link>, title: (b) => b.id },
    { key: "definition", header: "Definition", sortKey: "definition", naturalDir: "asc", render: (b) => b.definition },
    { key: "state", header: "State", sortKey: "state", naturalDir: "asc", render: (b) => <BackendState backend={b} /> },
    { key: "generation", header: "Gen", sortKey: "generation", naturalDir: "desc", align: "right", width: "56px", render: (b) => <Mono>{b.generation ?? 0}</Mono> },
    { key: "since", header: "Since", sortKey: "since", naturalDir: "desc", align: "right", width: "96px", render: (b) => <AbsTime value={b.status === "stopped" ? b.stoppedAt : b.startedAt} /> },
    { key: "ports", header: "Ports", render: (b) => <Ports ports={b.ports} /> },
    { key: "leases", header: "Leases", sortKey: "leases", naturalDir: "desc", align: "right", width: "72px", render: (b) => String(backendLeases(b).length) },
    {
      key: "more",
      header: <span className="visually-hidden">Actions</span>,
      width: "var(--h-control)",
      render: (b) => (
        <OverflowMenu
          label={`More actions for ${b.id}`}
          items={[
            { label: "Open", onSelect: () => void navigate({ to: backendHref(b.id) }) },
            { label: "Logs", onSelect: () => void navigate({ to: `${backendHref(b.id)}/logs` }) },
            ...(b.status !== "stopped" ? [{ label: "Stop…", danger: true, onSelect: () => setStopping(b) }] : []),
          ]}
        />
      ),
    },
  ];

  const defColumns: Column<BackendDefinitionOut>[] = [
    { key: "name", header: "Name", render: (d) => d.name },
    { key: "description", header: "Description", render: (d) => d.description ?? "" },
    { key: "path", header: "Found in", render: (d) => <span className="mono muted">{d.path}</span>, title: (d) => d.path },
    { key: "idle", header: "Idle timeout", align: "right", width: "104px", render: (d) => (d.idleTimeout === null || d.idleTimeout === undefined ? "" : String(d.idleTimeout)) },
  ];

  const state = tab === "instances" ? queryState(backends, "backends") : queryState(definitions, "backend definitions");

  return (
    <div className="page">
      <PageHeader title="Backends" />
      <Tabs
        label="Backend sections"
        items={[
          { label: "Instances", to: "/backends", count: backends.data?.items.length ?? null, active: tab === "instances" },
          { label: "Definitions", to: "/backends/definitions", count: definitions.data?.items.length ?? null, active: tab === "definitions" },
        ]}
      />
      {state ??
        (tab === "instances" ? (
          <DataTable
            rows={rows}
            columns={columns}
            getId={(b) => b.id}
            getHref={(b) => backendHref(b.id)}
            sort={sort}
            dir={dir}
            onSort={(key, d) => setParams(sortPatch(key, d, "id", "asc", NATURAL))}
            label="Backend instances"
            primary
            mobileRow={(b) => (
              <>
                <div className="row-title">
                  <Link to={backendHref(b.id)} className="mono">
                    {b.id}
                  </Link>
                  <BackendState backend={b} />
                </div>
                <div className="row-meta">
                  <Ports ports={b.ports} />
                  <span>{plural(backendLeases(b).length, "lease")}</span>
                  <span>gen {b.generation ?? 0}</span>
                </div>
              </>
            )}
            empty={<EmptyState>No backends running. Agents start them with eks-harness backend acquire.</EmptyState>}
          />
        ) : (
          <DataTable
            rows={definitions.data!.items}
            columns={defColumns}
            getId={(d) => `${d.source}:${d.path}:${d.name}`}
            label="Backend definitions"
            empty={<EmptyState>No backend definitions found. Add a backend plugin, or put definition scripts in a project's .harness/backends/ folder or the config's backends folder.</EmptyState>}
          />
        ))}
      {stopping ? <StopBackendConfirm backend={stopping} onClose={() => setStopping(null)} /> : null}
    </div>
  );
}

function StopBackendConfirm({ backend, onClose }: { backend: BackendOut; onClose: () => void }) {
  const client = useQueryClient();
  const [cleanup, setCleanup] = useState(false);
  const leases = backendLeases(backend);
  const holders = Array.from(new Set(leases.map((l) => (l.driverSid ? `driver ${l.driverSid}` : l.holder)).filter(Boolean)));
  const body = leases.length ? `${plural(leases.length, "lease")} end with it: ${holders.join(", ")}.` : "No lease holds it.";
  return (
    <ConfirmDialog
      title={`Stop ${backend.id}?`}
      body={body}
      extra={<Checkbox label="Also run the project's cleanup (drops its data)" checked={cleanup} onChange={(event) => setCleanup(event.target.checked)} />}
      confirmLabel={cleanup ? "Stop and clean up" : "Stop backend"}
      busyLabel={"Stopping…"}
      onClose={onClose}
      errorFor={(err) => errorText(err, "stop", backend.id)}
      onConfirm={async () => {
        await api.post(`/api/backends/${backendPath(backend.id)}/stop`, { reason: "stopped from the UI", cleanup }, { timeoutMs: 120_000 });
        void client.invalidateQueries({ queryKey: keys.backends });
        void client.invalidateQueries({ queryKey: ["backend"] });
      }}
    />
  );
}

function LeaseExpiry({ lease }: { lease: BackendLeaseOut }) {
  const now = useMinute();
  if (lease.driverSid) return <span className="muted">with the driver</span>;
  if (typeof lease.expires !== "number") return <span className="muted">never</span>;
  return <span>{formatFuture(new Date(lease.expires * 1000), now).replace(/^In/, "in")}</span>;
}

function idleLine(b: BackendOut): string {
  const timeout = describeSeconds(b.idleTimeout);
  if (!timeout) return "";
  if (b.idleTimeout === "never") return " Kept when no lease is left.";
  return ` Stops and cleans up ${timeout} after its last lease${b.idleTimeoutSource ? ` (${b.idleTimeoutSource} setting)` : ""}.`;
}

export function BackendDetailPage({ splat }: { splat: string }) {
  const logs = splat.endsWith("/logs");
  const id = decodeURIComponent(logs ? splat.slice(0, -"/logs".length) : splat);
  const backend = useBackend(id);
  const auth = useAuth();
  const client = useQueryClient();
  const [stopping, setStopping] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  useTitle(`${id} - Backends`);
  const state = queryState(backend, "backend", {
    notFound: <EmptyState action={<Link to="/backends" className="link">Backends</Link>}>There is no backend {id}. It may have stopped and been cleaned up.</EmptyState>,
    forbidden: <Forbidden what={id} />,
  });
  if (state) return <div className="page">{state}</div>;
  const b = backend.data!;
  const leases = backendLeases(b);
  const holdId = `ui:${auth.username}`;
  const myHold = leases.find((l) => l.kind === "hold" && l.holder === holdId);
  const refresh = () => {
    void client.invalidateQueries({ queryKey: keys.backend(b.id) });
    void client.invalidateQueries({ queryKey: keys.backends });
  };
  const toggleHold = async () => {
    setBusy("hold");
    setError("");
    try {
      await api.post(`/api/backends/${backendPath(b.id)}/hold`, myHold ? { seconds: 0, holdId } : { seconds: HOLD_SECONDS, holdId });
      refresh();
    } catch (err) {
      setError(errorText(err, myHold ? "release the hold on" : "hold", b.id));
    } finally {
      setBusy(null);
    }
  };
  const release = async (lease: BackendLeaseOut) => {
    setBusy(lease.id);
    setError("");
    try {
      await api.post(`/api/backend-leases/${encodeURIComponent(lease.id)}/release`, { reason: "released from the UI" });
      refresh();
    } catch (err) {
      setError(errorText(err, "release", `the lease ${lease.id}`));
    } finally {
      setBusy(null);
    }
  };
  const base = backendHref(b.id);
  const started = toDate(b.startedAt);
  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: "Backends", to: "/backends" }]}
        title={<span className="mono">{b.id}</span>}
        actions={
          b.status !== "stopped" ? (
            <>
              <Button onClick={() => void toggleHold()} busy={busy === "hold"} busyLabel={myHold ? "Releasing…" : "Holding…"}>
                {myHold ? "Release my hold" : "Hold 1 h"}
              </Button>
              <Button onClick={() => setStopping(true)}>{"Stop…"}</Button>
            </>
          ) : null
        }
        meta={
          <p>
            <BackendState backend={b} />
            {started ? ` since ${formatClock(started)}` : ""}, generation <Mono>{b.generation ?? 0}</Mono>, {plural(leases.length, "lease")}. Identity <Mono>{b.instance}</Mono>.
            {b.tree ? <> Tree <Mono>{b.tree}</Mono>.</> : null}
            {idleLine(b)}
            {b.error ? <span className="danger"> {b.error}</span> : null}
          </p>
        }
      />
      {error ? <Notice variant="error">{error}</Notice> : null}
      <Tabs
        label="Backend sections"
        items={[
          { label: "Overview", to: base, active: !logs },
          { label: "Logs", to: `${base}/logs`, active: logs },
        ]}
      />
      {logs ? (
        <LogsView resourceBase={`backend:${b.id}`} processes={Object.keys(b.logs ?? {})} filename={`${b.id.replace(/[^\w.-]+/g, "-")}.log`} />
      ) : (
        <>
          <section className="section">
            <h2 className="section-title">Leases</h2>
            <DataTable
              rows={leases}
              columns={[
                { key: "holder", header: "Holder", render: (l) => <span className="mono" title={l.owner ?? undefined}>{l.holder}</span> },
                { key: "kind", header: "Kind", width: "72px", render: (l) => l.kind },
                {
                  key: "session",
                  header: "Project / session",
                  render: (l) =>
                    l.project && l.session ? (
                      <Link className="link-quiet" to={sessionUrl(l.project, l.session)}>
                        {l.project} / {l.session}
                      </Link>
                    ) : (
                      <span className="muted">{l.project ?? ""}</span>
                    ),
                },
                { key: "driver", header: "Driver", width: "96px", render: (l) => (l.driverSid ? <Link className="link-quiet mono" to={`/sid/${l.driverSid}`}>{l.driverSid}</Link> : null) },
                { key: "since", header: "Since", align: "right", width: "96px", render: (l) => <AbsTime value={epochIso(l.created)} /> },
                { key: "expires", header: "Expires", align: "right", width: "128px", render: (l) => <LeaseExpiry lease={l} /> },
                {
                  key: "release",
                  header: <span className="visually-hidden">Release</span>,
                  width: "88px",
                  render: (l) =>
                    l.id && !l.driverSid ? (
                      <button type="button" className="link" disabled={busy === l.id} onClick={() => void release(l)}>
                        {busy === l.id ? "Releasing…" : "Release"}
                      </button>
                    ) : null,
                },
              ]}
              getId={(l) => l.id || `${l.holder}:${l.created}`}
              label="Leases"
              empty={<p className="muted">No lease holds it.{idleLine(b)}</p>}
            />
          </section>
          <section className="section">
            <h2 className="section-title">Info</h2>
            <InfoView info={b.info} />
          </section>
          <div className="grid-2">
            <section className="section">
              <h2 className="section-title">Processes</h2>
              <DataTable
                rows={b.processes.map((name) => ({ name, alive: b.alive?.[name], pid: (b.pids as Record<string, number> | undefined)?.[name], port: b.ports[name] }))}
                columns={[
                  { key: "name", header: "Name", render: (p) => p.name },
                  { key: "state", header: "State", render: (p) => (p.alive === undefined ? "" : p.alive ? "Running" : <span className="strong">Stopped</span>) },
                  { key: "pid", header: "PID", render: (p) => (p.pid ? <Mono>{p.pid}</Mono> : null) },
                  { key: "port", header: "Port", render: (p) => (p.port ? <Mono>{p.port}</Mono> : null) },
                ]}
                getId={(p) => p.name}
                label="Processes"
                empty={<p className="muted">No supervised processes; the project's start hook runs it.</p>}
              />
            </section>
            <section className="section">
              <h2 className="section-title">Exports</h2>
              <KeyValues values={b.exports} empty="Nothing exported." />
            </section>
            <section className="section">
              <h2 className="section-title">Captured values</h2>
              <KeyValues values={b.captured} empty="Nothing captured." />
            </section>
          </div>
        </>
      )}
      <PluginSlot slot="backend.inspector" props={{ backend: b as unknown as Record<string, unknown> }} />
      {stopping ? <StopBackendConfirm backend={b} onClose={() => setStopping(false)} /> : null}
    </div>
  );
}

function flatten(prefix: string, value: unknown, out: Record<string, string>) {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, inner] of Object.entries(value as Record<string, unknown>)) flatten(prefix ? `${prefix}.${key}` : key, inner, out);
    return;
  }
  out[prefix] = typeof value === "string" ? value : JSON.stringify(value);
}

function InfoView({ info }: { info: BackendInfoOut | null }) {
  if (!info) return <p className="muted">No info yet; it is filled in once the backend runs.</p>;
  const facts: Record<string, string> = {};
  if (info.url) facts.url = info.url;
  for (const [name, url] of Object.entries(info.urls ?? {})) facts[`url ${name}`] = url;
  for (const key of ["commit", "branch", "version", "worktree"] as const) if (info[key]) facts[key] = info[key];
  for (const [name, port] of Object.entries(info.ports ?? {})) facts[`port ${name}`] = String(port);
  const credentials: Record<string, string> = {};
  flatten("", info.credentials ?? {}, credentials);
  const extra: Record<string, string> = {};
  flatten("", info.extra ?? {}, extra);
  return (
    <>
      <KeyValues values={facts} empty="No URLs or versions." />
      {Object.keys(credentials).length ? (
        <>
          <h3 className="section-subtitle">Credentials</h3>
          <KeyValues values={credentials} empty="" />
          {info.redacted ? <p className="muted">Redacted. A lease holder reads them with eks-harness backend info &lt;lease&gt; --secrets.</p> : null}
        </>
      ) : null}
      {Object.keys(info.env ?? {}).length ? (
        <>
          <h3 className="section-subtitle">Environment</h3>
          <KeyValues values={info.env} empty="" />
        </>
      ) : null}
      {Object.keys(extra).length ? (
        <>
          <h3 className="section-subtitle">Extra</h3>
          <KeyValues values={extra} empty="" />
        </>
      ) : null}
    </>
  );
}

function KeyValues({ values, empty }: { values: Record<string, string>; empty: string }) {
  const rows = Object.entries(values ?? {});
  if (!rows.length) return empty ? <p className="muted">{empty}</p> : null;
  return (
    <table className="kv-table">
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Value</th>
          <th scope="col">
            <span className="visually-hidden">Copy</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([name, value]) => (
          <tr key={name}>
            <td>{name}</td>
            <td className="mono wrap-anywhere">{value}</td>
            <td>{value === "<redacted>" ? null : <CopyLink value={value} />}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function LogsView({ resourceBase, processes, filename }: { resourceBase: string; processes: string[]; filename: string }) {
  const params = useSearchParams();
  const setParams = useSetParams();
  const [grep, setGrep] = useSyncedText("grep", 300);
  const lines = ["200", "1000", "5000"].includes(params.lines ?? "") ? params.lines! : "1000";
  const follow = params.follow !== "0";
  const process = params.process && processes.includes(params.process) ? params.process : "";
  const resource = process ? `${resourceBase}:${process}` : resourceBase;
  const [text, setText] = useState("");
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [paused, setPaused] = useState(false);
  const [recent, setRecent] = useState<number[]>([]);
  const offset = useRef(0);
  const [find, setFind] = useState(params.find ?? "");
  const [wrap, setWrap] = useState(params.wrap === "1");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const response = await fetchers.logs({ resource, lines: Number(lines), grep: params.grep || undefined });
      offset.current = response.offset;
      setText(response.text);
    } catch (err) {
      setLoadError(err);
    } finally {
      setLoading(false);
    }
  }, [resource, lines, params.grep]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!follow || loadError) return;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const response = await fetchers.logs({ resource, offset: offset.current, grep: params.grep || undefined });
        if (response.offset < offset.current) {
          offset.current = response.offset;
          return;
        }
        offset.current = response.offset;
        if (response.text) {
          const added = response.text.split("\n").filter(Boolean);
          const needle = (params.grep ?? "").toLowerCase();
          const kept = needle ? added.filter((line) => line.toLowerCase().includes(needle)) : added;
          if (kept.length) {
            setText((prev) => {
              const joined = (prev && !prev.endsWith("\n") ? prev + "\n" : prev) + kept.join("\n") + "\n";
              const all = joined.split("\n");
              const max = Math.max(Number(lines), 200) * 4;
              return all.length > max ? all.slice(all.length - max).join("\n") : joined;
            });
            const now = Date.now();
            setRecent((prev) => [...prev.filter((t) => now - t < 60_000), ...kept.map(() => now)]);
          }
        }
      } catch {
        return;
      }
    };
    const timer = window.setInterval(() => void tick(), 1000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [follow, resource, params.grep, lines, loadError]);

  const recentCount = recent.filter((t) => Date.now() - t < 60_000).length;

  return (
    <div className="logs-view">
      <div className="toolbar page-tools">
        <SearchInput value={grep} onValueChange={setGrep} label="Filter lines (grep)" placeholder="Filter lines (grep)" className="page-search" />
        <Select value={lines} onChange={(event) => setParams({ lines: event.target.value === "1000" ? null : event.target.value })} aria-label="Lines">
          <option value="200">Last 200 lines</option>
          <option value="1000">Last 1,000 lines</option>
          <option value="5000">Last 5,000 lines</option>
        </Select>
        <Checkbox label="Follow" checked={follow} onChange={(event) => setParams({ follow: event.target.checked ? null : "0" }, { replace: true })} />
        {processes.length ? (
          <Select value={process} onChange={(event) => setParams({ process: event.target.value || null })} aria-label="Process">
            <option value="">Process: all</option>
            {processes.map((p) => (
              <option key={p} value={p}>
                Process: {p}
              </option>
            ))}
          </Select>
        ) : null}
        <button type="button" className="link" onClick={() => saveBlob(new Blob([text], { type: "text/plain" }), filename)}>
          Download
        </button>
      </div>
      {loadError ? (
        <ErrorState message={errorText(loadError, "load", "the log")} onRetry={() => void load()} />
      ) : loading && !text ? (
        <p className="state muted">{"Loading log…"}</p>
      ) : (
        <TextContent
          text={text}
          find={find}
          onFind={(value) => {
            setFind(value);
            setParams({ find: value || null }, { replace: true });
          }}
          wrap={wrap}
          onWrap={(value) => {
            setWrap(value);
            setParams({ wrap: value ? "1" : null }, { replace: true });
          }}
          follow={follow && !paused}
          onScrolledAway={setPaused}
          label="Log"
          height="calc(100vh - var(--h-topbar) * 6)"
        />
      )}
      {follow ? (
        paused ? (
          <p className="viewer-note">
            Paused following.{" "}
            <button type="button" className="link" onClick={() => setPaused(false)}>
              Jump to latest
            </button>
          </p>
        ) : (
          <p className="viewer-note">Following. {plural(recentCount, "new line")} in the last minute.</p>
        )
      ) : null}
    </div>
  );
}
