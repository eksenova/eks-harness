import { PluginSlot } from "../shell/plugins";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorText } from "../api/client";
import { backendPath, fetchers, keys, useBackend, useBackendDefinitions, useBackends, useLeases } from "../api/queries";
import type { BackendDefinitionOut, BackendOut } from "../api/types";
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

function holds(backend: BackendOut): { id: string; until: number }[] {
  const raw = backend.holds;
  if (!raw || typeof raw !== "object") return [];
  const now = Date.now() / 1000;
  return Object.entries(raw as Record<string, unknown>)
    .map(([id, value]) => ({ id, until: typeof value === "number" ? value : Number(value) }))
    .filter((hold) => Number.isFinite(hold.until) && hold.until > now);
}

export function leaseBindings(backend: BackendOut): string[] {
  return backend.bindings.filter((binding) => !binding.startsWith("hold "));
}

export function BackendState({ backend }: { backend: BackendOut }) {
  const now = useMinute();
  const emptySince = typeof backend.emptySince === "number" ? backend.emptySince : null;
  const grace = backend.idleGraceSeconds ?? null;
  const stopAt = emptySince !== null && grace !== null ? new Date((emptySince + grace) * 1000) : null;
  if (backend.status === "running" && stopAt && stopAt.getTime() > now.getTime() && !backend.bindings.length) {
    return <span className="state-word">Stops {formatFuture(stopAt, now).replace(/^In/, "in")}</span>;
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

const NATURAL: Natural = { id: "asc", definition: "asc", state: "asc", since: "desc", bindings: "desc" };

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
      bindings: (b) => leaseBindings(b).length,
    };
    return sortRows(backends.data?.items ?? [], accessors[sort] ?? accessors.id, dir);
  }, [backends.data, sort, dir]);

  const columns: Column<BackendOut>[] = [
    { key: "id", header: "Backend", sortKey: "id", naturalDir: "asc", render: (b) => <Link to={backendHref(b.id)} className="link-quiet mono">{b.id}</Link>, title: (b) => b.id },
    { key: "definition", header: "Definition", sortKey: "definition", naturalDir: "asc", render: (b) => b.definition },
    { key: "state", header: "State", sortKey: "state", naturalDir: "asc", render: (b) => <BackendState backend={b} /> },
    { key: "since", header: "Since", sortKey: "since", naturalDir: "desc", align: "right", width: "96px", render: (b) => <AbsTime value={b.status === "stopped" ? b.stoppedAt : b.startedAt} /> },
    { key: "ports", header: "Ports", render: (b) => <Ports ports={b.ports} /> },
    { key: "bindings", header: "Bindings", sortKey: "bindings", naturalDir: "desc", align: "right", width: "88px", render: (b) => String(leaseBindings(b).length) },
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
    { key: "description", header: "Description", render: (d) => (d as BackendDefinitionOut & { description?: string }).description ?? "" },
    { key: "ports", header: "Ports", render: (d) => ((d as BackendDefinitionOut & { ports?: string[] }).ports ?? []).join(", ") },
    { key: "path", header: "Found in", render: (d) => <span className="mono muted">{d.path}</span>, title: (d) => d.path },
    { key: "grace", header: "Idle grace", align: "right", width: "96px", render: (d) => {
      const grace = (d as BackendDefinitionOut & { idleGraceSeconds?: number }).idleGraceSeconds;
      return typeof grace === "number" ? `${grace} s` : "";
    } },
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
                  <span>{plural(leaseBindings(b).length, "binding")}</span>
                </div>
              </>
            )}
            empty={<EmptyState>No backends running. Agents start them with eks-harness backend ensure.</EmptyState>}
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
  const leases = useLeases();
  const bound = leaseBindings(backend);
  const sessions = Array.from(new Set(bound.map((sid) => leases.data?.items.find((l) => l.sid === sid)?.sessionName).filter(Boolean)));
  const body = bound.length
    ? `${plural(bound.length, "bound lease")} lose their backend${sessions.length ? `: ${sessions.join(", ")}` : ""} (${bound.join(", ")}).`
    : "No lease is bound to it.";
  return (
    <ConfirmDialog
      title={`Stop ${backend.id}?`}
      body={body}
      confirmLabel="Stop backend"
      busyLabel={"Stopping…"}
      onClose={onClose}
      errorFor={(err) => errorText(err, "stop", backend.id)}
      onConfirm={async () => {
        await api.post(`/api/backends/${backendPath(backend.id)}/stop`, { reason: "stopped from the UI" }, { timeoutMs: 60_000 });
        void client.invalidateQueries({ queryKey: keys.backends });
        void client.invalidateQueries({ queryKey: ["backend"] });
      }}
    />
  );
}

