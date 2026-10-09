import { PluginSlot } from "../shell/plugins";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useState } from "react";
import { api, ApiError, errorText, projectPath } from "../api/client";
import { keys, useGrants, useProject, useSessions, useUsers } from "../api/queries";
import type { GrantOut, ProjectOut, SessionOut } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Field, NumberInput, Radio, Select, SearchInput, Textarea, TextInput } from "../components/Form";
import { OverflowMenu, separator } from "../components/Menu";
import { AbsTime, Combobox, Forbidden, Mono, PageHeader, queryState, RelTime, Tabs } from "../components/Misc";
import { EmptyState, Notice, ResultText } from "../components/Notice";
import { DataTable, sortPatch, sortRows, useSort, type Column, type Natural } from "../components/Table";
import { ArtifactBrowser } from "../features/ArtifactBrowser";
import { setSessionSeen } from "../features/artifactActions";
import { DropTarget } from "../features/DropTarget";
import { DeleteProjectConfirm, DeleteSessionsConfirm, ProjectFormDialog, RenameSessionDialog } from "../features/projectDialogs";
import { UploadDialog, type PickedFile } from "../features/UploadDialog";
import { canEdit, useAuth } from "../lib/auth";
import { formatCount, formatSize, formatDate, plural, resourceName } from "../lib/format";
import { nowTime, useCopy, useTitle } from "../lib/hooks";
import { absoluteUrl, projectUrl, sessionUrl, useSearchParams, useSetParams, useSyncedText, sharedSessionUrl } from "../lib/url";

export type ProjectTab = "sessions" | "files" | "settings" | "access";

const NATURAL: Natural = { name: "asc", artifacts: "desc", unseen: "desc", activity: "desc" };

export function ProjectPage({ owner, name, tab }: { owner: string; name: string; tab: ProjectTab }) {
  const projectId = `${owner}/${name}`;
  const auth = useAuth();
  const project = useProject(projectId);
  const params = useSearchParams();
  const setParams = useSetParams();
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [dropped, setDropped] = useState<{ files: PickedFile[]; folder: boolean } | null>(null);
  const [copied, copy] = useCopy();
  useTitle(projectId);

  const state = queryState(project, "project", {
    notFound: (
      <EmptyState action={<Link to="/projects" className="link">Projects</Link>}>There is no project {projectId}. It may have been deleted.</EmptyState>
    ),
    forbidden: <Forbidden what={projectId} />,
  });
  if (state) return <div className="page">{state}</div>;
  const data = project.data!;
  const editable = canEdit(data.access, auth);
  const base = projectUrl(projectId);

  const tabs = [
    { label: "Sessions", to: base, count: data.sessionCount, active: tab === "sessions" },
    { label: "Project files", to: `${base}/files`, count: data.projectFileCount, active: tab === "files" },
    { label: "Settings", to: `${base}/settings`, active: tab === "settings" },
    ...(auth.isAdmin ? [{ label: "Access", to: `${base}/access`, active: tab === "access" }] : []),
  ];

  const menuItems = [
    { label: copied ? "Copied" : "Copy project link", onSelect: () => void copy(absoluteUrl(base)) },
    ...(editable ? [{ label: "Edit…", onSelect: () => setEditing(true) }, separator(), { label: "Delete project…", danger: true, onSelect: () => setDeleting(true) }] : []),
  ];

  const meta = data.implicit && !data.title ? (
    <p className="muted">
      Created automatically on {formatDate(data.createdAt)}.{" "}
      {editable ? (
        <button type="button" className="link" onClick={() => setEditing(true)}>
          Edit details
        </button>
      ) : null}
    </p>
  ) : data.title || data.description ? (
    <div className="project-meta">
      {data.title ? <p className="muted">{data.title}</p> : null}
      {data.description ? <p className="muted prose">{data.description}</p> : null}
    </div>
  ) : null;

  return (
    <DropTarget label={projectId} enabled={editable && tab !== "settings" && tab !== "access"} onDrop={(files, folder) => setDropped({ files, folder })}>
      <div className="page">
        <PageHeader
          crumbs={[{ label: "Projects", to: "/projects" }]}
          title={projectId}
          meta={meta}
          actions={
            <>
              {editable ? <Button onClick={() => setParams({ upload: "1" })}>Upload</Button> : null}
              <OverflowMenu label={`More actions for ${projectId}`} items={menuItems} />
            </>
          }
        />
        <Tabs items={tabs} label="Project sections" />
        {tab === "sessions" ? <SessionsTab project={data} editable={editable} onUpload={() => setParams({ upload: "1" })} /> : null}
        {tab === "sessions" ? <PluginSlot slot="project.tab" props={{ project: projectId }} /> : null}
        {tab === "files" ? (
          <ArtifactBrowser
            projectId={projectId}
            sessionSlug={null}
            sessionName={null}
            canEdit={editable}
            onUpload={() => setParams({ upload: "1" })}
            uploadLabel="Upload"
            emptyText="No project-level files. Upload files that belong to the project rather than a session."
          />
        ) : null}
        {tab === "settings" ? <SettingsTab project={data} editable={editable} onDelete={() => setDeleting(true)} /> : null}
        {tab === "access" && auth.isAdmin ? <AccessTab project={data} /> : null}
        {tab === "access" && !auth.isAdmin ? <Forbidden what={`the access settings of ${projectId}`} /> : null}
        {editing ? <ProjectFormDialog project={data} onClose={() => setEditing(false)} /> : null}
        {deleting ? <DeleteProjectConfirm project={data} onClose={() => setDeleting(false)} /> : null}
        {params.upload === "1" ? <UploadDialog projectId={projectId} sessionSlug={null} onClose={() => setParams({ upload: null })} /> : null}
        {dropped ? <UploadDialog projectId={projectId} sessionSlug={null} initialFiles={dropped.files} initialSite={dropped.folder} onClose={() => setDropped(null)} /> : null}
      </div>
    </DropTarget>
  );
}

