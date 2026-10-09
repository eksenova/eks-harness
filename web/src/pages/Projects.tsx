import { EvidenceTabs } from "../features/EvidenceTabs";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useProjects, useStatus } from "../api/queries";
import type { ProjectOut } from "../api/types";
import { Button } from "../components/Button";
import { Select, SearchInput } from "../components/Form";
import { OverflowMenu, separator, type MenuItem } from "../components/Menu";
import { PageHeader, queryState, RelTime } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { DataTable, sortPatch, sortRows, useSort, type Column, type Natural } from "../components/Table";
import { DeleteProjectConfirm, ProjectFormDialog } from "../features/projectDialogs";
import { UploadDialog, type PickedFile } from "../features/UploadDialog";
import { DropTarget } from "../features/DropTarget";
import { canEdit, useAuth } from "../lib/auth";
import { formatCount, formatSize, plural } from "../lib/format";
import { useIsSmall, useTitle } from "../lib/hooks";
import { projectUrl, useSearchParams, useSetParams, useSyncedText } from "../lib/url";

const NATURAL: Natural = { name: "asc", title: "asc", sessions: "desc", artifacts: "desc", unseen: "desc", activity: "desc" };

export function ProjectsPage() {
  useTitle("Projects");
  const auth = useAuth();
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const small = useIsSmall();
  const projects = useProjects();
  const status = useStatus();
  const [filter, setFilter] = useSyncedText("q", 150);
  const [editing, setEditing] = useState<ProjectOut | null>(null);
  const [deleting, setDeleting] = useState<ProjectOut | null>(null);
  const [dropped, setDropped] = useState<{ files: PickedFile[]; folder: boolean } | null>(null);
  const { sort, dir } = useSort(params, "activity", "desc", NATURAL);

  const rows = useMemo(() => {
    const needle = (params.q ?? "").toLowerCase();
    const list = (projects.data?.items ?? []).filter((p) => !needle || p.id.toLowerCase().includes(needle) || p.title.toLowerCase().includes(needle));
    const accessors: Record<string, (p: ProjectOut) => unknown> = {
      name: (p) => p.id,
      title: (p) => p.title,
      sessions: (p) => p.sessionCount,
      artifacts: (p) => p.artifactCount,
      unseen: (p) => p.unseenCount,
      activity: (p) => (p.lastActivityAt ? Date.parse(p.lastActivityAt) : 0),
    };
    return sortRows(list, accessors[sort] ?? accessors.activity, dir);
  }, [projects.data, params.q, sort, dir]);

  const onSort = (key: string, next: "asc" | "desc") => setParams(sortPatch(key, next, "activity", "desc", NATURAL));

  const menu = (project: ProjectOut) => {
    const items: MenuItem[] = [{ label: "Open", onSelect: () => void navigate({ to: projectUrl(project.id) }) }];
    if (canEdit(project.access, auth)) {
      items.push({ label: "Edit…", onSelect: () => setEditing(project) });
      return [...items, separator(), { label: "Delete…", danger: true, onSelect: () => setDeleting(project) }];
    }
    return items;
  };

  const columns: Column<ProjectOut>[] = [
    { key: "id", header: "Project", sortKey: "name", naturalDir: "asc", render: (p) => <Link to={projectUrl(p.id)} className="link-quiet mono strong">{p.id}</Link>, title: (p) => p.id },
    { key: "title", header: "Title", sortKey: "title", naturalDir: "asc", render: (p) => (p.title ? p.title : p.implicit ? <span className="muted">Created automatically</span> : ""), title: (p) => p.title },
    { key: "sessions", header: "Sessions", align: "right", sortKey: "sessions", width: "96px", render: (p) => formatCount(p.sessionCount) },
    { key: "artifacts", header: "Artifacts", align: "right", sortKey: "artifacts", width: "96px", render: (p) => formatCount(p.artifactCount) },
    { key: "unseen", header: "Unseen", align: "right", sortKey: "unseen", width: "80px", render: (p) => (p.unseenCount > 0 ? <span className="strong">{formatCount(p.unseenCount)}</span> : <span className="muted">0</span>) },
    { key: "activity", header: "Last activity", align: "right", sortKey: "activity", width: "128px", render: (p) => <RelTime value={p.lastActivityAt ?? p.updatedAt} /> },
    { key: "more", header: <span className="visually-hidden">Actions</span>, width: "var(--h-control)", render: (p) => <OverflowMenu label={`More actions for ${p.id}`} items={menu(p)} /> },
  ];

  const storage = status.data?.storage;
  const state = queryState(projects, "projects");

  return (
    <DropTarget label="a project" enabled onDrop={(files, folder) => setDropped({ files, folder })}>
      <div className="page">
        <PageHeader
          title="Evidence"
          actions={
            <>
              <Button onClick={() => setParams({ upload: "1" })}>Upload</Button>
              <Button variant="primary" onClick={() => setParams({ new: "project" })}>
                New project
              </Button>
            </>
          }
        />
        <EvidenceTabs active="projects" />
        {storage?.overQuota && storage.quotaBytes ? (
          <Notice
            variant="attention"
            title={`Storage is at ${formatSize(storage.usedBytes)}, over the ${formatSize(storage.quotaBytes)} quota.`}
            action={auth.isAdmin ? <Link to="/settings/storage" className="link">Settings</Link> : undefined}
          >
            {auth.isAdmin ? "Delete or unpin old artifacts, or raise storage.quotaGb." : "Ask an admin to raise the quota."}
          </Notice>
        ) : null}
        <div className="toolbar page-tools">
          <SearchInput value={filter} onValueChange={setFilter} label="Filter projects" placeholder="Filter projects" className="page-search" />
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
              <option value="name:asc">Sort by: Project</option>
              <option value="title:asc">Sort by: Title</option>
              <option value="sessions:desc">Sort by: Sessions</option>
              <option value="artifacts:desc">Sort by: Artifacts</option>
              <option value="unseen:desc">Sort by: Unseen</option>
            </Select>
          ) : null}
        </div>
        {state ?? (
          <DataTable
            rows={rows}
            columns={columns}
            getId={(p) => p.id}
            getHref={(p) => projectUrl(p.id)}
            sort={sort}
            dir={dir}
            onSort={onSort}
            label="Projects"
            primary
            mobileRow={(p) => (
              <>
                <div className="row-title">
                  <Link to={projectUrl(p.id)} className={p.unseenCount ? "strong" : ""}>
                    {p.id}
                  </Link>
                  <span className={p.unseenCount ? "strong" : "muted"} aria-label={`${p.unseenCount} unseen`}>
                    {formatCount(p.unseenCount)}
                  </span>
                </div>
                <div className="row-meta">{p.title || (p.implicit ? "Created automatically" : "")}</div>
                <div className="row-meta">
                  <span>{plural(p.sessionCount, "session")}</span>
                  <span>{plural(p.artifactCount, "artifact")}</span>
                  <RelTime value={p.lastActivityAt ?? p.updatedAt} />
                </div>
              </>
            )}
            empty={
              params.q ? (
                <EmptyState action={<Button onClick={() => { setFilter(""); setParams({ q: null }); }}>Clear filter</Button>}>No projects match "{params.q}".</EmptyState>
              ) : (
                <EmptyState action={<Button onClick={() => setParams({ new: "project" })}>New project</Button>}>
                  No projects yet. Projects are created when an agent acquires a lease, or you can create one.
                </EmptyState>
              )
            }
          />
        )}
        {params.new === "project" ? <ProjectFormDialog onClose={() => setParams({ new: null })} /> : null}
        {editing ? <ProjectFormDialog project={editing} onClose={() => setEditing(null)} /> : null}
        {deleting ? <DeleteProjectConfirm project={deleting} onClose={() => setDeleting(null)} /> : null}
        {params.upload === "1" ? <UploadDialog onClose={() => setParams({ upload: null })} /> : null}
        {dropped ? <UploadDialog initialFiles={dropped.files} initialSite={dropped.folder} onClose={() => setDropped(null)} /> : null}
      </div>
    </DropTarget>
  );
}
