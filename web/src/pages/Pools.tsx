import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { api, encodeSegment, errorText } from "../api/client";
import { keys, useBrowsers, useDevice, useDevices, useProfile, useProfiles, useSettings } from "../api/queries";
import type { BrowserProcessOut, DeviceDetail, DeviceOut, EventOut, LeaseBrief, LeaseOut, PoolAction, PoolActionResponse, ProfileDetail, ProfileOut, SessionBrief } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Field, TextInput } from "../components/Form";
import { OverflowMenu, separator, type MenuItem } from "../components/Menu";
import { AbsTime, DefList, Forbidden, Mono, PageHeader, queryState, Tabs } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { DataTable, sortPatch, sortRows, useSort, type Column, type Natural } from "../components/Table";
import { useAuth } from "../lib/auth";
import { setFocusedResource } from "../lib/events";
import { formatAgoShort, formatClock, formatFull, formatIdle, formatWaiting, plural, resourceName, stripScheme, toDate } from "../lib/format";
import { useMinute, useTitle } from "../lib/hooks";
import { localPath, sessionUrl, useSearchParams, useSetParams } from "../lib/url";
import { LiveFrame } from "../viewers/LiveFrame";
import { ToolRail } from "../features/ToolRail";
import { eventSentence, isMachineActor, leaseStateWords } from "./Session";

const STATUS_WORDS: Record<string, string> = {
  off: "Shut down",
  stopped: "Shut down",
  shutdown: "Shut down",
  booting: "Booting",
  starting: "Starting",
  on: "Running",
  running: "Running",
  stopping: "Shutting down",
  resetting: "Resetting",
  deleting: "Deleting",
  failed: "Failed",
  free: "Free",
};

function isRunning(status: string): boolean {
  return ["on", "running", "booting", "starting", "busy", "in_use", "idle", "free"].includes(status);
}

function isOff(status: string): boolean {
  return ["off", "stopped", "shutdown", "failed", ""].includes(status);
}

export function stateWord(status: string, statusText: string, lease: LeaseBrief | null): string {
  if (statusText) return statusText;
  if (lease && isRunning(status)) {
    if (lease.state === "idle") return formatIdle(lease.idleSeconds);
    return "In use";
  }
  return STATUS_WORDS[status] ?? (status ? status[0].toUpperCase() + status.slice(1) : "Unknown");
}

export function StateText({ status, statusText, lease, since }: { status: string; statusText: string; lease: LeaseBrief | null; since?: string | null }) {
  const word = stateWord(status, statusText, lease);
  const failed = /fail/i.test(status) || /^failed/i.test(word);
  const attention = /^unknown|^no heartbeat/i.test(word);
  const state = failed ? "failed" : attention ? "attention" : lease && isRunning(status) ? "active" : status;
  return (
    <span className="state-word" data-state={state} title={since ? formatFull(since) : undefined}>
      {word}
    </span>
  );
}

export function SessionLink({ session }: { session: SessionBrief | null }) {
  if (!session) return null;
  return (
    <Link to={sessionUrl(session.projectId, session.slug)} className="link-quiet" title={`${session.projectId} / ${session.name}`}>
      {session.name}
    </Link>
  );
}

export function Owner({ lease }: { lease: LeaseBrief | null }) {
  if (!lease) return null;
  if (lease.ownerKind === "manual") return <span>Manual {lease.ownerUsername ?? ""}</span>;
  return (
    <span>
      Agent <Mono>{lease.ownerInstance}</Mono>
    </span>
  );
}

interface ActionTarget {
  name: string;
  confirmKey: string;
  kind: "device" | "profile" | "browser";
  path: string;
  lease: LeaseBrief | null;
  session: SessionBrief | null;
  typeLabel?: string;
}

function actionCopy(action: PoolAction, target: ActionTarget, extra: { slots?: string }) {
  const interrupt = target.lease && target.session ? `${target.session.name} (${target.lease.sid})` : target.lease ? target.lease.sid : "";
  const who = target.lease?.ownerKind === "manual" ? `${target.lease.ownerUsername ?? "a user"}` : "the agent";
  if (target.kind === "browser") {
    return {
      title: `Stop ${target.name}?`,
      body: interrupt ? `This closes the profiles in use by ${interrupt}. Their agents' next commands fail.` : "Nothing is using it.",
      button: "Stop browser",
    };
  }
  switch (action) {
    case "start":
      return {
        title: `Start ${target.name}?`,
        body: `${target.kind === "device" ? "It boots in a window" : "It opens in the shared browser"} under a manual lease owned by you. Release it from this page when you are done.${extra.slots ? ` ${extra.slots}` : ""}`,
        button: `Start ${target.name}`,
      };
    case "shutdown":
      return {
        title: `Shut down ${target.name}?`,
        body: target.lease
          ? `This interrupts ${target.session ? `${target.session.name} in ${target.session.projectId}` : target.lease.sid}: the lease ${target.lease.sid} held by ${who} is broken, and the agent's next command fails.`
          : "Nothing is using it.",
        button: `Shut down ${target.name}`,
      };
    case "reset":
      return {
        title: `Reset ${target.name}?`,
        body:
          (target.kind === "device"
            ? "This erases all content and settings on the device (apps, data, keychain). Nothing is installed afterwards."
            : "This deletes the profile's cookies, local storage and cache.") + (interrupt ? ` It also interrupts ${interrupt}.` : ""),
        button: `Reset ${target.name}`,
      };
    case "delete":
      return {
        title: `Delete ${target.name}?`,
        body:
          (target.kind === "device"
            ? `This removes the ${target.typeLabel ?? "device"} ${target.name} entirely. The pool creates a fresh one the next time it is needed.`
            : "This removes the profile directory. The pool recreates it on demand.") + (interrupt ? ` It also interrupts ${interrupt}.` : ""),
        button: `Delete ${target.name}`,
      };
  }
}

