import { EvidenceTabs } from "../features/EvidenceTabs";
import { Link } from "@tanstack/react-router";
import { Fragment, useMemo } from "react";
import { useProjects, useSharedSessions } from "../api/queries";
import type { SessionOut } from "../api/types";
import { Button } from "../components/Button";
import { SearchInput, Select } from "../components/Form";
import { Mono, PageHeader, queryState, RelTime } from "../components/Misc";
import { EmptyState } from "../components/Notice";
import { DataTable, sortPatch, sortRows, useSort, type Column, type Natural } from "../components/Table";
import { formatCount, plural } from "../lib/format";
import { useIsSmall, useTitle } from "../lib/hooks";
import { localPath, sessionUrl, sharedSessionUrl, useSearchParams, useSetParams, useSyncedText } from "../lib/url";

const NATURAL: Natural = { name: "asc", projects: "desc", artifacts: "desc", unseen: "desc", activity: "desc" };

function ProjectLinks({ session }: { session: SessionOut }) {
  if (!session.projects.length) return <span className="muted">None</span>;
  return (
    <>
      {session.projects.map((p, index) => (
        <Fragment key={p.project.id}>
          {index ? ", " : ""}
          <Link to={localPath(p.url) || sessionUrl(p.project.id, session.slug)} className="link" title={`${p.project.id}: ${plural(p.artifactCount, "artifact")}`}>
            {p.project.id}
          </Link>
        </Fragment>
      ))}
    </>
  );
}

export function SessionsPage() {
  useTitle("Sessions");
  const params = useSearchParams();
  const setParams = useSetParams();
  const small = useIsSmall();
  const sessions = useSharedSessions();
  const projects = useProjects();
  const [filter, setFilter] = useSyncedText("q", 150);
  const { sort, dir } = useSort(params, "activity", "desc", NATURAL);

  const rows = useMemo(() => {
    const needle = (params.q ?? "").toLowerCase();
    const list = (sessions.data?.items ?? []).filter(
      (s) => (!needle || s.name.toLowerCase().includes(needle) || s.slug.includes(needle)) && (!params.project || s.projectIds.includes(params.project)),
    );
    const accessors: Record<string, (s: SessionOut) => unknown> = {
      name: (s) => s.name.toLowerCase(),
      projects: (s) => s.projects.length,
      artifacts: (s) => s.artifactCount,
      unseen: (s) => s.unseenCount,
      activity: (s) => Date.parse(s.lastActiveAt),
    };
    return sortRows(list, accessors[sort] ?? accessors.activity, dir);
  }, [sessions.data, params.q, params.project, sort, dir]);

  const onSort = (key: string, next: "asc" | "desc") => setParams(sortPatch(key, next, "activity", "desc", NATURAL));

  const columns: Column<SessionOut>[] = [
    { key: "name", header: "Session", sortKey: "name", naturalDir: "asc", render: (s) => <Link to={sharedSessionUrl(s.slug)} className={`link-quiet ${s.unseenCount ? "strong" : ""}`}>{s.name}</Link>, title: (s) => s.name },
    { key: "projects", header: "Projects", sortKey: "projects", render: (s) => <ProjectLinks session={s} />, title: (s) => s.projectIds.join(", ") },
    { key: "leases", header: "Live leases", width: "136px", render: (s) => (s.activeLeases.length ? s.activeLeases.map((l) => <Mono key={l.sid}>{l.sid} </Mono>) : <span className="muted">None</span>) },
    { key: "artifacts", header: "Artifacts", align: "right", sortKey: "artifacts", width: "96px", render: (s) => formatCount(s.artifactCount) },
    { key: "unseen", header: "Unseen", align: "right", sortKey: "unseen", width: "80px", render: (s) => (s.unseenCount > 0 ? <span className="strong">{formatCount(s.unseenCount)}</span> : <span className="muted">0</span>) },
    { key: "activity", header: "Last activity", align: "right", sortKey: "activity", width: "128px", render: (s) => <RelTime value={s.lastActiveAt} /> },
  ];

  const projectOptions = projects.data?.items.map((p) => p.id) ?? [];
  const state = queryState(sessions, "sessions");
  const filtered = Boolean(params.q || params.project);

  return (
    <div className="page">
      <PageHeader title="Evidence" />
      <EvidenceTabs active="sessions" />
      <div className="toolbar page-tools">
        <SearchInput value={filter} onValueChange={setFilter} label="Filter sessions" placeholder="Filter sessions" className="page-search" />
        <Select aria-label="Project" value={params.project ?? ""} onChange={(event) => setParams({ project: event.target.value || null })}>
          <option value="">All projects</option>
          {projectOptions.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </Select>
        {small ? (
          <Select
            aria-label="Sort by"
            value={`${sort}:${dir}`}
            onChange={(event) => {
              const [key, d] = event.target.value.split(":");
              onSort(key, d as "asc" | "desc");
            }}
          >
            <option value="activity:desc">Sort by: Last activity</option>
            <option value="name:asc">Sort by: Session</option>
            <option value="projects:desc">Sort by: Projects</option>
            <option value="artifacts:desc">Sort by: Artifacts</option>
            <option value="unseen:desc">Sort by: Unseen</option>
          </Select>
        ) : null}
      </div>
      {state ?? (
        <DataTable
          rows={rows}
          columns={columns}
          getId={(s) => String(s.id)}
          getHref={(s) => sharedSessionUrl(s.slug)}
          sort={sort}
          dir={dir}
          onSort={onSort}
          label="Sessions"
          primary
          mobileRow={(s) => (
            <>
              <div className="row-title">
                <Link to={sharedSessionUrl(s.slug)} className={s.unseenCount ? "strong" : ""}>
                  {s.name}
                </Link>
                <span className={s.unseenCount ? "strong" : "muted"} aria-label={`${s.unseenCount} unseen`}>
                  {formatCount(s.unseenCount)}
                </span>
              </div>
              <div className="row-meta">{s.projectIds.join(", ") || "No project"}</div>
              <div className="row-meta">
                <span>{plural(s.artifactCount, "artifact")}</span>
                {s.activeLeases.length ? <span>{plural(s.activeLeases.length, "live lease")}</span> : null}
                <RelTime value={s.lastActiveAt} />
              </div>
            </>
          )}
          empty={
            filtered ? (
              <EmptyState
                action={
                  <Button
                    onClick={() => {
                      setFilter("");
                      setParams({ q: null, project: null });
                    }}
                  >
                    Clear filters
                  </Button>
                }
              >
                No sessions match these filters.
              </EmptyState>
            ) : (
              <EmptyState>No sessions yet. A session is created when an agent acquires a lease or uploads to one.</EmptyState>
            )
          }
        />
      )}
    </div>
  );
}
