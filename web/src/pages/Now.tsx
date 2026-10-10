import { Link } from "@tanstack/react-router";
import { useMemo } from "react";
import { useArtifacts, useSharedSessions, useStatus } from "../api/queries";
import type { LeaseOut, SessionOut, StatusResponse } from "../api/types";
import { MetaItem, Mono, queryState, RelTime, Section, StateWord, Unseen } from "../components/Misc";
import { EmptyState } from "../components/Notice";
import { Thumb } from "../features/ArtifactBrowser";
import { formatWaiting, leaseKindLabel, plural, resourceName } from "../lib/format";
import { useMinute, useTitle } from "../lib/hooks";
import { sharedArtifactUrl, sharedSessionUrl } from "../lib/url";
import { StorageLine } from "../shell/Shell";
import { useJobs, useNodes, type JobOut, type NodeOut } from "../api/system";
import { JobList, lastSeen, NodeRack } from "../features/Jobs";
import { leaseState, leaseStateWords } from "./Session";

const RECENT = 8;
const STRIP = 8;

function shortProject(id: string): string {
  return id.slice(id.indexOf("/") + 1);
}

export function NowPage() {
  useTitle("Now");
  const sessions = useSharedSessions();
  const status = useStatus();
  const recent = useMemo(
    () => [...(sessions.data?.items ?? [])].sort((a, b) => Date.parse(b.lastActiveAt) - Date.parse(a.lastActiveAt)).slice(0, RECENT),
    [sessions.data],
  );
  const total = sessions.data?.items.length ?? 0;
  const unseen = sessions.data?.items.reduce((sum, s) => sum + s.unseenCount, 0) ?? 0;
  const state = queryState(sessions, "sessions");
  const jobs = useJobs();
  const nodes = useNodes();
  const activeJobs = (jobs.data?.items ?? []).filter((j) => j.state !== "done" && j.state !== "cancelled" && j.state !== "failed").slice(0, 8);

  return (
    <div className="page now">
      <header className="page-head">
        <div className="page-title-row">
          <h1 className="page-title" tabIndex={-1} data-page-title="">
            Now
          </h1>
        </div>
        <div className="page-meta">
          <MetaItem>{plural(total, "session")}</MetaItem>
          <MetaItem>{unseen ? <span className="danger strong">{plural(unseen, "capture")} not seen by you</span> : "everything seen"}</MetaItem>
          {status.data ? <MetaItem>{plural(status.data.leases.length, "machine")} leased</MetaItem> : null}
          {status.data?.queue.length ? <MetaItem>{status.data.queue.length} waiting</MetaItem> : null}
        </div>
      </header>
      <div className="now-grid">
        <Section title="Recent sessions" count={recent.length < total ? `${recent.length} of ${total}` : total} flush actions={<Link to="/sessions" className="link">All sessions</Link>}>
          {state ?? (recent.length ? (
            <ol className="contact-rows">
              {recent.map((session) => (
                <SessionStrip key={session.slug} session={session} />
              ))}
            </ol>
          ) : (
            <EmptyState hint={<>An agent creates one with <span className="mono">eks-harness lease acquire --project owner/name --session branch</span>.</>}>No sessions yet.</EmptyState>
          ))}
        </Section>
        <aside className="now-aside" aria-label="Machines and storage">
          <Attention status={status.data} jobs={jobs.data?.items ?? []} nodes={nodes.data?.items ?? []} />
          <MachinesInUse status={status.data} />
          <Queue status={status.data} />
          <Section title="Jobs on nodes" count={jobs.data ? activeJobs.length : null} actions={<Link to="/nodes" className="link">Nodes</Link>}>
            {queryState(jobs, "jobs") ?? <JobList jobs={activeJobs} empty="No jobs are running or queued." />}
          </Section>
          <Section title="Nodes" count={nodes.data ? nodes.data.items.length : null}>
            {queryState(nodes, "nodes") ?? <NodeRack nodes={nodes.data?.items ?? []} compact />}
          </Section>
          <Section title="Storage" className="hide-desktop">
            <StorageLine />
          </Section>
        </aside>
      </div>
    </div>
  );
}