function PoolActionConfirm({ action, target, onClose, slots, onDone }: { action: PoolAction; target: ActionTarget; onClose: () => void; slots?: string; onDone?: (result: PoolActionResponse) => void }) {
  const client = useQueryClient();
  const copy = actionCopy(action, target, { slots });
  const busy: Record<PoolAction, string> = { start: "Starting…", shutdown: "Shutting down…", reset: "Resetting…", delete: "Deleting…" };
  return (
    <ConfirmDialog
      title={copy.title}
      body={copy.body}
      confirmLabel={copy.button}
      busyLabel={target.kind === "browser" ? "Stopping…" : busy[action]}
      destructive={action !== "start"}
      typeToConfirm={action === "delete" ? target.name : undefined}
      onClose={onClose}
      errorFor={(err) => errorText(err, action === "shutdown" ? "shut down" : action, target.name)}
      onConfirm={async () => {
        const result = await api.post<PoolActionResponse>(`${target.path}/${action}`, { confirm: action === "start" ? undefined : target.confirmKey, reason: `${action} from the UI` }, { timeoutMs: 120_000 });
        void client.invalidateQueries({ queryKey: keys.devices });
        void client.invalidateQueries({ queryKey: ["device"] });
        void client.invalidateQueries({ queryKey: keys.profiles });
        void client.invalidateQueries({ queryKey: ["profile"] });
        void client.invalidateQueries({ queryKey: keys.browsers });
        void client.invalidateQueries({ queryKey: keys.status });
        onDone?.(result);
      }}
    />
  );
}

function availableActions(status: string): PoolAction[] {
  const actions: PoolAction[] = [];
  if (isOff(status)) actions.push("start");
  if (isRunning(status) || status === "stopping") actions.push("shutdown");
  if (isOff(status) || isRunning(status)) actions.push("reset");
  actions.push("delete");
  return actions;
}

const ACTION_LABELS: Record<PoolAction, string> = { start: "Start…", shutdown: "Shut down…", reset: "Reset…", delete: "Delete…" };

export function deviceName(device: { kind: string; index: number; name?: string }): string {
  return resourceName(`${device.kind}:${device.index}`) || device.name || "";
}

const DEVICE_NATURAL: Natural = { name: "asc", state: "asc", since: "desc", queue: "desc" };