function leaseText(session: SessionOut) {
  if (!session.activeLeases.length) return <span className="muted">None</span>;
  return (
    <span>
      {session.activeLeases.map((lease, index) => (
        <span key={lease.sid}>
          {index > 0 ? ", " : ""}
          {resourceName(lease.resource) || lease.kind} <Mono>{lease.sid}</Mono>
          {lease.state === "idle" ? " (idle)" : lease.state === "queued" ? " (queued)" : ""}
        </span>
      ))}
    </span>
  );
}

function SessionsTab({ project, editable, onUpload }: { project: ProjectOut; editable: boolean; onUpload: () => void }) {
  const sessions = useSessions(project.id);
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [filter, setFilter] = useSyncedText("q", 150);
  const { sort, dir } = useSort(params, "activity", "desc", NATURAL);
  const [renaming, setRenaming] = useState<SessionOut | null>(null);
  const [deleting, setDeleting] = useState<SessionOut[] | null>(null);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, copy] = useCopy();

  const selected = useMemo(() => new Set((params.sel ?? "").split(",").filter(Boolean)), [params.sel]);
  const setSelected = (next: Set<string>) => setParams({ sel: next.size ? Array.from(next).join(",") : null }, { replace: true });

  const rows = useMemo(() => {
    const needle = (params.q ?? "").toLowerCase();
    const list = (sessions.data?.items ?? []).filter((s) => !needle || s.name.toLowerCase().includes(needle) || s.slug.includes(needle));
    const accessors: Record<string, (s: SessionOut) => unknown> = {
      name: (s) => s.name,
      artifacts: (s) => s.artifactCount,
      unseen: (s) => s.unseenCount,
      activity: (s) => Date.parse(s.lastActiveAt),
    };
    return sortRows(list, accessors[sort] ?? accessors.activity, dir);
  }, [sessions.data, params.q, sort, dir]);

  const state = queryState(sessions, "sessions");
  const selectedSessions = rows.filter((s) => selected.has(s.slug));

  const markAllSeen = async (list: SessionOut[]) => {
    setBusy(true);
    setError("");
    try {
      let total = 0;
      for (const session of list) {
        total += await setSessionSeen(client, project.id, session.slug, true);
      }
      setSelected(new Set());
      setResult(`Marked ${plural(total, "artifact")} as seen.`);
      window.setTimeout(() => setResult(""), 5000);
    } catch (err) {
      setError(errorText(err, "mark", "the sessions as seen", project.id));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<SessionOut>[] = [
    { key: "name", header: "Session", sortKey: "name", naturalDir: "asc", render: (s) => <Link to={sessionUrl(project.id, s.slug)} className={`link-quiet ${s.unseenCount ? "strong" : ""}`}>{s.name}</Link>, title: (s) => s.name },
    {
      key: "shared",
      header: "Also in",
      width: "160px",
      render: (s) => {
        const others = s.projectIds.filter((id) => id !== project.id);
        return others.length ? <Link to={sharedSessionUrl(s.slug)} className="link" title="Open the session across all its projects">{others.map((id) => id.slice(id.indexOf("/") + 1)).join(", ")}</Link> : <span className="muted">None</span>;
      },
      title: (s) => s.projectIds.filter((id) => id !== project.id).join(", "),
    },
    { key: "leases", header: "Leases now", render: leaseText },
    { key: "artifacts", header: "Artifacts", align: "right", sortKey: "artifacts", width: "96px", render: (s) => formatCount(s.artifactCount) },
    { key: "unseen", header: "Unseen", align: "right", sortKey: "unseen", width: "80px", render: (s) => (s.unseenCount ? <span className="strong">{formatCount(s.unseenCount)}</span> : <span className="muted">0</span>) },
    { key: "activity", header: "Last activity", align: "right", sortKey: "activity", width: "128px", render: (s) => <RelTime value={s.lastActiveAt} /> },
    {
      key: "more",
      header: <span className="visually-hidden">Actions</span>,
      width: "var(--h-control)",
      render: (s) => (
        <OverflowMenu
          label={`More actions for ${s.name}`}
          items={[
            { label: "Open", onSelect: () => void navigate({ to: sessionUrl(project.id, s.slug) }) },
            { label: copied === s.slug ? "Copied" : "Copy session link", onSelect: () => void copy(absoluteUrl(sessionUrl(project.id, s.slug)), s.slug) },
            ...(editable ? [{ label: "Rename…", onSelect: () => setRenaming(s) }, separator(), { label: "Delete…", danger: true, onSelect: () => setDeleting([s]) }] : []),
          ]}
        />
      ),
    },
  ];

  const toolbar = selected.size ? (
    <div className="toolbar bulk-bar" role="toolbar" aria-label="Bulk actions">
      <span className="bulk-count">{selected.size.toLocaleString("en-US")} selected</span>
      <Button onClick={() => void markAllSeen(selectedSessions)} busy={busy} busyLabel={"Marking…"}>
        Mark all seen
      </Button>
      {editable ? (
        <Button onClick={() => setDeleting(selectedSessions)}>
          Delete {plural(selected.size, "session")}
        </Button>
      ) : null}
      <button type="button" className="link bulk-clear" onClick={() => setSelected(new Set())}>
        Clear selection
      </button>
    </div>
  ) : result ? (
    <div className="toolbar">
      <span className="muted">{result}</span>
    </div>
  ) : (
    <div className="toolbar page-tools">
      <SearchInput value={filter} onValueChange={setFilter} label="Filter sessions" placeholder="Filter sessions" className="page-search" />
    </div>
  );

  return (
    <div>
      {toolbar}
      {error ? <Notice variant="error">{error}</Notice> : null}
      {state ?? (
        <DataTable
          rows={rows}
          columns={columns}
          getId={(s) => s.slug}
          getHref={(s) => sessionUrl(project.id, s.slug)}
          sort={sort}
          dir={dir}
          onSort={(key, d) => setParams(sortPatch(key, d, "activity", "desc", NATURAL))}
          selection={{
            selected,
            onToggle: (id) => {
              const next = new Set(selected);
              if (next.has(id)) next.delete(id);
              else next.add(id);
              setSelected(next);
            },
            onToggleAll: (checked) => setSelected(checked ? new Set(rows.map((r) => r.slug)) : new Set()),
            onClear: () => setSelected(new Set()),
          }}
          label="Sessions"
          primary
          onRowKey={(s, key) => {
            if (key === "x" && editable) {
              setDeleting(selected.size ? selectedSessions : [s]);
              return true;
            }
            if (key === "s") {
              void markAllSeen(selected.size ? selectedSessions : [s]);
              return true;
            }
            return false;
          }}
          mobileRow={(s) => (
            <>
              <div className="row-title">
                <Link to={sessionUrl(project.id, s.slug)} className={s.unseenCount ? "strong" : ""}>
                  {s.name}
                </Link>
                <span className={s.unseenCount ? "strong" : "muted"} aria-label={`${s.unseenCount} unseen`}>
                  {formatCount(s.unseenCount)}
                </span>
              </div>
              <div className="row-meta">{s.activeLeases.length ? <span>Leases: {leaseText(s)}</span> : <span>No active leases</span>}</div>
              <div className="row-meta">
                <span>{plural(s.artifactCount, "artifact")}</span>
                <RelTime value={s.lastActiveAt} />
              </div>
            </>
          )}
          empty={
            params.q ? (
              <EmptyState action={<Button onClick={() => setFilter("")}>Clear filters</Button>}>No sessions match "{params.q}".</EmptyState>
            ) : (
              <EmptyState action={editable ? <Button onClick={onUpload}>Upload files</Button> : undefined}>
                No sessions yet. Sessions appear when an agent acquires a lease with --project {project.id}.
              </EmptyState>
            )
          }
        />
      )}
      {renaming ? <RenameSessionDialog projectId={project.id} session={renaming} onClose={() => setRenaming(null)} /> : null}
      {deleting ? <DeleteSessionsConfirm projectId={project.id} sessions={deleting} onClose={() => setDeleting(null)} onDeleted={() => setSelected(new Set())} /> : null}
    </div>
  );
}

type RetentionMode = "global" | "forever" | "days";

function retentionMode(project: ProjectOut): RetentionMode {
  if (project.retentionDays === null) return "global";
  return project.retentionDays === 0 ? "forever" : "days";
}

function projectRetentionText(project: ProjectOut): string {
  if (project.retentionSource === "forever") return "Kept forever (project setting).";
  if (project.retentionSource === "project") return `Unpinned artifacts are deleted after ${plural(project.retentionDays ?? 0, "day")}.`;
  return project.effectiveRetentionDays
    ? `Global default: unpinned artifacts are deleted after ${plural(project.effectiveRetentionDays, "day")}.`
    : "Global default: kept forever.";
}

function SettingsTab({ project, editable, onDelete }: { project: ProjectOut; editable: boolean; onDelete: () => void }) {
  const client = useQueryClient();
  const [title, setTitle] = useState(project.title);
  const [description, setDescription] = useState(project.description);
  const [mode, setMode] = useState<RetentionMode>(retentionMode(project));
  const [days, setDays] = useState(String(project.retentionDays || 30));
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [daysError, setDaysError] = useState("");
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    setTitle(project.title);
    setDescription(project.description);
    setMode(retentionMode(project));
    setDays(String(project.retentionDays || 30));
  }, [project.id]);

  if (!editable) {
    return (
      <div className="form-grid">
        <section className="section">
          <h2 className="section-title">Details</h2>
          <dl className="deflist">
            <div className="deflist-row">
              <dt>Title</dt>
              <dd>{project.title || <span className="muted">None</span>}</dd>
            </div>
            <div className="deflist-row">
              <dt>Description</dt>
              <dd className="prose">{project.description || <span className="muted">None</span>}</dd>
            </div>
            <div className="deflist-row">
              <dt>Retention</dt>
              <dd>{projectRetentionText(project)}</dd>
            </div>
          </dl>
        </section>
      </div>
    );
  }

  const save = async () => {
    setError("");
    setDaysError("");
    const value = Number(days);
    if (mode === "days" && (!Number.isInteger(value) || value < 1 || value > 36500)) {
      setDaysError("Enter a whole number of days from 1 to 36500.");
      return;
    }
    setBusy(true);
    try {
      const retentionDays = mode === "days" ? value : mode === "forever" ? 0 : null;
      const updated = await api.patch<ProjectOut>(projectPath(project.id), { title, description, retentionDays });
      client.setQueryData(keys.project(project.id), updated);
      void client.invalidateQueries({ queryKey: keys.projects });
      setSaved(`Saved at ${nowTime()}`);
    } catch (err) {
      setError(errorText(err, "save", "the project settings", project.id));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="form-grid"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
      noValidate
    >
      <section className="section">
        <h2 className="section-title">Details</h2>
        <Field label="Title" htmlFor={titleId}>
          <TextInput id={titleId} value={title} onChange={(e) => { setTitle(e.target.value); setSaved(""); }} maxLength={200} />
        </Field>
        <Field label="Description" htmlFor={descId}>
          <Textarea id={descId} value={description} onChange={(e) => { setDescription(e.target.value); setSaved(""); }} />
        </Field>
      </section>
      <section className="section">
        <h2 className="section-title">Retention</h2>
        <fieldset className="fieldset stack">
          <legend className="visually-hidden">Retention</legend>
          <Radio
            name="project-retention"
            label="Use the global default"
            helper={project.defaultRetentionDays ? `Currently: delete unpinned artifacts after ${plural(project.defaultRetentionDays, "day")}.` : "Currently: keep forever."}
            checked={mode === "global"}
            onChange={() => { setMode("global"); setSaved(""); }}
          />
          <Radio name="project-retention" label="Keep forever" helper="Ignore the global default for this project." checked={mode === "forever"} onChange={() => { setMode("forever"); setSaved(""); }} />
          <Radio name="project-retention" label="Delete unpinned artifacts after" checked={mode === "days"} onChange={() => { setMode("days"); setSaved(""); }} />
          <div className="indent">
            <Field label="Days" error={daysError} helper="Artifacts older than this are deleted in the next housekeeping run, at most once an hour.">
              <NumberInput aria-label="Days" value={days} unit="days" disabled={mode !== "days"} invalid={Boolean(daysError)} onChange={(e) => { setDays(e.target.value.replace(/[^\d]/g, "")); setSaved(""); }} />
            </Field>
          </div>
          <p className="field-helper">Pinned artifacts are always kept. An artifact's own retention overrides this.</p>
        </fieldset>
      </section>
      <div className="button-row">
        <Button type="submit" variant="primary" busy={busy} busyLabel={"Saving…"}>
          Save changes
        </Button>
        <ResultText>{saved}</ResultText>
      </div>
      {error ? <Notice variant="error">{error}</Notice> : null}
      <section className="section">
        <h2 className="section-title">Delete project</h2>
        <p>
          Deletes all {plural(project.sessionCount, "session")}, {plural(project.artifactCount, "artifact")} ({formatSize(project.sizeBytes)}), notes and share links.
        </p>
        <div>
          <Button onClick={onDelete}>
            {"Delete project…"}
          </Button>
        </div>
      </section>
    </form>
  );
}

