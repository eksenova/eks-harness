import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ApiError, uploadForm, type UploadHandle } from "../api/client";
import { invalidateArtifactViews, useProjects, useSessions } from "../api/queries";
import type { ArtifactOut } from "../api/types";
import { Button, IconButton } from "../components/Button";
import { Dialog } from "../components/Dialog";
import { Field, Radio, Select, TextInput } from "../components/Form";
import { Combobox, Progress, TagInput } from "../components/Misc";
import { Notice } from "../components/Notice";
import { useAuth } from "../lib/auth";
import { formatSize, kindLabel, plural, slugify } from "../lib/format";
import { useIsSmall } from "../lib/hooks";
import { artifactUrl, localPath, sessionUrl } from "../lib/url";

export interface PickedFile {
  file: File;
  path: string;
}

type RowState = { status: "waiting" } | { status: "uploading"; pct: number | null } | { status: "done"; url: string } | { status: "failed"; message: string };

const KINDS = ["screenshot", "video", "dom", "mhtml", "a11y", "har", "console", "log", "file"];

async function readEntry(entry: FileSystemEntry, prefix: string, out: PickedFile[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    out.push({ file, path: prefix + entry.name });
    return;
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await readEntry(child, `${prefix}${entry.name}/`, out);
    }
  }
}

export async function filesFromDrop(transfer: DataTransfer): Promise<{ files: PickedFile[]; folder: boolean }> {
  const items = Array.from(transfer.items ?? []);
  const entries = items.map((item) => (typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null)).filter(Boolean) as FileSystemEntry[];
  if (entries.length && entries.some((entry) => entry.isDirectory)) {
    const out: PickedFile[] = [];
    for (const entry of entries) await readEntry(entry, "", out);
    return { files: out, folder: true };
  }
  return { files: Array.from(transfer.files).map((file) => ({ file, path: file.name })), folder: false };
}

function stripTopFolder(files: PickedFile[]): { files: PickedFile[]; name: string } {
  const tops = new Set(files.map((f) => f.path.split("/")[0]));
  if (tops.size === 1 && files.every((f) => f.path.includes("/"))) {
    const top = Array.from(tops)[0];
    return { files: files.map((f) => ({ file: f.file, path: f.path.slice(top.length + 1) })), name: top };
  }
  return { files, name: "" };
}

interface UploadDialogProps {
  projectId?: string;
  sessionSlug?: string | null;
  initialFiles?: PickedFile[];
  initialSite?: boolean;
  onClose: () => void;
}

