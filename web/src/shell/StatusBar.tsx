import { Link } from "@tanstack/react-router";
import { useProjects, useStatus } from "../api/queries";
import { useJobs, useLiveSessions, useNodes, useRenderQueue } from "../api/system";
import { Icon } from "../components/Icon";
import { OverflowMenu, separator, type MenuItem } from "../components/Menu";
import { StateBar } from "../components/Workbench";
import { formatSize, plural } from "../lib/format";
import { useConnection } from "../lib/events";
import { useCurrentProject } from "./project";

export function StatusBar() {
  const status = useStatus();
  const nodes = useNodes();
  const jobs = useJobs({ state: "queued,assigned,running" });
  const live = useLiveSessions();
  const renders = useRenderQueue();
  const connection = useConnection();
  const leases = status.data?.leases.length ?? 0;
  const queued = status.data?.queue.length ?? 0;
  const online = nodes.data?.items.filter((n) => n.online).length ?? 0;
  const total = nodes.data?.items.length ?? 0;
  const running = jobs.data?.items.filter((j) => j.state === "running" || j.state === "assigned").length ?? 0;
  const waiting = jobs.data?.items.filter((j) => j.state === "queued").length ?? 0;
  const playing = live.data?.items.filter((s) => s.state === "playing").length ?? 0;
  const storage = status.data?.storage;
  const down = connection.state === "disconnected" || connection.state === "reconnecting";
  return (
    <footer className="statusbar" aria-label="Hub status">
      <span>
        <StateBar tone={down ? "fail" : "ok"} /> {down ? <span className="statusbar-alert">Hub unreachable</span> : "Hub connected"}
      </span>
      <Link to="/lab">
        <StateBar tone={leases ? "ok" : "idle"} />
        {plural(leases, "lease")}
        {queued ? `, ${queued} queued` : ""}
      </Link>
      <Link to="/nodes">
        <StateBar tone={total && online < total ? "fail" : online ? "ok" : "idle"} />
        {total ? `${online} of ${plural(total, "node")} online` : "no nodes"}
      </Link>
      <Link to="/nodes">
        <StateBar tone={running ? "busy" : waiting ? "wait" : "idle"} />
        {running ? plural(running, "job") + " running" : "no jobs running"}
        {waiting ? `, ${waiting} queued` : ""}
      </Link>
      {renders.data && (renders.data.running || renders.data.waiting) ? (
        <span title={renders.data.items.map((t) => `${t.state === "running" ? "rendering" : `#${t.position} waiting`}: ${t.label}${t.session ? ` (${t.session})` : ""}`).join("\n")}>
          <StateBar tone={renders.data.running ? "busy" : "wait"} />
          {renders.data.running ? `${plural(renders.data.running, "render")} running` : "no render running"}
          {renders.data.waiting ? `, ${renders.data.waiting} waiting` : ""}
        </span>
      ) : null}
      {playing ? (
        <Link to="/studio">
          <StateBar tone="busy" />
          {plural(playing, "score")} playing live
        </Link>
      ) : null}
      <span className="statusbar-fill" />
      {storage ? <span>Store {formatSize(storage.usedBytes)}{storage.quotaBytes ? ` of ${formatSize(storage.quotaBytes)}` : ""}</span> : null}
      {status.data ? <span className="mono">{status.data.version}</span> : null}
    </footer>
  );
}

export function ProjectSwitcher() {
  const projects = useProjects();
  const [project, setProject] = useCurrentProject();
  const items: MenuItem[] = [
    { label: "Current project", kind: "text" },
    ...(projects.data?.items ?? []).map<MenuItem>((item) => ({ label: item.id, kind: "radio", checked: item.id === project, onSelect: () => setProject(item.id) })),
    separator(),
    { label: "No current project", kind: "radio", checked: !project, onSelect: () => setProject(null) },
  ];
  return (
    <OverflowMenu
      items={items}
      label="Current project"
      trigger={({ ref, onClick, expanded }) => (
        <button ref={ref} type="button" className="bar-project" aria-haspopup="menu" aria-expanded={expanded} onClick={onClick} title="Current project: plugin pages, studio scores and new captures use it">
          <span>{project ?? "no project"}</span>
          <Icon name="chevron-down" size={12} />
        </button>
      )}
    />
  );
}
