import { Link } from "@tanstack/react-router";
import { useBackends, useBrowsers, useDevices, useProfiles, useStatus } from "../api/queries";
import { useDriverWorkers } from "../api/system";
import { Rack, RunBlock, StateLine } from "../components/Workbench";
import type { BackendOut, DeviceOut, ProfileOut } from "../api/types";
import { AbsTime, MetaItem, Mono, queryState, Section } from "../components/Misc";
import { EmptyState } from "../components/Notice";
import { DataTable, type Column } from "../components/Table";
import { useAuth } from "../lib/auth";
import { plural, stripScheme } from "../lib/format";
import { useTitle } from "../lib/hooks";
import { BackendState, backendHref, leaseBindings, Ports } from "./Backends";
import { deviceName, profileName, SessionLink, StateText } from "./Pools";

export function MachinesPage() {
  useTitle("Lab");
  const workers = useDriverWorkers();
  const status = useStatus();
  const auth = useAuth();
  const devices = useDevices();
  const browsers = useBrowsers();
  const profiles = useProfiles();
  const backends = useBackends();

  const deviceRows = devices.data?.items ?? [];
  const profileRows = profiles.data?.items ?? [];
  const backendRows = backends.data?.items ?? [];
  const alive = browsers.data?.items.filter((b) => b.alive).length ?? 0;
  const profilesInUse = profileRows.filter((p) => p.lease).length;
  const running = backendRows.filter((b) => b.status === "running").length;

  const deviceColumns: Column<DeviceOut>[] = [
    { key: "name", header: "Device", render: (d) => <Link to={`/devices/${d.kind}/${d.index}`} className="link-quiet strong">{deviceName(d)}</Link> },
    { key: "platform", header: "Platform", width: "96px", render: (d) => <span className="ink-2">{d.kind === "ios" ? "iOS" : "Android"}</span> },
    { key: "type", header: "Pool name", render: (d) => <span className="ink-3 mono">{d.name}</span> },
    { key: "state", header: "State", render: (d) => <StateText status={d.status} statusText={d.statusText} lease={d.lease} since={d.statusSince} /> },
    { key: "session", header: "Held by", render: (d) => <SessionLink session={d.session} /> },
    { key: "sid", header: "Sid", render: (d) => (d.lease ? <Mono>{d.lease.sid}</Mono> : null) },
    { key: "queue", header: "Queue", align: "right", width: "72px", render: (d) => (d.queueLength ? <span className="strong">{d.queueLength}</span> : <span className="ink-4">0</span>) },
  ];
  const profileColumns: Column<ProfileOut>[] = [
    { key: "name", header: "Profile", render: (p) => <Link to={`/browsers/profiles/${encodeURIComponent(p.id)}`} className="link-quiet strong">{profileName(p)}</Link> },
    { key: "state", header: "State", render: (p) => <StateText status={p.status} statusText={p.statusText} lease={p.lease} since={p.statusSince} /> },
    { key: "session", header: "Held by", render: (p) => <SessionLink session={p.session} /> },
    { key: "page", header: "Page", render: (p) => <span className="ink-2 mono ellipsis" title={p.pageUrl ?? undefined}>{stripScheme(p.pageUrl)}</span> },
    { key: "queue", header: "Queue", align: "right", width: "72px", render: (p) => (p.queueLength ? <span className="strong">{p.queueLength}</span> : <span className="ink-4">0</span>) },
  ];
  const backendColumns: Column<BackendOut>[] = [
    { key: "id", header: "Backend", render: (b) => <Link to={backendHref(b.id)} className="link-quiet mono strong">{b.id}</Link> },
    { key: "state", header: "State", render: (b) => <BackendState backend={b} /> },
    { key: "since", header: "Since", render: (b) => <AbsTime value={b.status === "stopped" ? b.stoppedAt : b.startedAt} /> },
    { key: "ports", header: "Ports", render: (b) => <Ports ports={b.ports} /> },
    { key: "bindings", header: "Leases", align: "right", width: "72px", render: (b) => leaseBindings(b).length },
  ];

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-title-row">
          <h1 className="page-title" tabIndex={-1} data-page-title="">
            Lab
          </h1>
        </div>
        <div className="page-meta">
          {devices.data ? (
            <MetaItem label="Devices">
              {devices.data.running} of {devices.data.maxRunning} running slots in use
            </MetaItem>
          ) : null}
          {browsers.data ? (
            <MetaItem label="Browsers">
              {alive} of {browsers.data.items.length} processes, {profilesInUse} of {profileRows.length} profiles in use, <Mono>{browsers.data.command}</Mono>
            </MetaItem>
          ) : null}
          {backends.data ? <MetaItem label="Backends">{running} running</MetaItem> : null}
        </div>
      </header>
      <div className="stack machines">
        <Section title="Devices" count={devices.data ? deviceRows.length : null} flush actions={<Link to="/devices" className="link">Devices</Link>}>
          {queryState(devices, "devices") ?? (
            <DataTable
              rows={deviceRows}
              columns={deviceColumns}
              getId={(d) => d.key}
              getHref={(d) => `/devices/${d.kind}/${d.index}`}
              label="Devices"
              compact
              mobileRow={(d) => (
                <>
                  <div className="row-title">
                    {deviceName(d)} <StateText status={d.status} statusText={d.statusText} lease={d.lease} />
                  </div>
                  <div className="row-meta">
                    <span>{d.name}</span>
                    {d.session ? <SessionLink session={d.session} /> : null}
                    {d.queueLength ? <span>queue {d.queueLength}</span> : null}
                  </div>
                </>
              )}
              empty={<EmptyState action={auth.isAdmin ? <Link to="/settings/devices" className="link">Settings</Link> : undefined}>No devices in the pool. Set devices.ios or devices.android in Settings, Devices.</EmptyState>}
            />
          )}
        </Section>
        <Section title="Browser profiles" count={profiles.data ? profileRows.length : null} actions={<Link to="/browsers" className="link">Browsers</Link>}>
          {queryState(profiles, "browser profiles") ?? (
            <DataTable
              rows={profileRows}
              columns={profileColumns}
              getId={(p) => p.id}
              getHref={(p) => `/browsers/profiles/${encodeURIComponent(p.id)}`}
              label="Browser profiles"
              compact
              mobileRow={(p) => (
                <>
                  <div className="row-title">
                    {profileName(p)} <StateText status={p.status} statusText={p.statusText} lease={p.lease} />
                  </div>
                  <div className="row-meta">
                    {p.session ? <SessionLink session={p.session} /> : <span>free</span>}
                    {p.pageUrl ? <span className="mono ellipsis">{stripScheme(p.pageUrl)}</span> : null}
                  </div>
                </>
              )}
              empty={<EmptyState action={auth.isAdmin ? <Link to="/settings/browser" className="link">Settings</Link> : undefined}>No browser profiles. Set browser.instances in Settings, Browser.</EmptyState>}
            />
          )}
        </Section>
        {status.data?.queue.length ? (
          <Section title="Waiting for a machine" count={status.data.queue.length}>
            <div className="run-list">
              {status.data.queue.map((lease) => (
                <RunBlock key={lease.sid} title={`${lease.kind} for ${lease.sessionName ?? lease.projectId ?? lease.sid}`} state={`queued${lease.queuePosition ? ` (${ordinal(lease.queuePosition)})` : ""}`} tone="wait" facts={[<span key="sid" className="mono">{lease.sid}</span>, lease.projectId]} />
              ))}
            </div>
          </Section>
        ) : null}
        <Section title="Backends" count={backends.data ? backendRows.length : null} actions={<Link to="/backends" className="link">Backends</Link>}>
          {queryState(backends, "backends") ?? (
            <DataTable
              rows={backendRows}
              columns={backendColumns}
              getId={(b) => b.id}
              getHref={(b) => backendHref(b.id)}
              label="Backends"
              compact
              mobileRow={(b) => (
                <>
                  <div className="row-title mono">{b.id}</div>
                  <div className="row-meta">
                    <BackendState backend={b} />
                    <span>{plural(leaseBindings(b).length, "lease")}</span>
                  </div>
                </>
              )}
              empty={<EmptyState>No backends running. Agents start them with <span className="mono">eks-harness backend ensure</span>.</EmptyState>}
            />
          )}
        </Section>
        <Section title="Driver workers" count={workers.data ? workers.data.items.length : null}>
          {queryState(workers, "driver workers") ?? (
            <Rack
              rows={workers.data?.items ?? []}
              rowKey={(w) => `${w.kind}-${w.sid}`}
              label="Driver workers"
              columns={[
                { key: "sid", label: "Lease", width: "minmax(0, 1fr)", render: (w) => <Mono>{w.sid}</Mono> },
                { key: "kind", label: "Worker", width: "minmax(0, 1fr)", render: (w) => (w.kind === "web" ? "web driver" : "mobile driver") },
                { key: "state", label: "State", width: "minmax(0, 1fr)", render: (w) => <StateLine state={w.state ?? "running"} /> },
                { key: "port", label: "Port", width: "80px", align: "end", render: (w) => <span className="num">{w.port}</span> },
              ]}
              empty={<span>No driver workers. A flow starts one per lease: <span className="mono">eks-harness flow run</span>.</span>}
            />
          )}
        </Section>
      </div>
    </div>
  );
}

function ordinal(n: number): string {
  const suffix = n % 10 === 1 && n % 100 !== 11 ? "st" : n % 10 === 2 && n % 100 !== 12 ? "nd" : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th";
  return `${n}${suffix}`;
}