export function DevicesPage() {
  useTitle("Devices");
  const auth = useAuth();
  const devices = useDevices();
  const settings = useSettings(auth.isAdmin);
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const [action, setAction] = useState<{ action: PoolAction; device: DeviceOut } | null>(null);
  const { sort, dir } = useSort(params, "name", "asc", DEVICE_NATURAL);
  const kind = params.kind === "ios" || params.kind === "android" ? params.kind : "";
  const now = useMinute();

  const all = devices.data?.items ?? [];
  const rows = useMemo(() => {
    const list = all.filter((d) => !kind || d.kind === kind);
    const accessors: Record<string, (d: DeviceOut) => unknown> = {
      name: (d) => `${d.kind}:${String(d.index).padStart(4, "0")}`,
      state: (d) => stateWord(d.status, d.statusText, d.lease),
      since: (d) => (d.statusSince ? Date.parse(d.statusSince) : 0),
      queue: (d) => d.queueLength,
    };
    return sortRows(list, accessors[sort] ?? accessors.name, dir);
  }, [all, kind, sort, dir, now]);

  const idle = settings.data?.settings.find((s) => s.key === "devices.idleSeconds")?.value;
  const sentence = devices.data
    ? `${devices.data.running} of ${devices.data.maxRunning} running slots in use.${typeof idle === "number" && idle > 0 ? ` Free devices shut down after ${Math.round(idle / 60)} min.` : ""}`
    : "";

  const menu = (device: DeviceOut): MenuItem[] => {
    const items: MenuItem[] = [{ label: "Open", onSelect: () => void navigate({ to: `/devices/${device.kind}/${device.index}` }) }, separator()];
    for (const a of availableActions(device.status)) items.push({ label: ACTION_LABELS[a], danger: a === "delete", onSelect: () => setAction({ action: a, device }) });
    return items;
  };

  const columns: Column<DeviceOut>[] = [
    { key: "name", header: "Device", sortKey: "name", naturalDir: "asc", width: "112px", render: (d) => <Link to={`/devices/${d.kind}/${d.index}`} className="link-quiet">{deviceName(d)}</Link> },
    { key: "type", header: "Type", render: (d) => <span className="muted">{d.name}</span> },
    { key: "state", header: "State", sortKey: "state", naturalDir: "asc", render: (d) => <StateText status={d.status} statusText={d.statusText} lease={d.lease} since={d.statusSince} /> },
    { key: "since", header: "Since", sortKey: "since", naturalDir: "desc", align: "right", width: "96px", render: (d) => <AbsTime value={d.statusSince} /> },
    { key: "session", header: "Session", render: (d) => <SessionLink session={d.session} /> },
    { key: "sid", header: "Sid", width: "96px", render: (d) => (d.lease ? <Mono>{d.lease.sid}</Mono> : null) },
    { key: "owner", header: "Owner", render: (d) => <Owner lease={d.lease} /> },
    { key: "queue", header: "Queue", sortKey: "queue", naturalDir: "desc", align: "right", width: "72px", render: (d) => (d.queueLength ? <span className="strong">{d.queueLength}</span> : "0") },
    { key: "more", header: <span className="visually-hidden">Actions</span>, width: "var(--h-control)", render: (d) => <OverflowMenu label={`More actions for ${deviceName(d)}`} items={menu(d)} /> },
  ];

  const state = queryState(devices, "devices");
  const counts = { all: all.length, ios: all.filter((d) => d.kind === "ios").length, android: all.filter((d) => d.kind === "android").length };
  const slots = devices.data && devices.data.running >= devices.data.maxRunning ? `${devices.data.running} of ${devices.data.maxRunning} running slots are in use; a free device is shut down to make room.` : undefined;

  return (
    <div className="page">
      <PageHeader title="Devices" meta={sentence ? <p>{sentence}</p> : null} />
      <Tabs
        label="Device kinds"
        items={[
          { label: "All", to: "/devices", count: counts.all, active: !kind },
          { label: "iOS", to: "/devices", search: { kind: "ios" }, count: counts.ios, active: kind === "ios" },
          { label: "Android", to: "/devices", search: { kind: "android" }, count: counts.android, active: kind === "android" },
        ]}
      />
      {state ?? (
        <DataTable
          rows={rows}
          columns={columns}
          getId={(d) => d.key}
          getHref={(d) => `/devices/${d.kind}/${d.index}`}
          sort={sort}
          dir={dir}
          onSort={(key, d) => setParams(sortPatch(key, d, "name", "asc", DEVICE_NATURAL))}
          label="Devices"
          primary
          mobileRow={(d) => (
            <>
              <div className="row-title">
                <Link to={`/devices/${d.kind}/${d.index}`}>{deviceName(d)}</Link>
                <StateText status={d.status} statusText={d.statusText} lease={d.lease} since={d.statusSince} />
              </div>
              {d.session || d.lease ? (
                <div className="row-meta">
                  {d.session ? <span>{d.session.name}</span> : null}
                  {d.lease ? <Mono>{d.lease.sid}</Mono> : null}
                </div>
              ) : null}
              <div className="row-meta">
                {d.statusSince ? (
                  <span>
                    Since <AbsTime value={d.statusSince} />
                  </span>
                ) : null}
                <span>Queue {d.queueLength}</span>
              </div>
            </>
          )}
          empty={
            <EmptyState action={auth.isAdmin ? <Link to="/settings/devices" className="link">Settings</Link> : undefined}>
              No devices in the pool. Set devices.ios or devices.android in Settings {">"} Devices.
            </EmptyState>
          }
        />
      )}
      {action ? (
        <PoolActionConfirm
          action={action.action}
          target={{ name: deviceName(action.device), confirmKey: action.device.key, kind: "device", path: `/api/devices/${action.device.kind}/${action.device.index}`, lease: action.device.lease, session: action.device.session, typeLabel: action.device.kind === "ios" ? "simulator" : "emulator" }}
          slots={slots}
          onClose={() => setAction(null)}
        />
      ) : null}
    </div>
  );
}

function ActivityTable({ events, empty }: { events: EventOut[]; empty: string }) {
  const rows = [...events].sort((a, b) => b.id - a.id).slice(0, 200);
  const columns: Column<EventOut>[] = [
    { key: "time", header: "Time", width: "96px", render: (e) => <time dateTime={e.ts} title={formatFull(e.ts)}>{formatClock(toDate(e.ts) ?? new Date(), true)}</time> },
    { key: "event", header: "Event", render: (e) => eventSentence(e) },
    { key: "actor", header: "Actor", width: "160px", title: (e) => e.actor ?? "daemon", render: (e) => (e.actor ? isMachineActor(e.actor) ? <Mono>{e.actor}</Mono> : e.actor : "daemon") },
  ];
  return <DataTable rows={rows} columns={columns} getId={(e) => String(e.id)} label="Activity" empty={<EmptyState>{empty}</EmptyState>} />;
}

