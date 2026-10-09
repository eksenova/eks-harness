import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useState } from "react";
import { api, ApiError, errorText, projectPath } from "../api/client";
import { invalidateArtifactViews, keys, sessionPath } from "../api/queries";
import type { DeleteSummary, GrantList, ProjectOut, SessionOut } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Field, TextInput, Textarea } from "../components/Form";
import { Notice } from "../components/Notice";
import { formatCount, formatSize, plural, resourceName, slugify } from "../lib/format";
import { projectUrl, sessionUrl } from "../lib/url";

const PART = /^[a-z0-9][a-z0-9._-]{0,62}$/;
const PART_ERROR = "Use lowercase letters, digits, ., _ and -, starting with a letter or digit.";

export function ProjectFormDialog({ project, onClose }: { project?: ProjectOut; onClose: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [owner, setOwner] = useState(project?.owner ?? "");
  const [name, setName] = useState(project?.name ?? "");
  const [title, setTitle] = useState(project?.title ?? "");
  const [description, setDescription] = useState(project?.description ?? "");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [idError, setIdError] = useState("");
  const ownerId = useId();
  const nameId = useId();
  const titleId = useId();
  const descId = useId();
  const editing = Boolean(project);
  const ownerInvalid = submitted && !editing && !PART.test(owner);
  const nameInvalid = submitted && !editing && !PART.test(name);

  const submit = async () => {
    setSubmitted(true);
    setError("");
    setIdError("");
    if (!editing && (!PART.test(owner) || !PART.test(name))) return;
    setBusy(true);
    try {
      if (project) {
        const updated = await api.patch<ProjectOut>(projectPath(project.id), { title, description });
        client.setQueryData(keys.project(project.id), updated);
        void client.invalidateQueries({ queryKey: keys.projects });
        onClose();
      } else {
        const created = await api.post<ProjectOut>("/api/projects", { owner, name, title, description });
        void client.invalidateQueries({ queryKey: keys.projects });
        onClose();
        void navigate({ to: projectUrl(created?.id ?? `${owner}/${name}`) });
      }
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 409) setIdError(`A project ${owner}/${name} already exists.`);
      else if (err instanceof ApiError && err.status === 422) setError("Fix the marked fields.");
      else setError(errorText(err, editing ? "save" : "create", editing ? "the project" : "the project"));
    }
  };

  return (
    <Dialog
      title={editing ? `Edit ${project!.id}` : "Create project"}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={editing ? "Saving…" : "Creating…"}>
            {editing ? "Save changes" : "Create project"}
          </Button>
        </>
      }
    >
      <div className="stack">
        {editing ? (
          <p>
            <span className="muted">Project </span>
            {project!.id}
          </p>
        ) : (
          <div>
            <div className="id-row">
              <Field label="Owner" htmlFor={ownerId}>
                <TextInput id={ownerId} value={owner} invalid={ownerInvalid || Boolean(idError)} onChange={(e) => setOwner(e.target.value.trim())} placeholder="acme" autoCapitalize="none" spellCheck={false} data-autofocus="" />
              </Field>
              <span className="id-slash" aria-hidden="true">
                /
              </span>
              <Field label="Name" htmlFor={nameId}>
                <TextInput id={nameId} value={name} invalid={nameInvalid || Boolean(idError)} onChange={(e) => setName(e.target.value.trim())} placeholder="web-app" autoCapitalize="none" spellCheck={false} />
              </Field>
            </div>
            <p className={ownerInvalid || nameInvalid || idError ? "field-error" : "field-helper"}>{idError || (ownerInvalid || nameInvalid ? PART_ERROR : "Lowercase letters, digits, dot, hyphen and underscore. Up to 63 characters each.")}</p>
          </div>
        )}
        <Field label="Title (optional)" htmlFor={titleId}>
          <TextInput id={titleId} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
        </Field>
        <Field label="Description (optional)" htmlFor={descId}>
          <Textarea id={descId} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

export function DeleteProjectConfirm({ project, onClose, onDeleted }: { project: ProjectOut; onClose: () => void; onDeleted?: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [grants, setGrants] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .get<GrantList>("/api/grants", { query: { project: project.id } })
      .then((list) => !cancelled && setGrants(list.items.length))
      .catch(() => !cancelled && setGrants(null));
    return () => {
      cancelled = true;
    };
  }, [project.id]);
  const parts = [
    plural(project.sessionCount, "session"),
    `${formatCount(project.artifactCount)} ${project.artifactCount === 1 ? "artifact" : "artifacts"} (${formatSize(project.sizeBytes)})`,
    "their notes and share links",
  ];
  if (grants) parts.push(plural(grants, "grant"));
  return (
    <ConfirmDialog
      title={`Delete project ${project.id}?`}
      body={`This deletes ${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}. This cannot be undone.`}
      confirmLabel="Delete project"
      busyLabel={"Deleting…"}
      typeToConfirm={project.id}
      onClose={onClose}
      errorFor={(err) => errorText(err, "delete", "the project", project.id)}
      onConfirm={async () => {
        await api.delete<DeleteSummary>(projectPath(project.id));
        client.removeQueries({ queryKey: keys.project(project.id) });
        void client.invalidateQueries({ queryKey: keys.projects });
        onDeleted?.();
        void navigate({ to: "/projects" });
      }}
    />
  );
}

