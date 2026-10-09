import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError, encodeSegment } from "../api/client";
import { nodeGpuLabel, systemKeys, useJobs, useNode, useNodes, type NodeOut } from "../api/system";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { CopyField, DefList, MetaItem, Mono, PageHeader, queryState, Section, Tabs } from "../components/Misc";
import { EmptyState } from "../components/Notice";
import { Rack, StateLine } from "../components/Workbench";
import { JobList, lastSeen, NodeRack, slotCells, systemText } from "../features/Jobs";
import { useAuth } from "../lib/auth";
import { plural } from "../lib/format";
import { useTitle } from "../lib/hooks";
import { useSearchParams } from "../lib/url";
import { PluginSlot } from "../shell/plugins";

const JOB_FILTERS = [
  { key: "active", label: "Running and queued", states: "queued,assigned,running" },
  { key: "failed", label: "Failed", states: "failed" },
  { key: "all", label: "All", states: "" },
];

export function NodesPage() {
  useTitle("Nodes");
  const nodes = useNodes();
  const params = useSearchParams();
  const filter = JOB_FILTERS.find((f) => f.key === params.jobs) ?? JOB_FILTERS[0];
  const jobs = useJobs(filter.states ? { state: filter.states } : {});
  const items = nodes.data?.items ?? [];
  const online = items.filter((n) => n.online).length;
  const gpus = items.filter((n) => n.online).reduce((sum, n) => sum + (n.capabilities.gpus?.length ?? 0), 0);
  const slots = items.filter((n) => n.online).flatMap((n) => n.slots);
  const capacity = slots.reduce((sum, s) => sum + (s.workers ?? 1), 0);
  const busy = slots.reduce((sum, s) => sum + (s.busy ?? 0), 0);
  return (
    <div className="page">
      <PageHeader
        title="Nodes"
        meta={
          nodes.data ? (
            <>
              <MetaItem>
                {online} of {plural(items.length, "node")} online
              </MetaItem>
              <MetaItem>{plural(gpus, "GPU")}</MetaItem>
              <MetaItem>
                {busy} of {plural(capacity, "worker")} busy
              </MetaItem>
            </>
          ) : null
        }
      />
      <div className="stack">
        <Section title="Machines" count={nodes.data ? items.length : null}>
          {queryState(nodes, "nodes") ?? <NodeRack nodes={items} />}
        </Section>
        <Section title="Jobs" count={jobs.data ? jobs.data.items.length : null}>
          <Tabs
            label="Job filter"
            sub
            items={JOB_FILTERS.map((f) => ({ label: f.label, to: "/nodes", search: f.key === "active" ? ({} as Record<string, string>) : { jobs: f.key }, active: f.key === filter.key }))}
          />
          {queryState(jobs, "jobs") ?? <JobList jobs={jobs.data?.items ?? []} empty={filter.key === "failed" ? "No failed jobs." : "Nothing is running or queued. Renders and scene previews queue here."} />}
        </Section>
      </div>
    </div>
  );
}