function HistoryTable({ history }: { history: LeaseOut[] }) {
  const params = useSearchParams();
  const setParams = useSetParams();
  const since = params.since ? Date.parse(params.since) : NaN;
  const [page, setPage] = useState(0);
  const rows = [...history]
    .filter((l) => !Number.isFinite(since) || Date.parse(l.acquiredAt ?? l.queuedAt ?? "") >= since)
    .sort((a, b) => Date.parse(b.acquiredAt ?? b.queuedAt ?? "") - Date.parse(a.acquiredAt ?? a.queuedAt ?? ""));
  const pageRows = rows.slice(page * 100, page * 100 + 100);
  const describe = (l: LeaseOut) => {
    const end = l.state === "released" ? "released" : l.state === "broken" ? "broken" : l.state;
    return (
      <>
        Lease <Mono>{l.sid}</Mono> {end}
        {l.reason ? ` (${l.reason})` : ""}
      </>
    );
  };
  const columns: Column<LeaseOut>[] = [
    { key: "time", header: "Time", width: "200px", render: (l) => formatFull(l.acquiredAt ?? l.queuedAt) },
    { key: "event", header: "Event", render: describe },
    { key: "actor", header: "Actor", render: (l) => (l.ownerKind === "manual" ? l.ownerUsername ?? "Manual" : <Mono>{l.ownerInstance}</Mono>) },
    { key: "session", header: "Session", render: (l) => (l.projectId && l.sessionSlug ? <Link className="link-quiet" to={sessionUrl(l.projectId, l.sessionSlug)}>{l.sessionName ?? l.sessionSlug}</Link> : null) },
  ];
  const dateId = useId();
  return (
    <div>
      <div className="toolbar page-tools">
        <label htmlFor={dateId} className="field-label">
          Since
        </label>
        <TextInput id={dateId} type="date" value={params.since ?? ""} onChange={(event) => { setPage(0); setParams({ since: event.target.value || null }, { replace: true }); }} className="date-input" />
      </div>
      <DataTable rows={pageRows} columns={columns} getId={(l) => l.id || l.sid} label="History" empty={<EmptyState>No leases in this period.</EmptyState>} />
      {rows.length > 100 ? (
        <div className="pagination">
          <span className="pagination-range">
            {page * 100 + 1}-{Math.min(rows.length, page * 100 + 100)} of {rows.length.toLocaleString("en-US")}
          </span>
          <span className="pagination-buttons">
            <Button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
              Previous
            </Button>
            <Button disabled={(page + 1) * 100 >= rows.length} onClick={() => setPage((p) => p + 1)}>
              Next
            </Button>
          </span>
        </div>
      ) : null}
    </div>
  );
}

function QueueTable({ queue }: { queue: LeaseOut[] }) {
  const now = useMinute();
  const columns: Column<LeaseOut>[] = [
    { key: "position", header: "Position", align: "right", width: "80px", render: (l) => String(queue.indexOf(l) + 1) },
    {
      key: "session",
      header: "Session",
      render: (l) =>
        l.projectId ? (
          <span className="two-line">
            <span>{l.projectId}</span>
            <span>{l.sessionName ?? l.sessionSlug}</span>
          </span>
        ) : (
          <Mono>{l.sid}</Mono>
        ),
    },
    { key: "instance", header: "Instance", render: (l) => <Mono>{l.ownerInstance}</Mono> },
    { key: "waiting", header: "Waiting", align: "right", width: "96px", render: (l) => {
      const since = toDate(l.queuedAt ?? l.acquiredAt);
      return since ? formatWaiting((now.getTime() - since.getTime()) / 1000) : "";
    } },
  ];
  return <DataTable rows={queue} columns={columns} getId={(l) => l.sid} label="Queue" empty={<p className="muted">Nobody is waiting.</p>} />;
}

function CurrentLease({ lease, session, onRelease, onBreak }: { lease: LeaseBrief | null; session: SessionBrief | null; onRelease: () => void; onBreak: () => void }) {
  const now = useMinute();
  if (!lease) return <p>No lease. Free for the next agent.</p>;
  const manual = lease.ownerKind === "manual";
  return (
    <div className="stack">
      <DefList
        items={[
          session ? { label: "Session", value: <Link className="link" to={sessionUrl(session.projectId, session.slug)}>{session.projectId} {session.name}</Link> } : null,
          { label: "Sid", value: lease.sid, mono: true, copy: lease.sid },
          { label: "Owner", value: manual ? `Manual (${lease.ownerUsername ?? "unknown"}), started from the UI` : <>Agent <Mono>{lease.ownerInstance}</Mono></> },
          { label: "State", value: leaseStateWords(lease, now) },
          lease.acquiredAt ? { label: "Acquired", value: formatFull(lease.acquiredAt) } : null,
          lease.heartbeatAt ? { label: "Heartbeat", value: formatAgoShort(lease.heartbeatAt, now) } : null,
        ]}
      />
      <div>
        {manual ? (
          <Button onClick={onRelease}>Release</Button>
        ) : (
          <Button onClick={onBreak}>
            {"Break lease…"}
          </Button>
        )}
      </div>
    </div>
  );
}

