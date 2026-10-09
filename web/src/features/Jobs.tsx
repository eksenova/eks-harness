import { Link } from "@tanstack/react-router";
import { jobDuration, nodeGpuLabel, type JobOut, type NodeOut } from "../api/system";
import { Mono } from "../components/Misc";
import { Rack, RunBlock, StateLine, type Tone } from "../components/Workbench";
import { formatSeconds, formatSize } from "../lib/format";
import { useSecondTicker } from "../lib/hooks";

const JOB_TONE: Record<JobOut["state"], Tone> = { queued: "wait", assigned: "busy", running: "busy", done: "ok", failed: "fail", cancelled: "idle" };

function jobTitle(job: JobOut): string {
  const payload = job.payload as Record<string, unknown>;
  const subject = (payload.scene ?? payload.project ?? payload.name ?? payload.argv) as unknown;
  const frames = payload.frames as number[] | undefined;
  const range = Array.isArray(frames) && frames.length === 2 ? ` f${frames[0]}-${frames[1]}` : "";
  return `${job.kind}${subject ? ` ${Array.isArray(subject) ? subject.join(" ") : String(subject)}` : ""}${range}`;
}

function jobWord(job: JobOut): string {
  if (job.state === "queued" && job.attempts > 0) return `queued, retry ${job.attempts + 1}`;
  if (job.state === "running" && job.progress) return `running ${Math.round(job.progress * 100)}%`;
  return job.state;
}

export function JobBlock({ job, now }: { job: JobOut; now: number }) {
  const duration = jobDuration(job, now / 1000);
  const outputs = job.result?.outputs ?? [];
  const facts = [
    job.node ? (
      <Link key="node" to={`/nodes/${encodeURIComponent(job.node)}`} className="link-quiet mono">
        {job.node}
        {job.slot ? `/${job.slot}` : ""}
      </Link>
    ) : job.state === "queued" ? (
      <span key="node">{needs(job)}</span>
    ) : null,
    duration !== null ? <span key="t" className="num">{formatSeconds(duration)}</span> : null,
    job.message && job.state !== "failed" ? <span key="m">{job.message}</span> : null,
    outputs.length ? <span key="o">{outputs.length} outputs</span> : null,
  ].filter(Boolean);
  const body = job.error || outputs.length || job.result?.data ? (
    <>
      {job.error ? <pre className="danger">{job.error}</pre> : null}
      {outputs.length ? (
        <ul className="plain-list">
          {outputs.map((output) => (
            <li key={output.hash}>
              <a href={`/api/blobs/${output.hash}`} className="link mono" download={output.name}>
                {output.name}
              </a>{" "}
              <span className="ink-3 num">{formatSize(output.size)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {job.result?.data ? <pre>{JSON.stringify(job.result.data, null, 2)}</pre> : null}
      <p className="ink-3">
        Job <Mono>{job.id}</Mono>, attempt {job.attempts}, by {job.owner ?? "unknown"}
      </p>
    </>
  ) : null;
  return (
    <RunBlock title={jobTitle(job)} state={jobWord(job)} tone={JOB_TONE[job.state]} progress={job.state === "running" ? job.progress ?? 0.02 : null} facts={facts as React.ReactNode[]}>
      {body}
    </RunBlock>
  );
}

function needs(job: JobOut): string {
  const req = job.requirements as { gpu?: boolean; capabilities?: string[]; node?: string };
  const parts = [req.node ? `node ${req.node}` : null, req.gpu ? "a GPU slot" : null, ...(req.capabilities ?? [])].filter(Boolean);
  return parts.length ? `needs ${parts.join(", ")}` : "waiting for a slot";
}

export function JobList({ jobs, empty }: { jobs: JobOut[]; empty?: React.ReactNode }) {
  const live = jobs.some((j) => j.state === "running" || j.state === "assigned");
  const now = useSecondTicker(live);
  if (!jobs.length) return <p className="ink-2 empty-line">{empty ?? "No jobs."}</p>;
  return (
    <div className="run-list">
      {jobs.map((job) => (
        <JobBlock key={job.id} job={job} now={now || Date.now()} />
      ))}
    </div>
  );
}

export function slotCells(node: NodeOut) {
  return (
    <span className="slot-cells">
      {node.slots.map((slot) => {
        const capacity = slot.workers ?? 1;
        const busy = slot.busy ?? 0;
        return (
          <span key={slot.id} className="slot-cell" title={`${slot.id}: ${busy} of ${capacity} busy${slot.backend ? `, ${slot.backend}` : ""}`}>
            <span className="slot-cell-name mono">{slot.id}</span>
            <span className="meter-cells" aria-label={`${busy} of ${capacity}`}>
              {Array.from({ length: capacity }, (_, i) => (
                <i key={i} data-on={i < busy ? "" : undefined} />
              ))}
            </span>
          </span>
        );
      })}
    </span>
  );
}

export function NodeRack({ nodes, compact = false }: { nodes: NodeOut[]; compact?: boolean }) {
  return (
    <Rack
      rows={nodes}
      rowKey={(n) => n.id}
      rowTo={(n) => `/nodes/${encodeURIComponent(n.id)}`}
      label="Nodes"
      columns={
        compact
          ? [
              { key: "id", label: "Node", width: "minmax(0, 0.8fr)", render: (n) => <Mono>{n.id}</Mono> },
              { key: "state", label: "State", width: "minmax(0, 0.7fr)", render: (n) => <StateLine state={n.state} /> },
              { key: "slots", label: "Slots", width: "minmax(0, 1.4fr)", render: (n) => (n.online ? slotCells(n) : <span className="ink-3">{lastSeen(n)}</span>) },
            ]
          : [
              { key: "id", label: "Node", width: "minmax(120px, 0.8fr)", render: (n) => <Mono>{n.id}</Mono> },
              { key: "state", label: "State", width: "minmax(120px, 0.7fr)", render: (n) => <StateLine state={n.state} detail={n.online ? null : lastSeen(n)} /> },
              { key: "system", label: "System", width: "minmax(0, 0.9fr)", render: (n) => systemText(n) },
              { key: "gpus", label: "GPUs", width: "minmax(0, 1.1fr)", render: (n) => nodeGpuLabel(n) },
              { key: "slots", label: "Slots busy", width: "minmax(0, 1.6fr)", render: (n) => (n.slots.length ? slotCells(n) : <span className="ink-3">none reported</span>) },
              { key: "jobs", label: "Jobs", width: "56px", align: "end", render: (n) => <span className="num">{n.running.length}</span> },
            ]
      }
      empty={
        <span>
          No nodes. Add one over SSH with <span className="mono">eks-harness node add user@host</span>.
        </span>
      }
    />
  );
}

export function systemText(node: NodeOut): string {
  const caps = node.capabilities;
  if (!caps.os) return "";
  const parts = [`${caps.os}${caps.wsl ? " (WSL)" : ""}`, caps.cpus ? `${caps.cpus} CPUs` : null, caps.memoryMb ? `${Math.round(caps.memoryMb / 1024)} GB` : null];
  return parts.filter(Boolean).join(", ");
}

export function lastSeen(node: NodeOut): string {
  if (!node.lastSeenAt) return "never connected";
  const seconds = Date.now() / 1000 - node.lastSeenAt;
  if (seconds < 90) return "seen just now";
  if (seconds < 3600) return `seen ${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `seen ${Math.round(seconds / 3600)} h ago`;
  return `seen ${Math.round(seconds / 86400)} days ago`;
}