export function BackendDetailPage({ splat }: { splat: string }) {
  const logs = splat.endsWith("/logs");
  const id = decodeURIComponent(logs ? splat.slice(0, -"/logs".length) : splat);
  const backend = useBackend(id);
  const leases = useLeases();
  const auth = useAuth();
  const client = useQueryClient();
  const [stopping, setStopping] = useState(false);
  const [holdBusy, setHoldBusy] = useState(false);
  const [error, setError] = useState("");
  useTitle(`${id} - Backends`);
  const state = queryState(backend, "backend", {
    notFound: <EmptyState action={<Link to="/backends" className="link">Backends</Link>}>There is no backend {id}. It may have stopped and been removed.</EmptyState>,
    forbidden: <Forbidden what={id} />,
  });
  if (state) return <div className="page">{state}</div>;
  const b = backend.data!;
  const activeHolds = holds(b);
  const uiHold = activeHolds.find((h) => h.id === `ui:${auth.username}`);
  const held = activeHolds.length > 0;
  const toggleHold = async () => {
    setHoldBusy(true);
    setError("");
    try {
      await api.post(`/api/backends/${backendPath(b.id)}/hold`, held ? { seconds: 0, holdId: uiHold ? uiHold.id : null } : { seconds: 3600, holdId: `ui:${auth.username}` });
      void client.invalidateQueries({ queryKey: keys.backend(b.id) });
      void client.invalidateQueries({ queryKey: keys.backends });
    } catch (err) {
      setError(errorText(err, held ? "release the hold on" : "hold", b.id));
    } finally {
      setHoldBusy(false);
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
              <Button onClick={() => void toggleHold()} busy={holdBusy} busyLabel={held ? "Releasing…" : "Holding…"}>
                {held ? "Release hold" : "Hold"}
              </Button>
              <Button onClick={() => setStopping(true)}>
                {"Stop…"}
              </Button>
            </>
          ) : null
        }
        meta={
          <p>
            <BackendState backend={b} />
            {started ? ` since ${formatClock(started)}` : ""} for instance <Mono>{b.instance}</Mono>.{b.tree ? <> Tree <Mono>{b.tree}</Mono></> : null}
            {held ? ` Held by ${activeHolds.map((h) => h.id.replace(/^ui:/, "")).join(", ")} until ${formatClock(new Date(Math.max(...activeHolds.map((h) => h.until)) * 1000))}.` : ""}
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
             
              empty={<p className="muted">No processes.</p>}
            />
          </section>
          <section className="section">
            <h2 className="section-title">Captured values</h2>
            <KeyValues values={b.captured} empty="Nothing captured." />
          </section>
          <section className="section">
            <h2 className="section-title">Bindings</h2>
            <DataTable
              rows={leaseBindings(b).map((sid) => ({ sid, lease: leases.data?.items.find((l) => l.sid === sid) }))}
              columns={[
                { key: "sid", header: "Sid", width: "112px", render: (r) => <Mono>{r.sid}</Mono> },
                { key: "session", header: "Session", render: (r) => (r.lease?.projectId && r.lease.sessionSlug ? <Link className="link-quiet" to={sessionUrl(r.lease.projectId, r.lease.sessionSlug)}>{r.lease.sessionName ?? r.lease.sessionSlug}</Link> : null) },
              ]}
              getId={(r) => r.sid}
              label="Bindings"
             
              empty={<p className="muted">No lease is bound to it.</p>}
            />
          </section>
          <section className="section">
            <h2 className="section-title">Exports</h2>
            <KeyValues values={b.exports} empty="Nothing exported." />
          </section>
          <section className="section">
            <h2 className="section-title">Ports</h2>
            <KeyValues values={Object.fromEntries(Object.entries(b.ports).map(([k, v]) => [k, String(v)]))} empty="No ports." />
          </section>
        </div>
      )}
      <PluginSlot slot="backend.inspector" props={{ backend: b as unknown as Record<string, unknown> }} />
      {stopping ? <StopBackendConfirm backend={b} onClose={() => setStopping(false)} /> : null}
    </div>
  );
}

function KeyValues({ values, empty }: { values: Record<string, string>; empty: string }) {
  const rows = Object.entries(values ?? {});
  if (!rows.length) return <p className="muted">{empty}</p>;
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
            <td>
              <CopyLink value={value} />
            </td>
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