function AccessTab({ project }: { project: ProjectOut }) {
  const client = useQueryClient();
  const grants = useGrants({ project: project.id });
  const users = useUsers();
  const sessions = useSessions(project.id);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<GrantOut | null>(null);
  const state = queryState(grants, "grants");
  const columns: Column<GrantOut>[] = [
    { key: "user", header: "User", render: (g) => g.username ?? String(g.userId) },
    { key: "scope", header: "Scope", render: (g) => (g.sessionId ? g.sessionName ?? g.sessionSlug : "All sessions") },
    { key: "level", header: "Level", width: "96px", render: (g) => (g.level === "editor" ? "Editor" : "Viewer") },
    { key: "granted", header: "Granted", width: "128px", render: (g) => <AbsTime value={g.createdAt} /> },
    { key: "remove", header: <span className="visually-hidden">Actions</span>, width: "96px", render: (g) => <button type="button" className="link" onClick={() => setRemoving(g)}>Remove</button> },
  ];
  return (
    <div className="stack">
      <div className="section-head">
        <h2 className="section-title">Grants for {project.id}</h2>
        {!adding ? (
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add grant
          </Button>
        ) : null}
      </div>
      {adding ? (
        <GrantForm
          fixedProject={project.id}
          users={(users.data?.items ?? []).filter((u) => u.role !== "admin" && !u.builtin).map((u) => ({ value: u.username, label: u.username }))}
          sessions={(sessions.data?.items ?? []).map((s) => ({ value: s.slug, label: s.name }))}
          onDone={() => {
            setAdding(false);
            void client.invalidateQueries({ queryKey: ["grants"] });
          }}
          onCancel={() => setAdding(false)}
        />
      ) : null}
      {state ?? (
        <DataTable
          rows={grants.data!.items}
          columns={columns}
          getId={(g) => String(g.id)}
          label="Grants"
          empty={<EmptyState action={!adding ? <Button onClick={() => setAdding(true)}>Add grant</Button> : undefined}>Only admins can see this project. Add a grant to give a member access.</EmptyState>}
        />
      )}
      {removing ? <RemoveGrantConfirm grant={removing} onClose={() => setRemoving(null)} /> : null}
    </div>
  );
}