function SessionStrip({ session }: { session: SessionOut }) {
  const artifacts = useArtifacts({ session: session.slug, limit: STRIP });
  const all = artifacts.data?.items ?? [];
  const items = session.artifactCount > STRIP ? all.slice(0, STRIP - 1) : all;
  const more = Math.max(0, session.artifactCount - items.length);
  return (
    <li className="contact-row">
      <div className="contact-head">
        <Link to={sharedSessionUrl(session.slug)} className="contact-name mono">
          {session.unseenCount ? <Unseen /> : null}
          {session.name}
        </Link>
        <span className="contact-facts">
          {session.projects.map((p) => (
            <span key={p.project.id}>{shortProject(p.project.id)}</span>
          ))}
          {session.activeLeases.length ? <span className="frame-meta-strong">{plural(session.activeLeases.length, "machine")} leased</span> : null}
          <RelTime value={session.lastActiveAt} />
        </span>
        <span className="contact-count num">{session.unseenCount ? <span className="danger strong">{session.unseenCount.toLocaleString("en-US")} new</span> : plural(session.artifactCount, "capture")}</span>
      </div>
      {items.length ? (
        <div className="contact-strip">
          {items.map((item) => (
            <Link key={item.id} to={sharedArtifactUrl(session.slug, item.id)} className="contact-frame" data-unseen={item.seen ? undefined : ""} title={item.caption || item.filename}>
              <Thumb item={item} size="tile" />
            </Link>
          ))}
          {more ? (
            <Link to={sharedSessionUrl(session.slug)} className="contact-more num">
              +{more.toLocaleString("en-US")}
            </Link>
          ) : null}
        </div>
      ) : artifacts.isPending ? (
        <div className="contact-strip contact-strip--empty" aria-hidden="true" />
      ) : (
        <p className="ink-3 contact-empty">No captures yet.</p>
      )}
    </li>
  );
}

function holderLink(lease: LeaseOut) {
  if (!lease.sessionSlug) return <Mono>{lease.sid}</Mono>;
  return (
    <Link to={sharedSessionUrl(lease.sessionSlug)} className="link-quiet mono">
      {lease.sessionName ?? lease.sessionSlug}
    </Link>
  );
}

function MachinesInUse({ status }: { status: StatusResponse | undefined }) {
  const now = useMinute();
  const leases = status?.leases ?? [];
  return (
    <Section title="Leased now" count={status ? leases.length : null} actions={<Link to="/lab" className="link">Lab</Link>}>
      {!status ? null : leases.length ? (
        <ul className="ledger-list">
          {leases.map((lease) => (
            <li key={lease.sid} className="ledger-item">
              <span className="ledger-item-main">
                <span className="strong">{resourceName(lease.resource) || leaseKindLabel(lease.kind)}</span>
                {holderLink(lease)}
              </span>
              <span className="ledger-item-side">
                <StateWord state={leaseState(lease)}>{leaseStateWords(lease, now)}</StateWord>
                <Mono>{lease.sid}</Mono>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ink-3">Nothing is leased. Every machine is free.</p>
      )}
    </Section>
  );
}

function Queue({ status }: { status: StatusResponse | undefined }) {
  const now = useMinute();
  const queue = status?.queue ?? [];
  if (!queue.length) return null;
  return (
    <Section title="Waiting" count={queue.length}>
      <ul className="ledger-list">
        {queue.map((lease, index) => (
          <li key={lease.sid} className="ledger-item">
            <span className="ledger-item-main">
              <span className="strong num">{lease.queuePosition ?? index + 1}.</span>
              <span>{leaseKindLabel(lease.kind)}</span>
              {holderLink(lease)}
            </span>
            <span className="ledger-item-side num">{lease.queuedAt ? formatWaiting((now.getTime() - Date.parse(lease.queuedAt)) / 1000) : ""}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Attention({ status, jobs, nodes }: { status: StatusResponse | undefined; jobs: JobOut[]; nodes: NodeOut[] }) {
  if (!status) return null;
  const rows: { key: string; label: string; detail: string; to: string }[] = [];
  for (const node of nodes) {
    if (node.state === "offline" && node.lastSeenAt) rows.push({ key: `node-${node.id}`, label: node.id, detail: `node offline, ${lastSeen(node)}`, to: `/nodes/${encodeURIComponent(node.id)}` });
  }
  for (const job of jobs.filter((j) => j.state === "failed").slice(0, 4)) {
    rows.push({ key: `job-${job.id}`, label: job.kind, detail: job.error?.split("\n")[0] ?? "job failed", to: job.node ? `/nodes/${encodeURIComponent(job.node)}` : "/nodes" });
  }
  for (const device of status.devices) {
    if (/fail/i.test(device.status)) rows.push({ key: device.key, label: resourceName(`${device.kind}:${device.index}`), detail: device.statusText || "failed", to: `/devices/${device.kind}/${device.index}` });
  }
  for (const backend of status.backends) {
    if (backend.status === "failed") rows.push({ key: backend.id, label: backend.id, detail: backend.error || "failed", to: `/backends/${backend.id.split("/").map(encodeURIComponent).join("/")}` });
  }
  for (const lease of status.leases) {
    if (lease.phase === "failed") rows.push({ key: lease.sid, label: resourceName(lease.resource) || lease.sid, detail: lease.error || lease.reason || "lease failed", to: `/sid/${lease.sid}` });
  }
  if (!rows.length) return null;
  return (
    <Section title="Needs attention" count={rows.length}>
      <ul className="ledger-list">
        {rows.map((row) => (
          <li key={row.key} className="ledger-item">
            <span className="ledger-item-main">
              <Link to={row.to} className="link strong">
                {row.label}
              </Link>
            </span>
            <StateWord state="failed">{row.detail}</StateWord>
          </li>
        ))}
      </ul>
    </Section>
  );
}