function LeaseConfirms({ mode, lease, session, name, onClose }: { mode: "release" | "break"; lease: LeaseBrief; session: SessionBrief | null; name: string; onClose: () => void }) {
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const invalidate = () => {
    void client.invalidateQueries({ queryKey: keys.devices });
    void client.invalidateQueries({ queryKey: ["device"] });
    void client.invalidateQueries({ queryKey: keys.profiles });
    void client.invalidateQueries({ queryKey: ["profile"] });
    void client.invalidateQueries({ queryKey: keys.status });
  };
  if (mode === "release") {
    return (
      <ConfirmDialog
        title={`Release ${name}?`}
        body="You started it from the UI. Releasing makes it free for agents; it shuts down after a while without use."
        confirmLabel={`Release ${name}`}
        busyLabel={"Releasing…"}
        destructive={false}
        onClose={onClose}
        errorFor={(err) => errorText(err, "release", name)}
        onConfirm={async () => {
          await api.post(`/api/leases/${encodeSegment(lease.sid)}/release`, { reason: "released from the UI" });
          invalidate();
        }}
      />
    );
  }
  return (
    <ConfirmDialog
      title={`Break the lease ${lease.sid} on ${name}?`}
      body={`The agent working on ${session?.name ?? "this lease"} loses the ${name.startsWith("Browser") ? "profile" : "device"}. Its next command fails and tells it to reacquire. The ${name.startsWith("Browser") ? "profile" : "device"} keeps running.`}
      confirmLabel="Break lease"
      busyLabel={"Breaking…"}
      onClose={onClose}
      extraValid={reason.trim().length > 0}
      extra={
        <Field label="Reason (shown to the agent)" htmlFor={reasonId}>
          <TextInput id={reasonId} value={reason} onChange={(event) => setReason(event.target.value)} data-autofocus="" />
        </Field>
      }
      errorFor={(err) => errorText(err, "break", "the lease")}
      onConfirm={async () => {
        await api.post("/api/leases/break", { sid: lease.sid, reason: reason.trim() });
        invalidate();
      }}
    />
  );
}

function DetailLayout({ title, crumbs, sentence, actions, live, rail, lease, queue, tab, basePath, activity, history, activityEmpty, error }: { title: string; crumbs: { label: string; to: string }[]; sentence: ReactNode; actions: ReactNode; live: ReactNode; rail?: ReactNode; lease: ReactNode; queue: LeaseOut[]; tab: string; basePath: string; activity: EventOut[]; history: LeaseOut[]; activityEmpty: string; error?: string }) {
  return (
    <div className="page">
      <PageHeader crumbs={crumbs} title={title} actions={<div className="action-row">{actions}</div>} meta={<p>{sentence}</p>} />
      {error ? <Notice variant="error">{error}</Notice> : null}
      <div className="pool-split">
        <div className="pool-live">{live}</div>
        {rail}
        <div className="pool-info">
          <section className="section">
            <h2 className="section-title">Current lease</h2>
            {lease}
          </section>
          <section className="section">
            <h2 className="section-title">Queue</h2>
            <QueueTable queue={queue} />
          </section>
        </div>
      </div>
      <Tabs
        label="Activity and history"
        items={[
          { label: "Activity", to: basePath, active: tab !== "history" },
          { label: "History", to: basePath, search: { tab: "history" }, active: tab === "history" },
        ]}
      />
      {tab === "history" ? <HistoryTable history={history} /> : <ActivityTable events={activity} empty={activityEmpty} />}
    </div>
  );
}