export function UploadDialog({ projectId: initialProject, sessionSlug: initialSession, initialFiles, initialSite, onClose }: UploadDialogProps) {
  const auth = useAuth();
  const client = useQueryClient();
  const navigate = useNavigate();
  const small = useIsSmall();
  const projects = useProjects();
  const [projectId, setProjectId] = useState(initialProject ?? "");
  const [session, setSession] = useState<{ value: string; created: boolean }>({ value: initialSession ?? "", created: false });
  const sessions = useSessions(projectId);
  const [mode, setMode] = useState<"files" | "site">(initialSite ? "site" : "files");
  const [files, setFiles] = useState<PickedFile[]>(initialFiles ?? []);
  const [kind, setKind] = useState("");
  const [caption, setCaption] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [entry, setEntry] = useState("index.html");
  const [states, setStates] = useState<RowState[]>([]);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState("");
  const [siteResult, setSiteResult] = useState<ArtifactOut | null>(null);
  const handle = useRef<UploadHandle<ArtifactOut> | null>(null);
  const stopRequested = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const zipInput = useRef<HTMLInputElement>(null);
  const projectFieldId = useId();
  const sessionFieldId = useId();

  useEffect(() => {
    if (folderInput.current) {
      folderInput.current.setAttribute("webkitdirectory", "");
      folderInput.current.setAttribute("directory", "");
    }
  }, [mode]);

  const projectOptions = useMemo(
    () =>
      (projects.data?.items ?? [])
        .filter((p) => auth.isAdmin || p.access === "editor" || p.access === "admin")
        .map((p) => ({ value: p.id, label: p.id })),
    [projects.data, auth.isAdmin],
  );
  const sessionOptions = useMemo(
    () => [{ value: "", label: "No session (project level)" }, ...(sessions.data?.items ?? []).map((s) => ({ value: s.slug, label: s.name }))],
    [sessions.data],
  );
  const sessionLabel = session.created ? session.value : sessionOptions.find((o) => o.value === session.value)?.label ?? session.value;

  const setPicked = (picked: PickedFile[], folder: boolean) => {
    if (running) return;
    setStates([]);
    setFinished(false);
    setError("");
    if (mode === "site" || folder) {
      if (folder) {
        setMode("site");
        setFiles(stripTopFolder(picked).files);
        return;
      }
      const zip = picked.find((p) => /\.zip$/i.test(p.file.name));
      if (zip) {
        setMode("site");
        setFiles([zip]);
        return;
      }
      if (mode === "site") {
        setError("A site is one folder or one zip file with an index.html.");
        return;
      }
    }
    setFiles((prev) => {
      const seen = new Set(prev.map((p) => p.path + p.file.size));
      return [...prev, ...picked.filter((p) => !seen.has(p.path + p.file.size))];
    });
  };

  const totalSize = files.reduce((sum, f) => sum + f.file.size, 0);
  const isZipSite = mode === "site" && files.length === 1 && /\.zip$/i.test(files[0].file.name);
  const doneCount = states.filter((s) => s.status === "done").length;

  const errorFor = (err: unknown, name: string): string => {
    if (err instanceof ApiError) {
      if (err.error === "aborted") return "stopped";
      if (err.status === 413) return `${name} is larger than the upload limit (storage.maxUploadMb)`;
      if (err.status === 0) return "the connection closed";
      if (err.status === 403) return `you do not have permission to upload to ${projectId}`;
      return err.message.replace(/\.$/, "");
    }
    return err instanceof Error ? err.message : String(err);
  };

  const baseForm = () => {
    const form = new FormData();
    form.append("project", projectId);
    if (session.value) form.append("session", session.value);
    if (caption.trim()) form.append("caption", caption.trim());
    if (tags.length) form.append("tags", tags.join(","));
    form.append("source", "ui");
    return form;
  };

  const uploadOne = async (index: number) => {
    const picked = files[index];
    const form = baseForm();
    if (kind) form.append("kind", kind);
    form.append("filename", picked.file.name);
    form.append("file", picked.file, picked.file.name);
    setStates((prev) => prev.map((s, i) => (i === index ? { status: "uploading", pct: null } : s)));
    handle.current = uploadForm<ArtifactOut>("/api/artifacts", form, (loaded, total) => {
      setStates((prev) => prev.map((s, i) => (i === index ? { status: "uploading", pct: total ? (loaded / total) * 100 : null } : s)));
    });
    try {
      const result = await handle.current.promise;
      setStates((prev) => prev.map((s, i) => (i === index ? { status: "done", url: localPath(result.url) || artifactUrl(projectId, result.sessionSlug, result.id) } : s)));
    } catch (err) {
      setStates((prev) => prev.map((s, i) => (i === index ? { status: "failed", message: errorFor(err, picked.file.name) } : s)));
    }
  };

  const uploadSite = async () => {
    const form = baseForm();
    form.append("entry", entry.trim() || "index.html");
    if (isZipSite) {
      form.append("name", files[0].file.name.replace(/\.zip$/i, ""));
      form.append("file", files[0].file, files[0].file.name);
    } else {
      for (const picked of files) form.append("files", picked.file, picked.path);
    }
    setStates([{ status: "uploading", pct: null }]);
    handle.current = uploadForm<ArtifactOut>("/api/sites", form, (loaded, total) => setStates([{ status: "uploading", pct: total ? (loaded / total) * 100 : null }]));
    try {
      const result = await handle.current.promise;
      setSiteResult(result);
      setStates([{ status: "done", url: localPath(result.url) }]);
    } catch (err) {
      setStates([{ status: "failed", message: errorFor(err, "The site") }]);
    }
  };

  const start = async (only?: number) => {
    if (!projectId) {
      setError("Choose a project first.");
      return;
    }
    if (!files.length) {
      setError(mode === "site" ? "Choose a folder or a zip file." : "Choose at least one file.");
      return;
    }
    setError("");
    setRunning(true);
    setFinished(false);
    stopRequested.current = false;
    if (mode === "site") {
      await uploadSite();
    } else {
      setStates((prev) => files.map((_, i) => (only !== undefined && i !== only ? prev[i] ?? { status: "waiting" } : { status: "waiting" })));
      for (let i = 0; i < files.length; i += 1) {
        if (only !== undefined && i !== only) continue;
        if (stopRequested.current) break;
        await uploadOne(i);
      }
    }
    handle.current = null;
    setRunning(false);
    setFinished(true);
    invalidateArtifactViews(client, projectId);
  };

  const stop = () => {
    stopRequested.current = true;
    handle.current?.abort();
  };

  const targetUrl = session.value && !session.created ? sessionUrl(projectId, session.value) : session.value ? sessionUrl(projectId, slugify(session.value)) : `${sessionUrl(projectId, null)}`;
  const allDone = finished && states.length > 0 && states.every((s) => s.status === "done");
  const doneFiles = mode === "site" ? (allDone ? 1 : 0) : doneCount;

  const footer = allDone ? (
    <>
      <span className="dialog-status">{mode === "site" ? "Uploaded the site." : `Uploaded ${plural(doneFiles, "file")}.`}</span>
      <Button onClick={onClose}>Done</Button>
      <Button
        variant="primary"
        onClick={() => {
          onClose();
          void navigate({ to: siteResult ? localPath(siteResult.url) : targetUrl });
        }}
      >
        {siteResult ? "Open site" : session.value ? "Open session" : "Open project files"}
      </Button>
    </>
  ) : (
    <>
      <span className="dialog-status">{states.length && mode === "files" ? `Uploaded ${doneCount} of ${files.length}.` : ""}</span>
      {running ? <Button onClick={stop}>Stop</Button> : <Button onClick={onClose}>Cancel</Button>}
      <Button type="submit" variant="primary" busy={running} busyLabel={"Uploading…"} disabled={!files.length || !projectId}>
        {mode === "site" ? "Upload site" : files.length ? `Upload ${plural(files.length, "file")}` : "Upload files"}
      </Button>
    </>
  );

  return (
    <Dialog title="Upload" onClose={onClose} wide busy={running} onSubmit={() => void start()} footer={footer} bottomSheet={small}>
      <div className="stack">
        <div className="field-row">
          <Field label="Project" htmlFor={projectFieldId}>
            <Combobox
              id={projectFieldId}
              autoFocusTarget
              label="Project"
              options={projectOptions}
              value={projectId}
              placeholder="owner/name"
              onChange={(value) => {
                setProjectId(value);
                setSession({ value: "", created: false });
              }}
            />
          </Field>
          <Field label="Session" htmlFor={sessionFieldId}>
            <Combobox
              id={sessionFieldId}
              label="Session"
              options={sessionOptions}
              value={session.created ? session.value : session.value}
              placeholder="No session (project level)"
              createLabel={(text) => `Create session ${text}`}
              onChange={(value, created) => setSession({ value, created })}
            />
          </Field>
        </div>
        {session.created && sessionLabel ? <p className="field-helper">A new session {sessionLabel} is created with the upload.</p> : null}
        <fieldset className="fieldset">
          <legend className="field-label">What</legend>
          <div className="radio-row">
            <Radio name="upload-mode" label="Files" checked={mode === "files"} disabled={running} onChange={() => { setMode("files"); setFiles([]); setStates([]); }} />
            <Radio name="upload-mode" label="Site (a folder or a zip with index.html)" checked={mode === "site"} disabled={running} onChange={() => { setMode("site"); setFiles([]); setStates([]); }} />
          </div>
        </fieldset>
        <div
          className={`dropzone ${dragOver ? "is-over" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={async (event) => {
            event.preventDefault();
            event.stopPropagation();
            setDragOver(false);
            const { files: dropped, folder } = await filesFromDrop(event.dataTransfer);
            setPicked(dropped, folder);
          }}
        >
          {dragOver ? (
            <span>Release to add</span>
          ) : mode === "files" ? (
            <span>
              Drop files here, or{" "}
              <Button onClick={() => fileInput.current?.click()} disabled={running}>
                Choose files
              </Button>
            </span>
          ) : (
            <span>
              Drop a folder or a zip here, or{" "}
              <Button onClick={() => folderInput.current?.click()} disabled={running}>
                Choose folder
              </Button>{" "}
              <Button onClick={() => zipInput.current?.click()} disabled={running}>
                Choose zip
              </Button>
            </span>
          )}
          <input ref={fileInput} type="file" multiple hidden onChange={(event) => { setPicked(Array.from(event.target.files ?? []).map((file) => ({ file, path: file.name })), false); event.target.value = ""; }} />
          <input ref={folderInput} type="file" multiple hidden onChange={(event) => { setPicked(Array.from(event.target.files ?? []).map((file) => ({ file, path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name })), true); event.target.value = ""; }} />
          <input ref={zipInput} type="file" accept=".zip,application/zip" hidden onChange={(event) => { setPicked(Array.from(event.target.files ?? []).map((file) => ({ file, path: file.name })), false); event.target.value = ""; }} />
        </div>
        <div className="field-row">
          {mode === "files" ? (
            <Field label="Kind">
              <Select value={kind} onChange={(event) => setKind(event.target.value)} aria-label="Kind" disabled={running}>
                <option value="">Detect automatically</option>
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {kindLabel(k)}
                  </option>
                ))}
              </Select>
            </Field>
          ) : (
            <Field label="Entry file" helper="The page the preview opens first.">
              <TextInput value={entry} onChange={(event) => setEntry(event.target.value)} mono disabled={running} aria-label="Entry file" />
            </Field>
          )}
          <Field label="Caption (optional)">
            <TextInput value={caption} onChange={(event) => setCaption(event.target.value)} aria-label="Caption" disabled={running} />
          </Field>
        </div>
        <div className="field">
          <span className="field-label">Tags (optional)</span>
          <TagInput tags={tags} onChange={setTags} disabled={running} />
        </div>
        {files.length ? (
          mode === "site" ? (
            <div className="upload-rows">
              <div className="upload-row">
                <span className="ellipsis">{isZipSite ? files[0].file.name : `${plural(files.length, "file")} in the folder`}</span>
                <span className="align-right">{formatSize(totalSize)}</span>
                <RowStatus state={states[0]} onRetry={() => void start()} />
              </div>
            </div>
          ) : (
            <div className="upload-rows">
              {files.map((picked, index) => (
                <div key={picked.path + index} className="upload-row">
                  <span className="ellipsis" title={picked.path}>
                    {picked.path}
                  </span>
                  <span className="align-right">{formatSize(picked.file.size)}</span>
                  {!states.length ? (
                    <IconButton icon="close" label={`Remove ${picked.file.name}`} onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))} />
                  ) : (
                    <RowStatus state={states[index]} onRetry={() => void start(index)} />
                  )}
                </div>
              ))}
            </div>
          )
        ) : null}
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

function RowStatus({ state, onRetry }: { state: RowState | undefined; onRetry: () => void }) {
  if (!state || state.status === "waiting") return <span className="muted">Waiting</span>;
  if (state.status === "uploading") return state.pct === null ? <span className="muted">{"Uploading…"}</span> : <Progress value={state.pct} />;
  if (state.status === "done")
    return (
      <span>
        Uploaded{" "}
        <a className="link" href={state.url}>
          Open
        </a>
      </span>
    );
  return (
    <span className="danger">
      Failed: {state.message}{" "}
      <button type="button" className="link" onClick={onRetry}>
        Retry
      </button>
    </span>
  );
}