export function NodePage({ id }: { id: string }) {
  useTitle(`${id} - Nodes`);
  const node = useNode(id);
  const auth = useAuth();
  const client = useQueryClient();
  const [confirm, setConfirm] = useState<"remove" | "token" | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const state = queryState(node, "node", {
    notFound: <EmptyState action={<Link to="/nodes" className="link">Nodes</Link>}>There is no node {id}.</EmptyState>,
  });
  if (state) return <div className="page">{state}</div>;
  const n = node.data as NodeOut;
  const caps = n.capabilities;
  const toggle = async () => {
    await api.patch(`/api/nodes/${encodeSegment(n.id)}`, { disabled: !n.disabled });
    void client.invalidateQueries({ queryKey: systemKeys.node(n.id) });
    void client.invalidateQueries({ queryKey: systemKeys.nodes });
  };
  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: "Nodes", to: "/nodes" }]}
        title={n.id}
        mono
        actions={
          auth.isAdmin ? (
            <div className="action-row">
              <Button onClick={() => void toggle()}>{n.disabled ? "Enable" : "Disable"}</Button>
              <Button onClick={() => setConfirm("token")}>New token</Button>
              <Button variant="danger" onClick={() => setConfirm("remove")}>
                Remove
              </Button>
            </div>
          ) : null
        }
        meta={
          <>
            <MetaItem>
              <StateLine state={n.state} detail={n.online ? null : lastSeen(n)} />
            </MetaItem>
            {n.label ? <MetaItem>{n.label}</MetaItem> : null}
            {n.version ? <MetaItem>version <Mono>{n.version}</Mono></MetaItem> : null}
          </>
        }
      />
      {token ? (
        <Section title="New token">
          <p className="ink-2">Shown once. On the node: <span className="mono">eks-harness node configure --hub URL --token-stdin</span></p>
          <CopyField value={token} />
        </Section>
      ) : null}
      <div className="desk">
        <div className="desk-main">
          <Section title="Slots" count={n.slots.length}>
            <Rack
              rows={n.slots}
              rowKey={(s) => s.id}
              label="Slots"
              columns={[
                { key: "id", label: "Slot", width: "minmax(0, 0.6fr)", render: (s) => <Mono>{s.id}</Mono> },
                { key: "backend", label: "Device", width: "minmax(0, 0.7fr)", render: (s) => s.backend ?? (s.tags ?? []).join(", ") },
                { key: "use", label: "Busy", width: "minmax(0, 0.8fr)", render: (s) => `${s.busy ?? 0} of ${s.workers ?? 1}` },
                { key: "env", label: "Environment", width: "minmax(0, 1.4fr)", render: (s) => <Mono>{Object.entries(s.env ?? {}).map(([k, v]) => `${k}=${v}`).join(" ")}</Mono> },
              ]}
              empty={<span>The node reports no slots while it is offline.</span>}
            />
          </Section>
          <Section title="Recent jobs" count={n.jobs?.length ?? 0}>
            <JobList jobs={n.jobs ?? []} empty="This node has not run a job yet." />
          </Section>
          <PluginSlot slot="node.inspector" props={{ node: n as unknown as Record<string, unknown> }} />
        </div>
        <aside className="desk-side">
          <Section title="Capabilities">
            <DefList
              compact
              items={[
                { label: "System", value: systemText(n) || "unknown" },
                { label: "Host", value: caps.hostname ? <Mono>{caps.hostname}</Mono> : null },
                { label: "GPUs", value: nodeGpuLabel(n) },
                caps.blender ? { label: "Blender", value: <>{caps.blender.version} <Mono>{caps.blender.path}</Mono></> } : null,
                caps.diskFreeGb !== undefined ? { label: "Disk", value: `${caps.diskFreeGb} GB free of ${caps.diskTotalGb} GB` } : null,
                caps.jobKinds?.length ? { label: "Job kinds", value: <Mono>{caps.jobKinds.join(", ")}</Mono> } : null,
                { label: "Slots", value: n.slots.length ? slotCells(n) : "none" },
                n.tokenPrefix ? { label: "Token", value: <Mono>ehn_{n.tokenPrefix}_...</Mono> } : null,
              ]}
            />
          </Section>
          {(caps.gpus ?? []).length ? (
            <Section title="GPUs" count={caps.gpus?.length}>
              <Rack
                rows={caps.gpus ?? []}
                rowKey={(g) => String(g.index)}
                label="GPUs"
                columns={[
                  { key: "i", label: "#", width: "32px", render: (g) => <span className="num">{g.index}</span> },
                  { key: "name", label: "Model", width: "minmax(0, 1fr)", render: (g) => g.name },
                  { key: "mem", label: "Memory", width: "80px", align: "end", render: (g) => (g.memoryMb ? `${Math.round(g.memoryMb / 1024)} GB` : "") },
                  { key: "b", label: "Backends", width: "minmax(0, 0.8fr)", render: (g) => (g.backends ?? []).join(", ") },
                ]}
              />
            </Section>
          ) : null}
        </aside>
      </div>
      {confirm === "remove" ? (
        <ConfirmDialog
          title={`Remove node ${n.id}`}
          body={<p>Its queued and running jobs go back to the queue. The node can be added again with eks-harness node add.</p>}
          confirmLabel="Remove node"
          onClose={() => setConfirm(null)}
          onConfirm={async () => {
            await api.delete(`/api/nodes/${encodeSegment(n.id)}`);
            void client.invalidateQueries({ queryKey: systemKeys.nodes });
            window.history.back();
          }}
        />
      ) : null}
      {confirm === "token" ? (
        <ConfirmDialog
          title={`New token for ${n.id}`}
          body={<p>The old token stops working at once; the node disconnects until it is configured with the new one.</p>}
          confirmLabel="Issue a new token"
          onClose={() => setConfirm(null)}
          onConfirm={async () => {
            try {
              const data = await api.post<{ token: string }>(`/api/nodes/${encodeSegment(n.id)}/token`);
              setToken(data.token);
            } catch (error) {
              if (error instanceof ApiError) throw error;
            }
          }}
        />
      ) : null}
    </div>
  );
}