export function DeviceDetailPage({ kind, index }: { kind: string; index: string }) {
  const device = useDevice(kind, index);
  const devices = useDevices();
  const params = useSearchParams();
  const name = resourceName(`${kind}:${index}`);
  useTitle(`${name} - Devices`);
  const [action, setAction] = useState<PoolAction | null>(null);
  const [leaseMode, setLeaseMode] = useState<"release" | "break" | null>(null);
  useEffect(() => {
    setFocusedResource(`${kind}:${index}`);
    return () => setFocusedResource(null);
  }, [kind, index]);
  const state = queryState(device, "device", {
    notFound: <EmptyState action={<Link to="/devices" className="link">Devices</Link>}>There is no device {name}. It may have been removed from the pool.</EmptyState>,
    forbidden: <Forbidden what={name} />,
  });
  if (state) return <div className="page">{state}</div>;
  const d = device.data as DeviceDetail;
  const running = isRunning(d.status);
  const actions = availableActions(d.status);
  const idText = d.kind === "ios" ? (d.udid ? <Mono>{d.udid}</Mono> : null) : (
    <>
      AVD <Mono>{d.name}</Mono>
      {d.serial ? <>, serial <Mono>{d.serial}</Mono></> : null}
    </>
  );
  const slots = devices.data && devices.data.running >= devices.data.maxRunning ? `${devices.data.running} of ${devices.data.maxRunning} running slots are in use; a free device is shut down to make room.` : undefined;
  const target: ActionTarget = { name, confirmKey: d.key, kind: "device", path: `/api/devices/${d.kind}/${d.index}`, lease: d.lease, session: d.session, typeLabel: d.kind === "ios" ? "simulator" : "emulator" };
  return (
    <>
      <DetailLayout
        title={name}
        crumbs={[{ label: "Devices", to: "/devices" }]}
        sentence={
          <>
            <StateText status={d.status} statusText={d.statusText} lease={d.lease} /> since <AbsTime value={d.statusSince} />. {d.kind === "ios" ? d.name : ""}
            {d.kind === "ios" && d.udid ? ", " : ""}
            {idText}.
          </>
        }
        actions={actions.map((a) => (
          <Button key={a} variant="secondary" onClick={() => setAction(a)}>
            {a === "start" ? "Start" : a === "shutdown" ? "Shut down" : a === "reset" ? "Reset" : "Delete"}
          </Button>
        ))}
        live={<LiveFrame src={d.liveUrl ? localPath(d.liveUrl) : `/api/devices/${d.kind}/${d.index}/live`} name={name} running={running && d.status !== "booting"} aspect={screenAspect(d, d.kind === "ios" ? "9 / 19.5" : "9 / 20")} viewers={d.liveViewers} onStart={() => setAction("start")} />}
        rail={<ToolRail sid={d.lease?.sid ?? null} platform={d.kind === "ios" ? "ios" : "android"} device={{ kind: d.kind, index: d.index }} />}
        lease={<CurrentLease lease={d.lease} session={d.session} onRelease={() => setLeaseMode("release")} onBreak={() => setLeaseMode("break")} />}
        queue={d.queue}
        tab={params.tab ?? "activity"}
        basePath={`/devices/${kind}/${index}`}
        activity={d.activity}
        history={d.history}
        activityEmpty="No activity since the daemon started."
      />
      {action ? <PoolActionConfirm action={action} target={target} slots={slots} onClose={() => setAction(null)} /> : null}
      {leaseMode && d.lease ? <LeaseConfirms mode={leaseMode} lease={d.lease} session={d.session} name={name} onClose={() => setLeaseMode(null)} /> : null}
    </>
  );
}

export function profileName(profile: { browser: number; profile: number }): string {
  return `Browser ${profile.browser}.${profile.profile}`;
}

function profilePage(profile: ProfileOut): string {
  return profile.pageUrl ?? "";
}

function profileSince(profile: ProfileOut): string | null {
  return profile.statusSince ?? profile.lease?.acquiredAt ?? null;
}

function screenAspect(item: { screenWidth: number | null; screenHeight: number | null }, fallback: string): string {
  return item.screenWidth && item.screenHeight ? `${item.screenWidth} / ${item.screenHeight}` : fallback;
}

const PROFILE_NATURAL: Natural = { name: "asc", state: "asc", queue: "desc" };