export function RemoveGrantConfirm({ grant, onClose }: { grant: GrantOut; onClose: () => void }) {
  const client = useQueryClient();
  const who = grant.username ?? `user ${grant.userId}`;
  const scope = grant.sessionId ? `${grant.sessionName ?? grant.sessionSlug} in ${grant.projectId}` : grant.projectId;
  return (
    <ConfirmDialog
      title={`Remove ${who}'s ${grant.level} access to ${scope}?`}
      body="They can no longer see its sessions and artifacts, unless another grant allows it."
      confirmLabel="Remove access"
      busyLabel={"Removing…"}
      onClose={onClose}
      errorFor={(err) => errorText(err, "remove", "the grant")}
      onConfirm={async () => {
        await api.delete(`/api/grants/${grant.id}`);
        void client.invalidateQueries({ queryKey: ["grants"] });
      }}
    />
  );
}

export function GrantForm({ fixedProject, users, sessions, projects, onDone, onCancel, fixedUser }: { fixedProject?: string; users: { value: string; label: string }[]; sessions?: { value: string; label: string }[]; projects?: { value: string; label: string }[]; onDone: () => void; onCancel: () => void; fixedUser?: string }) {
  const [user, setUser] = useState(fixedUser ?? "");
  const [project, setProject] = useState(fixedProject ?? "");
  const [scope, setScope] = useState("");
  const [level, setLevel] = useState<"viewer" | "editor">("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const projectSessions = useSessions(project, !sessions);
  const sessionOptions = sessions ?? (project ? (projectSessions.data?.items ?? []).map((s) => ({ value: s.slug, label: s.name })) : []);
  const submit = async () => {
    if (!user) {
      setError("Choose a user.");
      return;
    }
    if (!project) {
      setError("Choose a project.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.post("/api/grants", { username: user, project, session: scope || null, level });
      onDone();
    } catch (err) {
      setBusy(false);
      setError(err instanceof ApiError && err.status === 409 ? "That grant already exists." : errorText(err, "add", "the grant", project));
    }
  };
  return (
    <form
      className="inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      noValidate
    >
      <div className="inline-form-row">
        {!fixedUser ? (
          <Field label="User">
            <Combobox label="User" options={users} value={user} onChange={(value) => setUser(value)} placeholder="username" />
          </Field>
        ) : null}
        {!fixedProject ? (
          <Field label="Project">
            <Combobox label="Project" options={projects ?? []} value={project} onChange={(value) => { setProject(value); setScope(""); }} placeholder="owner/name" />
          </Field>
        ) : null}
        <Field label="Scope">
          <Select value={scope} onChange={(event) => setScope(event.target.value)} aria-label="Scope">
            <option value="">All sessions</option>
            {sessionOptions.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Level">
          <Select value={level} onChange={(event) => setLevel(event.target.value as "viewer" | "editor")} aria-label="Level">
            <option value="viewer">Viewer</option>
            <option value="editor">Editor</option>
          </Select>
        </Field>
        <div className="inline-form-buttons">
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Adding…"}>
            Add grant
          </Button>
        </div>
      </div>
      <p className="field-helper">Viewers can open, download, mark seen and watch live views. Editors can also upload, delete, share, add notes and tag.</p>
      {error ? <Notice variant="error">{error}</Notice> : null}
    </form>
  );
}