export function DeleteSessionsConfirm({ projectId, sessions, onClose, onDeleted }: { projectId: string | null; sessions: SessionOut[]; onClose: () => void; onDeleted?: () => void }) {
  const client = useQueryClient();
  const single = sessions.length === 1 ? sessions[0] : null;
  const artifacts = sessions.reduce((s, x) => s + x.artifactCount, 0);
  const bytes = sessions.reduce((s, x) => s + x.sizeBytes, 0);
  const notes = sessions.reduce((s, x) => s + x.noteCount, 0);
  const leases = sessions.flatMap((s) => s.activeLeases);
  const leaseText = leases.length
    ? ` ${leases.map((l) => `${resourceName(l.resource) || l.kind} (${l.sid})`).join(", ")} ${leases.length === 1 ? "is" : "are"} still leased to ${single ? "this session" : "these sessions"}; ${leases.length === 1 ? "the lease is" : "the leases are"} released first.`
    : "";
  const others = single && projectId ? single.projectIds.filter((p) => p !== projectId) : [];
  const where = projectId ?? (single && single.projectIds.length ? single.projectIds.join(", ") : "every project");
  const body = single && others.length
    ? `This deletes the ${plural(artifacts, "artifact")} (${formatSize(bytes)}) of ${projectId} in this session and their share links. The session stays in ${others.join(", ")} with its notes. This cannot be undone.${leaseText}`
    : single
    ? `This deletes ${plural(artifacts, "artifact")} (${formatSize(bytes)}), ${plural(notes, "note")} and their share links in ${where}. Share links stop working. This cannot be undone.${leaseText}`
    : `This deletes ${plural(sessions.length, "session")} with ${plural(artifacts, "artifact")} (${formatSize(bytes)}), ${plural(notes, "note")} and their share links. This cannot be undone.${leaseText}`;
  return (
    <ConfirmDialog
      title={single ? `Delete session ${single.name}?` : `Delete ${plural(sessions.length, "session")}?`}
      body={body}
      confirmLabel={single ? "Delete session" : `Delete ${plural(sessions.length, "session")}`}
      busyLabel={"Deleting…"}
      typeToConfirm={single && single.artifactCount > 20 ? single.name : undefined}
      onClose={onClose}
      errorFor={(err) => errorText(err, "delete", single ? "the session" : "the sessions", projectId ?? undefined)}
      onConfirm={async () => {
        for (const session of sessions) {
          await api.delete<DeleteSummary>(sessionPath(projectId, session.slug));
        }
        invalidateArtifactViews(client, projectId);
        onDeleted?.();
      }}
    />
  );
}

export function RenameSessionDialog({ projectId, session, onClose }: { projectId: string | null; session: SessionOut; onClose: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(session.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const id = useId();
  const slug = slugify(name);
  const submit = async () => {
    if (!name.trim()) {
      setError("Enter a name.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const updated = await api.patch<SessionOut>(sessionPath(projectId, session.slug), { name: name.trim() });
      void client.invalidateQueries({ queryKey: ["sessions"] });
      void client.invalidateQueries({ queryKey: ["session"] });
      onClose();
      const nextSlug = updated?.slug ?? slug;
      const current = encodeURIComponent(session.slug);
      if (window.location.pathname.includes(`/s/${current}`) || window.location.pathname.includes(`/sessions/${current}`)) void navigate({ to: sessionUrl(projectId, nextSlug), replace: true });
    } catch (err) {
      setBusy(false);
      setError(errorText(err, "rename", "the session", projectId ?? undefined));
    }
  };
  return (
    <Dialog
      title={`Rename ${session.name}`}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Renaming…"}>
            Rename
          </Button>
        </>
      }
    >
      <div className="stack">
        <Field label="Name" htmlFor={id} helper={`The link changes to …/${projectId ? "s" : "sessions"}/${slug}${session.projectIds.length > 1 ? ` in ${session.projectIds.join(", ")}` : ""}. Old links stop working.`}>
          <TextInput id={id} value={name} onChange={(e) => setName(e.target.value)} data-autofocus="" />
        </Field>
        <p className="mono muted">{slug}</p>
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}