export function BrowsersPage({ tab }: { tab: "profiles" | "processes" }) {
  useTitle("Browsers");
  const browsers = useBrowsers();
  const profiles = useProfiles();
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const now = useMinute();
  const [action, setAction] = useState<{ action: PoolAction; target: ActionTarget } | null>(null);
  const { sort, dir } = useSort(params, "name", "asc", PROFILE_NATURAL);

  const profileRows = useMemo(() => {
    const accessors: Record<string, (p: ProfileOut) => unknown> = {
      name: (p) => p.browser * 1000 + p.profile,
      state: (p) => stateWord(p.status, p.statusText, p.lease),
      queue: (p) => p.queueLength,
    };
    return sortRows(profiles.data?.items ?? [], accessors[sort] ?? accessors.name, dir);
  }, [profiles.data, sort, dir, now]);

  const processes = browsers.data?.items ?? [];
  const allProfiles = profiles.data?.items ?? [];
  const inUse = allProfiles.filter((p) => p.lease).length;
  const sentence = browsers.data
    ? `${processes.filter((p) => p.alive).length} of ${processes.length} browser processes running with ${inUse} of ${allProfiles.length} profiles in use. Browser: ${browsers.data.command}.`
    : "";

  const profileMenu = (p: ProfileOut): MenuItem[] => {
    const target: ActionTarget = { name: profileName(p), confirmKey: p.id, kind: "profile", path: `/api/profiles/${encodeSegment(p.id)}`, lease: p.lease, session: p.session };
    const items: MenuItem[] = [{ label: "Open", onSelect: () => void navigate({ to: `/browsers/profiles/${encodeURIComponent(p.id)}` }) }, separator()];
    for (const a of availableActions(p.status)) items.push({ label: ACTION_LABELS[a], danger: a === "delete", onSelect: () => setAction({ action: a, target }) });
    return items;
  };

  const profileColumns: Column<ProfileOut>[] = [
    { key: "name", header: "Profile", sortKey: "name", naturalDir: "asc", width: "120px", render: (p) => <Link to={`/browsers/profiles/${encodeURIComponent(p.id)}`} className="link-quiet">{profileName(p)}</Link> },
    { key: "state", header: "State", sortKey: "state", naturalDir: "asc", render: (p) => <StateText status={p.status} statusText={p.statusText} lease={p.lease} /> },
    { key: "since", header: "Since", align: "right", width: "96px", render: (p) => <AbsTime value={profileSince(p)} /> },
    { key: "session", header: "Session", render: (p) => <SessionLink session={p.session} /> },
    { key: "sid", header: "Sid", width: "96px", render: (p) => (p.lease ? <Mono>{p.lease.sid}</Mono> : null) },
    { key: "page", header: "Page", render: (p) => <span className="muted">{stripScheme(profilePage(p))}</span>, title: (p) => profilePage(p) },
    { key: "queue", header: "Queue", sortKey: "queue", naturalDir: "desc", align: "right", width: "72px", render: (p) => (p.queueLength ? <span className="strong">{p.queueLength}</span> : "0") },
    { key: "more", header: <span className="visually-hidden">Actions</span>, width: "var(--h-control)", render: (p) => <OverflowMenu label={`More actions for ${profileName(p)}`} items={profileMenu(p)} /> },
  ];

  const processColumns: Column<BrowserProcessOut>[] = [
    { key: "name", header: "Process", width: "112px", render: (b) => `Browser ${b.index}` },
    { key: "command", header: "Command", render: (b) => b.binary ?? browsers.data?.command ?? "" },
    { key: "state", header: "State", render: (b) => <StateText status={b.status} statusText={b.statusText} lease={null} /> },
    { key: "started", header: "Started", align: "right", width: "112px", render: (b) => <AbsTime value={b.launchedAt} /> },
    { key: "pid", header: "PID", width: "88px", render: (b) => (b.pid ? <Mono>{b.pid}</Mono> : null) },
    { key: "port", header: "CDP port", width: "96px", render: (b) => (b.port ? <Mono>{b.port}</Mono> : null) },
    { key: "profiles", header: "Profiles in use", align: "right", width: "128px", render: (b) => `${b.profiles.filter((p) => p.lease).length} of ${b.profiles.length}` },
    {
      key: "more",
      header: <span className="visually-hidden">Actions</span>,
      width: "var(--h-control)",
      render: (b) => {
        const leased = b.profiles.filter((p) => p.lease);
        const target: ActionTarget = { name: `Browser ${b.index}`, confirmKey: `browser:${b.index}`, kind: "browser", path: `/api/browsers/${b.index}`, lease: leased[0]?.lease ?? null, session: leased[0]?.session ?? null };
        return <OverflowMenu label={`More actions for Browser ${b.index}`} items={b.alive ? [{ label: "Stop…", danger: true, onSelect: () => setAction({ action: "shutdown", target: { ...target, name: `Browser ${b.index}` } }) }] : [{ label: "Nothing to do", disabled: true }]} />;
      },
    },
  ];

  const state = tab === "profiles" ? queryState(profiles, "profiles") : queryState(browsers, "browser processes");
  const auth = useAuth();

  return (
    <div className="page">
      <PageHeader title="Browsers" meta={sentence ? <p>{sentence}</p> : null} />
      <Tabs
        label="Browser sections"
        items={[
          { label: "Profiles", to: "/browsers", count: allProfiles.length, active: tab === "profiles" },
          { label: "Processes", to: "/browsers/processes", count: processes.length, active: tab === "processes" },
        ]}
      />
      {state ??
        (tab === "profiles" ? (
          <DataTable
            rows={profileRows}
            columns={profileColumns}
            getId={(p) => p.id}
            getHref={(p) => `/browsers/profiles/${encodeURIComponent(p.id)}`}
            sort={sort}
            dir={dir}
            onSort={(key, d) => setParams(sortPatch(key, d, "name", "asc", PROFILE_NATURAL))}
            label="Profiles"
            primary
            mobileRow={(p) => (
              <>
                <div className="row-title">
                  <Link to={`/browsers/profiles/${encodeURIComponent(p.id)}`}>{profileName(p)}</Link>
                  <StateText status={p.status} statusText={p.statusText} lease={p.lease} />
                </div>
                {p.session || p.lease ? (
                  <div className="row-meta">
                    {p.session ? <span>{p.session.name}</span> : null}
                    {p.lease ? <Mono>{p.lease.sid}</Mono> : null}
                  </div>
                ) : null}
                <div className="row-meta">
                  <span>Queue {p.queueLength}</span>
                </div>
              </>
            )}
            empty={
              <EmptyState action={auth.isAdmin ? <Link to="/settings/browser" className="link">Settings</Link> : undefined}>
                No browser profiles. Set browser.instances in Settings {">"} Browser.
              </EmptyState>
            }
          />
        ) : (
          <DataTable rows={processes} columns={processColumns} getId={(b) => String(b.index)} label="Browser processes" primary empty={<EmptyState>No browser processes are running.</EmptyState>} />
        ))}
      {action ? (
        action.target.kind === "browser" ? (
          <BrowserStopConfirm target={action.target} process={processes.find((p) => action.target.path.endsWith(`/${p.index}`))} onClose={() => setAction(null)} />
        ) : (
          <PoolActionConfirm action={action.action} target={action.target} onClose={() => setAction(null)} />
        )
      ) : null}
    </div>
  );
}

function BrowserStopConfirm({ target, process, onClose }: { target: ActionTarget; process?: BrowserProcessOut; onClose: () => void }) {
  const client = useQueryClient();
  const leased = process?.profiles.filter((p) => p.lease) ?? [];
  const names = Array.from(new Set(leased.map((p) => p.session?.name ?? p.lease?.sid ?? ""))).filter(Boolean);
  return (
    <ConfirmDialog
      title={`Stop ${target.name}?`}
      body={leased.length ? `This closes ${plural(leased.length, "profile")} in use by ${names.join(" and ")}. Their agents' next commands fail.` : "No profile is in use."}
      confirmLabel="Stop browser"
      busyLabel={"Stopping…"}
      onClose={onClose}
      errorFor={(err) => errorText(err, "stop", target.name)}
      onConfirm={async () => {
        await api.post(`${target.path}/shutdown`, { confirm: target.confirmKey, reason: "stopped from the UI" }, { timeoutMs: 60_000 });
        void client.invalidateQueries({ queryKey: keys.browsers });
        void client.invalidateQueries({ queryKey: keys.profiles });
        void client.invalidateQueries({ queryKey: keys.status });
      }}
    />
  );
}

export function ProfileDetailPage({ id }: { id: string }) {
  const profile = useProfile(id);
  const params = useSearchParams();
  const [action, setAction] = useState<PoolAction | null>(null);
  const [leaseMode, setLeaseMode] = useState<"release" | "break" | null>(null);
  const fallbackName = (() => {
    const parts = id.split(/[.:]/).filter((p) => /^\d+$/.test(p));
    return parts.length >= 2 ? `Browser ${parts[parts.length - 2]}.${parts[parts.length - 1]}` : id;
  })();
  const name = profile.data ? profileName(profile.data) : fallbackName;
  useTitle(`${name} - Browsers`);
  useEffect(() => {
    if (profile.data) setFocusedResource(`browser:${profile.data.browser}:${profile.data.profile}`);
    return () => setFocusedResource(null);
  }, [profile.data?.browser, profile.data?.profile]);
  const state = queryState(profile, "profile", {
    notFound: <EmptyState action={<Link to="/browsers" className="link">Browsers</Link>}>There is no profile {id}. It may have been removed from the pool.</EmptyState>,
    forbidden: <Forbidden what={name} />,
  });
  if (state) return <div className="page">{state}</div>;
  const p = profile.data as ProfileDetail;
  const running = isRunning(p.status) || Boolean(p.lease);
  const page = profilePage(p);
  const target: ActionTarget = { name, confirmKey: p.id, kind: "profile", path: `/api/profiles/${encodeSegment(p.id)}`, lease: p.lease, session: p.session };
  return (
    <>
      <DetailLayout
        title={name}
        crumbs={[{ label: "Browsers", to: "/browsers" }]}
        sentence={
          <>
            <StateText status={p.status} statusText={p.statusText} lease={p.lease} />
            {profileSince(p) ? <> since <AbsTime value={profileSince(p)} /></> : null}.{page ? <> Page {page}</> : null}
            {p.cdp ? <> CDP <Mono>{p.cdp}</Mono></> : null}
          </>
        }
        actions={availableActions(p.status).map((a) => (
          <Button key={a} variant="secondary" onClick={() => setAction(a)}>
            {a === "start" ? "Start" : a === "shutdown" ? "Shut down" : a === "reset" ? "Reset" : "Delete"}
          </Button>
        ))}
        live={<LiveFrame src={p.liveUrl ? localPath(p.liveUrl) : `/api/profiles/${encodeSegment(p.id)}/live`} name={name} running={running} aspect={screenAspect(p, "16 / 10")} viewers={p.liveViewers} onStart={() => setAction("start")} />}
        rail={<ToolRail sid={p.lease?.sid ?? null} platform="web" />}
        lease={<CurrentLease lease={p.lease} session={p.session} onRelease={() => setLeaseMode("release")} onBreak={() => setLeaseMode("break")} />}
        queue={p.queue}
        tab={params.tab ?? "activity"}
        basePath={`/browsers/profiles/${encodeURIComponent(id)}`}
        activity={p.activity}
        history={p.history}
        activityEmpty="No activity since the daemon started."
      />
      {action ? <PoolActionConfirm action={action} target={target} onClose={() => setAction(null)} /> : null}
      {leaseMode && p.lease ? <LeaseConfirms mode={leaseMode} lease={p.lease} session={p.session} name={name} onClose={() => setLeaseMode(null)} /> : null}
    </>
  );
}
