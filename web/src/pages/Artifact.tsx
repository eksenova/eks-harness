import { PluginSlot } from "../shell/plugins";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, encodeSegment, errorText } from "../api/client";
import { keys, useArtifact, useProject, useShares, useSession, useTagCatalog, useTags } from "../api/queries";
import type { ArtifactOut, ShareOut } from "../api/types";
import { Button, ButtonLink, IconButton } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Checkbox, NumberInput, Radio, Select, Textarea, TextInput } from "../components/Form";
import { OverflowMenu } from "../components/Menu";
import { CopyField, CopyLink, DefList, Forbidden, Mono, queryState, RelTime, TagInput, TagToken, Unseen, type Crumb, Breadcrumbs } from "../components/Misc";
import { EmptyState, Notice, ResultText } from "../components/Notice";
import { collectAll, deleteArtifacts, patchArtifact, setSeen } from "../features/artifactActions";
import { AnnotationCrops, AnnotationPanels, useAnnotationState } from "../features/annotations";
import { listSort, Thumb } from "../features/ArtifactBrowser";
import { useAuth, canEdit } from "../lib/auth";
import { useDeletedArtifact } from "../lib/events";
import { dimensions, flatMeta, formatClock, formatCount, formatDuration, formatFull, formatFuture, formatSize, kindLabel, metaNumber, metaString, plural, shortHash, sourceLabel, stripScheme, toDate } from "../lib/format";
import { copyText, nowTime, shortcutAllowed, useKeydown, useMinute, useTitle } from "../lib/hooks";
import { absoluteUrl, artifactUrl, hrefWith, LIST_CONTEXT_KEYS, localPath, pick, projectUrl, sessionUrl, SHARED_CONTEXT_KEYS, sharedArtifactUrl, sharedSessionUrl, useSearchParams, useSetParams } from "../lib/url";
import { TagColorDialog } from "../features/tagColors";
import { Viewer } from "../viewers/Viewer";
import { viewerKind } from "../viewers/types";

let flash: { text: string; until: number } | null = null;

export function setFlash(text: string): void {
  flash = { text, until: Date.now() + 5000 };
}

function useFlash(id: string): string {
  const [text, setText] = useState(() => (flash && flash.until > Date.now() ? flash.text : ""));
  useEffect(() => {
    if (!text) return;
    flash = null;
    const timer = window.setTimeout(() => setText(""), 5000);
    return () => window.clearTimeout(timer);
  }, [id, text]);
  return text;
}

function useNavList(projectId: string | null, slug: string | null, params: Record<string, string>, enabled = true) {
  const { sort, dir } = listSort(params);
  const listParams = {
    project: projectId ?? undefined,
    session: slug ?? undefined,
    projectLevel: projectId !== null && slug === null,
    kind: params.kind,
    tag: params.tag,
    q: params.q,
    unseen: params.unseen === "1",
    sort,
    dir,
  };
  return useQuery({
    queryKey: ["artifact-nav", listParams],
    queryFn: async () => {
      const items = await collectAll(listParams);
      const factor = dir === "asc" ? 1 : -1;
      items.sort((a, b) => {
        let r = 0;
        if (sort === "name") r = a.filename.localeCompare(b.filename, "en", { numeric: true, sensitivity: "base" });
        else if (sort === "size") r = a.size - b.size;
        else if (sort === "kind") r = a.kind.localeCompare(b.kind);
        else r = Date.parse(a.createdAt) - Date.parse(b.createdAt);
        return (r || a.id.localeCompare(b.id)) * factor;
      });
      return items;
    },
    staleTime: 30_000,
    enabled,
  });
}

export function ArtifactPage({ owner, name, slug, id, shared = false }: { owner?: string; name?: string; slug: string | null; id: string; shared?: boolean }) {
  const auth = useAuth();
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const artifact = useArtifact(id);
  const projectId = shared ? artifact.data?.projectId ?? "" : `${owner}/${name}`;
  const sessionSlug = slug === "_project" ? null : slug;
  const sharedSlug = shared && sessionSlug ? sessionSlug : null;
  const session = useSession(sharedSlug ? null : projectId, sessionSlug ?? "", Boolean(sessionSlug) && Boolean(projectId || sharedSlug));
  const project = useProject(projectId, Boolean(projectId));
  const navScope = sharedSlug ? params.project || null : projectId;
  const nav = useNavList(navScope, sessionSlug, params, Boolean(navScope !== "" && (sharedSlug || projectId)));
  const [colorTag, setColorTag] = useState<string | null>(null);
  const deleted = useDeletedArtifact(id);
  const flashText = useFlash(id);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);
  const [missing, setMissing] = useState(false);
  const captionRef = useRef<{ edit: () => void } | null>(null);
  const tagRef = useRef<HTMLInputElement>(null);
  const markedFor = useRef<string | null>(null);
  const [error, setError] = useState("");

  const data = artifact.data;
  const annotation = useAnnotationState(data ?? null);
  useTitle(data ? `${data.filename} - ${sessionSlug ? session.data?.name ?? sessionSlug : projectId}` : "Artifact");

  const context = pick(params, sharedSlug ? SHARED_CONTEXT_KEYS : LIST_CONTEXT_KEYS);
  const listUrl = sharedSlug ? sharedSessionUrl(sharedSlug) : sessionSlug ? sessionUrl(projectId, sessionSlug) : `${projectUrl(projectId)}/files`;
  const backHref = hrefWith(listUrl, context);
  const navItems = nav.data ?? [];
  const ids = navItems.map((item) => item.id);
  const index = ids.indexOf(id);
  const prevId = index > 0 ? ids[index - 1] : index < 0 ? data?.previousId ?? null : null;
  const nextId = index >= 0 && index < ids.length - 1 ? ids[index + 1] : index < 0 ? data?.nextId ?? null : null;
  const pageUrl = (other: string) => (sharedSlug ? sharedArtifactUrl(sharedSlug, other) : artifactUrl(projectId, sessionSlug, other));
  const hrefFor = (other: string) => hrefWith(pageUrl(other), context);

  useEffect(() => {
    if (!data || markedFor.current === data.id) return;
    markedFor.current = data.id;
    if (!data.seen && viewerKind(data) !== "video") {
      setSeen(client, [data.id], true, projectId).catch(() => undefined);
    }
  }, [data, client, projectId]);

  useEffect(() => {
    setMissing(false);
    if (!data || ["site", "dom", "mhtml"].includes(data.kind)) return;
    let cancelled = false;
    fetch(localPath(data.rawUrl), { method: "HEAD", credentials: "same-origin" })
      .then((response) => {
        if (!cancelled && response.status === 404) setMissing(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [data?.id]);

  const editable = data
    ? canEdit(sharedSlug ? project.data?.access ?? session.data?.access : sessionSlug ? session.data?.access ?? project.data?.access : project.data?.access, auth)
    : false;
  const disabled = Boolean(deleted);

  const toggleSeen = async () => {
    if (!data) return;
    setError("");
    try {
      await setSeen(client, [data.id], !data.seen, projectId);
    } catch (err) {
      setError(errorText(err, "update", "the seen state", projectId));
    }
  };
  const togglePinned = async () => {
    if (!data || !editable) return;
    setError("");
    try {
      await patchArtifact(client, data.id, { pinned: !data.pinned });
    } catch (err) {
      setError(errorText(err, "pin", "the artifact", projectId));
    }
  };

  useKeydown((event) => {
    if (!data) return;
    if (event.key === "Escape" && shortcutAllowed(event) && !(document.activeElement instanceof HTMLElement && document.activeElement.closest(".har-table"))) {
      event.preventDefault();
      void navigate({ to: backHref });
      return;
    }
    if (!shortcutAllowed(event)) return;
    const target = event.target as HTMLElement;
    const inViewer = target.closest?.(".har-table, .video-stage, .video-chrome, .text-frame, .json-tree");
    switch (event.key) {
      case "ArrowLeft":
        if (inViewer) return;
        if (prevId) void navigate({ to: hrefFor(prevId) });
        break;
      case "ArrowRight":
        if (inViewer) return;
        if (nextId) void navigate({ to: hrefFor(nextId) });
        break;
      case "s":
        void toggleSeen();
        break;
      case "p":
        void togglePinned();
        break;
      case "d":
        window.location.assign(localPath(data.downloadUrl));
        break;
      case "c":
        void copyText(absoluteUrl(window.location.pathname)).then(() => {
          setLinkCopied(true);
          window.setTimeout(() => setLinkCopied(false), 2000);
        });
        break;
      case "e":
        if (editable) captionRef.current?.edit();
        break;
      case "t":
        if (editable) tagRef.current?.focus();
        break;
      case "x":
        if (editable) setConfirmDelete(true);
        break;
      default:
        return;
    }
    event.preventDefault();
  });

  const crumbs: Crumb[] = sharedSlug
    ? [
        { label: "Sessions", to: "/sessions" },
        { label: session.data?.name ?? sharedSlug, to: backHref },
      ]
    : [
        { label: "Projects", to: "/projects" },
        { label: projectId, to: projectUrl(projectId) },
        ...(sessionSlug ? [{ label: session.data?.name ?? sessionSlug, to: backHref }] : [{ label: "Project files", to: backHref }]),
      ];

  const state = queryState(artifact, "artifact", {
    notFound: (
      <EmptyState action={<Link to={backHref} className="link">Back to {sessionSlug ? session.data?.name ?? sessionSlug : projectId}</Link>}>
        There is no artifact {id} here. It may have been deleted.
      </EmptyState>
    ),
    forbidden: <Forbidden what={projectId || sessionSlug || id} />,
  });
  if (state || !data) {
    return (
      <div className="page">
        <header className="page-head">
          <Breadcrumbs items={crumbs} />
        </header>
        {state}
      </div>
    );
  }

  const afterDelete = () => {
    setFlash(`Deleted ${data.filename}.`);
    const target = nextId ?? prevId;
    void navigate({ to: target ? hrefFor(target) : backHref, replace: true });
  };

  const siteBase = null;
  const viewerProps = { artifact: data, rawUrl: localPath(data.rawUrl), siteBase, params, setParams };

  const kind = viewerKind(data);
  return (
    <div className="lightbox" data-viewer={kind}>
      <header className="lightbox-head">
        <div className="lightbox-title">
          <Breadcrumbs items={crumbs} />
          <h1 className="lightbox-name" tabIndex={-1} data-page-title="" title={data.filename}>
            {!data.seen ? <Unseen /> : null}
            <span className="ellipsis">{data.filename}</span>
          </h1>
        </div>
        <nav className="lightbox-nav" aria-label="Artifact navigation">
          {index >= 0 ? (
            <span className="lightbox-count num">
              {formatCount(index + 1)} of {formatCount(ids.length)}
            </span>
          ) : null}
          <IconButton icon="previous" label="Previous" shortcut="Left arrow" disabled={!prevId} onClick={() => prevId && void navigate({ to: hrefFor(prevId) })} />
          <IconButton icon="next" label="Next" shortcut="Right arrow" disabled={!nextId} onClick={() => nextId && void navigate({ to: hrefFor(nextId) })} />
          <IconButton icon="close" label={sessionSlug ? "Back to session" : "Back to project files"} shortcut="Escape" onClick={() => void navigate({ to: backHref })} />
        </nav>
      </header>
      {flashText || deleted ? (
        <div className="lightbox-notices">
          {flashText ? <ResultText>{flashText}</ResultText> : null}
          {deleted ? <Notice variant="attention" title={`This artifact was deleted${deleted.actor ? ` by ${deleted.actor}` : ""} at ${formatClock(toDate(deleted.ts) ?? new Date())}.`} /> : null}
        </div>
      ) : null}
      <div className="lightbox-body">
        <div className="lightbox-stage" data-stage={kind}>
          <div className="lightbox-viewer">
            {missing ? (
              <div className="state">
                <p className="state-sentence">The file for this artifact is missing from the store. Its record is still here.</p>
                {editable ? (
                  <div className="button-row">
                    <Button onClick={() => setConfirmDelete(true)}>
                      {"Delete record…"}
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : (
              <Viewer key={data.id} {...viewerProps} onEnded={() => void setSeen(client, [data.id], true, projectId).catch(() => undefined)} />
            )}
          </div>
          <AnnotationCrops crops={annotation.crops} />
          <Filmstrip items={navItems} currentId={data.id} hrefFor={hrefFor} />
        </div>
        <aside className="inspector" aria-label="Artifact details">
          <div className="inspector-actions">
            <ButtonLink to={localPath(data.downloadUrl)} download icon="download" variant="primary">
              Download
            </ButtonLink>
            <Button onClick={() => void copyText(absoluteUrl(pageUrl(data.id))).then(() => {
              setLinkCopied(true);
              window.setTimeout(() => setLinkCopied(false), 2000);
            })} icon="copy">
              {linkCopied ? "Copied" : "Copy link"}
            </Button>
            <span className="toolbar-spacer" />
            <OverflowMenu
              label={`More actions for ${data.filename}`}
              items={[
                { label: "Open raw file", onSelect: () => window.open(localPath(data.siteUrl ?? data.rawUrl), "_blank", "noopener") },
                { label: "Copy direct link", onSelect: () => void copyText(data.siteUrl ?? data.rawUrl) },
                { label: "Copy as Markdown", onSelect: () => void copyText(markdownFor(data, absoluteUrl(pageUrl(data.id)))) },
                { label: "Copy artifact id", onSelect: () => void copyText(data.id) },
                { label: "Copy sha256", onSelect: () => void copyText(data.sha256) },
              ]}
            />
          </div>
          {error ? <Notice variant="error">{error}</Notice> : null}
          <div className="inspector-section inspector-toggles">
            <Checkbox label="Seen by you" checked={data.seen} disabled={disabled} onChange={() => void toggleSeen()} />
            <Checkbox label="Pinned" checked={data.pinned} disabled={disabled || !editable} helper="Kept when retention runs" onChange={() => void togglePinned()} />
          </div>
          <CaptionEditor artifact={data} editable={editable && !disabled} controlRef={captionRef} />
          <TagsEditor artifact={data} editable={editable && !disabled} inputRef={tagRef} projectId={sharedSlug ? null : projectId} slug={sessionSlug} onColor={editable && !disabled ? setColorTag : undefined} />
          <PluginSlot slot="artifact.viewer" props={{ artifact: data as unknown as Record<string, unknown> }} />
          <ReviewLinks artifact={data} hrefFor={hrefFor} />
          <section className="inspector-section">
            <h2 className="inspector-title">Links</h2>
            <DefList
              compact
              items={[
                { label: "Page", value: <span className="mono ellipsis">{stripScheme(absoluteUrl(pageUrl(data.id)))}</span>, copy: absoluteUrl(pageUrl(data.id)) },
                { label: "Direct file", value: <span className="mono ellipsis">{stripScheme(absoluteUrl(localPath(data.siteUrl ?? data.rawUrl)))}</span>, copy: absoluteUrl(localPath(data.siteUrl ?? data.rawUrl)) },
              ]}
            />
          </section>
          <SharesSection artifact={data} editable={editable && !disabled} projectId={projectId} />
          <section className="inspector-section">
            <h2 className="inspector-title">Details</h2>
            <ArtifactDetails artifact={data} projectId={projectId} sessionName={session.data?.name ?? null} shared={Boolean(sharedSlug)} />
          </section>
          <RetentionEditor artifact={data} editable={editable && !disabled} projectId={projectId} />
          <AnnotationPanels artifact={data} state={annotation} editable={editable && !disabled} projectId={projectId} />
          {editable && !disabled ? (
            <div className="inspector-section">
              <Button onClick={() => setConfirmDelete(true)}>{"Delete artifact…"}</Button>
            </div>
          ) : null}
        </aside>
      </div>
      {colorTag ? <TagColorDialog tag={colorTag} onClose={() => setColorTag(null)} /> : null}
      {confirmDelete ? (
        <ConfirmDialog
          title={`Delete ${data.filename}?`}
          body={`This deletes the file (${formatSize(data.size)})${data.shareCount ? ` and its ${data.shareCount === 1 ? "1 share link" : `${data.shareCount} share links`}` : ""}. This cannot be undone.`}
          confirmLabel="Delete artifact"
          busyLabel={"Deleting…"}
          onClose={() => setConfirmDelete(false)}
          errorFor={(err) => errorText(err, "delete", "the artifact", projectId)}
          onConfirm={async () => {
            await deleteArtifacts(client, [data.id], projectId);
            void client.invalidateQueries({ queryKey: ["artifact-nav"] });
            afterDelete();
          }}
        />
      ) : null}
    </div>
  );
}

function CaptionEditor({ artifact, editable, controlRef }: { artifact: ArtifactOut; editable: boolean; controlRef: React.MutableRefObject<{ edit: () => void } | null> }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(artifact.caption);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const areaRef = useRef<HTMLTextAreaElement>(null);
  controlRef.current = {
    edit: () => {
      setText(artifact.caption);
      setEditing(true);
      window.setTimeout(() => areaRef.current?.focus(), 0);
    },
  };
  useEffect(() => {
    setEditing(false);
    setSaved("");
    setText(artifact.caption);
  }, [artifact.id]);
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await patchArtifact(client, artifact.id, { caption: text.trim() });
      setEditing(false);
      setSaved(`Saved at ${nowTime()}`);
    } catch (err) {
      setError(errorText(err, "save", "the caption"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Caption</h2>
      {editing ? (
        <>
          <Textarea
            ref={areaRef}
            aria-label="Caption"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void save();
              } else if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setEditing(false);
              }
            }}
          />
          <div className="button-row">
            <Button variant="primary" busy={busy} busyLabel={"Saving…"} onClick={() => void save()}>
              Save
            </Button>
            <button type="button" className="link" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <p className={artifact.caption ? "prose" : "muted"}>{artifact.caption || "No caption"}</p>
          <div className="button-row">
            {editable ? (
              <button type="button" className="link" onClick={() => controlRef.current?.edit()}>
                Edit
              </button>
            ) : null}
            <ResultText>{saved}</ResultText>
          </div>
        </>
      )}
      {error ? <p className="field-error">{error}</p> : null}
    </section>
  );
}

function TagsEditor({ artifact, editable, inputRef, projectId, slug, onColor }: { artifact: ArtifactOut; editable: boolean; inputRef: React.Ref<HTMLInputElement>; projectId: string | null; slug: string | null; onColor?: (tag: string) => void }) {
  const client = useQueryClient();
  const tags = useTags(projectId, slug);
  const catalog = useTagCatalog();
  const suggestions = Array.from(new Set([...(tags.data ?? []).map((t) => t.tag), ...(catalog.data?.items ?? []).map((t) => t.tag)])).sort();
  const [local, setLocal] = useState(artifact.tags);
  const [error, setError] = useState("");
  useEffect(() => setLocal(artifact.tags), [artifact.tags]);
  const change = async (next: string[]) => {
    const previous = local;
    setLocal(next);
    setError("");
    try {
      await patchArtifact(client, artifact.id, { tags: next });
      void client.invalidateQueries({ queryKey: ["tags"] });
    } catch (err) {
      setLocal(previous);
      setError(errorText(err, "save", "the tags"));
    }
  };
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Tags</h2>
      {editable ? (
        <TagInput tags={local} onChange={(next) => void change(next)} suggestions={suggestions} inputRef={inputRef} onColor={onColor} />
      ) : local.length ? (
        <div className="tag-input">
          {local.map((tag) => (
            <TagToken key={tag} tag={tag} />
          ))}
        </div>
      ) : (
        <p className="muted">No tags.</p>
      )}
      {editable && !local.length ? <p className="muted">No tags.</p> : null}
      {error ? <p className="field-error">{error}</p> : null}
    </section>
  );
}

function retentionSummary(artifact: ArtifactOut): string {
  const days = artifact.effectiveRetentionDays;
  const when = artifact.expiresAt ? `, around ${formatFull(artifact.expiresAt)}` : "";
  switch (artifact.retentionSource) {
    case "pinned":
      return "Kept forever because it is pinned.";
    case "artifact":
      return `Deleted ${plural(days ?? 0, "day")} after it was created${when}. Set on this artifact.`;
    case "project":
      return days ? `Deleted ${plural(days, "day")} after it was created${when}. Set by the project.` : "Kept forever. Set by the project.";
    case "global":
      return days ? `Deleted ${plural(days, "day")} after it was created${when}. Global default.` : "Kept forever. The project and the global default set no retention.";
    default:
      return "";
  }
}

function RetentionEditor({ artifact, editable, projectId }: { artifact: ArtifactOut; editable: boolean; projectId: string }) {
  const client = useQueryClient();
  const [mode, setMode] = useState<"inherit" | "days">(artifact.retentionDays ? "days" : "inherit");
  const [days, setDays] = useState(String(artifact.retentionDays ?? 30));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setMode(artifact.retentionDays ? "days" : "inherit");
    setDays(String(artifact.retentionDays ?? 30));
  }, [artifact.retentionDays]);
  const value = Number(days);
  const invalid = mode === "days" && (!Number.isInteger(value) || value < 1 || value > 36500);
  const wanted = mode === "days" ? value : null;
  const dirty = !invalid && wanted !== (artifact.retentionDays ?? null);
  const save = async () => {
    if (!dirty) return;
    setBusy(true);
    setError("");
    try {
      await patchArtifact(client, artifact.id, { retentionDays: wanted });
    } catch (err) {
      setError(errorText(err, "change", "the retention", projectId || undefined));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Retention</h2>
      <p className="muted">{retentionSummary(artifact)}</p>
      {editable ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <Radio name={`retention-${artifact.id}`} label="Follow the project" checked={mode === "inherit"} onChange={() => setMode("inherit")} />
          <Radio name={`retention-${artifact.id}`} label="Own retention" checked={mode === "days"} onChange={() => setMode("days")} />
          <div className="retention-row">
            <NumberInput aria-label="Days to keep" value={days} unit="days" disabled={mode !== "days"} invalid={invalid} onChange={(event) => setDays(event.target.value.replace(/[^\d]/g, ""))} />
            <Button type="submit" disabled={!dirty} busy={busy} busyLabel={"Saving…"}>
              Save
            </Button>
          </div>
        </form>
      ) : null}
      {error ? <p className="field-error">{error}</p> : null}
    </section>
  );
}

function ReviewLinks({ artifact, hrefFor }: { artifact: ArtifactOut; hrefFor: (id: string) => string }) {
  const meta = (artifact.meta ?? {}) as Record<string, unknown>;
  const video = typeof meta.video === "string" ? meta.video : null;
  const sheets = Array.isArray(meta.sheets) ? meta.sheets.filter((id): id is string => typeof id === "string") : [];
  const checks = typeof meta.checks === "string" ? meta.checks : null;
  const role = meta.role === "checks" ? "checks" : meta.role === "sheet" ? "sheet" : null;
  const verdict = role === "checks" ? meta.ok : meta.checksOk;
  if (!video && !sheets.length && !checks) return null;
  const link = (id: string, text: string) => (
    <Link to={hrefFor(id)} className="link" title={id}>
      {text}
    </Link>
  );
  const outcome = typeof verdict === "boolean" ? (verdict ? "all checks pass" : "checks failed") : null;
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Review</h2>
      <DefList
        items={[
          video ? { label: "Video", value: link(video, "Open the video") } : null,
          ...(role === "sheet" ? [] : sheets.map((id, index) => ({
            label: sheets.length > 1 ? `Contact sheet ${index + 1}/${sheets.length}` : "Contact sheet",
            value: link(id, "Open the sheet"),
          }))),
          checks && role !== "checks" ? { label: "Checks", value: link(checks, outcome ? `Report: ${outcome}` : "Open the report") } : null,
          role === "checks" && outcome ? { label: "Verdict", value: outcome[0].toUpperCase() + outcome.slice(1) } : null,
        ]}
      />
    </section>
  );
}

function ArtifactDetails({ artifact, projectId, sessionName, shared }: { artifact: ArtifactOut; projectId: string; sessionName: string | null; shared: boolean }) {
  const meta = flatMeta(artifact.meta);
  const url = metaString(meta, "url", "pageUrl", "urlAtCapture");
  const viewport = meta.viewport && typeof meta.viewport === "object" ? dimensions(Number((meta.viewport as Record<string, unknown>).width), Number((meta.viewport as Record<string, unknown>).height)) : metaString(meta, "viewport");
  const device = metaString(meta, "deviceName", "device");
  const profile = metaString(meta, "profile", "browserProfile");
  const lines = metaNumber(meta, "lines", "lineCount");
  const requests = metaNumber(meta, "requests", "requestCount", "entries");
  const files = metaNumber(meta, "fileCount", "files");
  const entry = metaString(meta, "entry");
  return (
    <DefList
      items={[
        shared ? { label: "Project", value: <Link to={projectUrl(projectId)} className="link">{projectId}</Link> } : null,
        { label: "Kind", value: kindLabel(artifact.kind) },
        { label: "Size", value: `${formatSize(artifact.size)} (${formatCount(artifact.size)} bytes)` },
        artifact.width && artifact.height ? { label: "Dimensions", value: dimensions(artifact.width, artifact.height) } : null,
        artifact.durationMs ? { label: "Duration", value: formatDuration(artifact.durationMs) } : null,
        requests !== null && artifact.kind === "har" ? { label: "Requests", value: formatCount(requests) } : null,
        lines !== null ? { label: "Lines", value: formatCount(lines) } : null,
        files !== null && (artifact.kind === "site" || artifact.kind === "mhtml") ? { label: "Files", value: formatCount(files) } : null,
        entry && artifact.kind === "site" ? { label: "Entry", value: entry, mono: true } : null,
        device ? { label: "Device", value: device.includes(":") ? device.replace(/^ios:/, "iOS ").replace(/^android:/, "Android ") : device } : null,
        profile ? { label: "Browser profile", value: profile } : null,
        url
          ? {
              label: "URL at capture",
              value: (
                <a href={url} className="link mono" target="_blank" rel="noopener noreferrer">
                  {url}
                </a>
              ),
              copy: url,
            }
          : null,
        viewport ? { label: "Viewport", value: viewport } : null,
        { label: "Created", value: formatFull(artifact.createdAt) },
        { label: "Source", value: sourceLabel(artifact.source) },
        artifact.createdBy ? { label: "Created by", value: artifact.createdBy } : null,
        artifact.sessionSlug && artifact.sessionSlug !== "_project"
          ? { label: "Session", value: <Link to={shared ? sharedSessionUrl(artifact.sessionSlug) : sessionUrl(projectId, artifact.sessionSlug)} className="link">{artifact.sessionName ?? sessionName ?? artifact.sessionSlug}</Link> }
          : null,
        artifact.leaseSid ? { label: "Lease", value: <Link to={`/sid/${encodeURIComponent(artifact.leaseSid)}`} className="link mono">{artifact.leaseSid}</Link>, copy: artifact.leaseSid } : null,
        { label: "ID", value: artifact.id, mono: true, copy: artifact.id },
        { label: "SHA-256", value: shortHash(artifact.sha256), mono: true, copy: artifact.sha256, title: artifact.sha256 },
        { label: "MIME type", value: artifact.mime, mono: true },
      ]}
    />
  );
}

const EXPIRY_OPTIONS: [string, string][] = [
  ["never", "Never expires"],
  ["1h", "Expires in 1 hour"],
  ["1d", "1 day"],
  ["7d", "7 days"],
  ["30d", "30 days"],
  ["custom", "Custom date…"],
];

function SharesSection({ artifact, editable, projectId }: { artifact: ArtifactOut; editable: boolean; projectId: string }) {
  const client = useQueryClient();
  const shares = useShares(artifact.id);
  const now = useMinute();
  const [expiry, setExpiry] = useState("never");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<ShareOut | null>(null);
  const [error, setError] = useState("");
  const [revoking, setRevoking] = useState<ShareOut | null>(null);
  useEffect(() => {
    setCreated(null);
    setError("");
  }, [artifact.id]);

  const create = async () => {
    setBusy(true);
    setError("");
    try {
      let body: Record<string, unknown> = {};
      if (expiry === "custom") {
        const date = custom ? new Date(custom) : null;
        if (!date || Number.isNaN(date.getTime()) || date.getTime() <= Date.now()) {
          setError("Choose a date and time in the future.");
          setBusy(false);
          return;
        }
        body = { expiresAt: date.toISOString() };
      } else if (expiry !== "never") {
        body = { expires: expiry };
      }
      const share = await api.post<ShareOut>(`/api/artifacts/${encodeSegment(artifact.id)}/shares`, body);
      setCreated(share);
      void client.invalidateQueries({ queryKey: keys.shares(artifact.id) });
      void client.invalidateQueries({ queryKey: keys.artifact(artifact.id) });
    } catch (err) {
      setError(errorText(err, "create", "the share link", projectId));
    } finally {
      setBusy(false);
    }
  };

  const active = (shares.data?.items ?? []).filter((share) => !share.revokedAt);
  const expiryText = (share: ShareOut) => {
    const date = toDate(share.expiresAt);
    const expired = Boolean(date && (date.getTime() <= now.getTime() || !share.active));
    if (!date) return "never expires";
    if (expired) return <span className="strong">expired</span>;
    return <span title={formatFull(date)}>expires {formatFuture(date, now).replace(/^In/, "in")}</span>;
  };

  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Share links</h2>
      {editable ? (
        <div className="share-create cluster">
          <Select value={expiry} onChange={(event) => setExpiry(event.target.value)} aria-label="Expiry">
            {EXPIRY_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
          {expiry === "custom" ? <TextInput type="datetime-local" value={custom} onChange={(event) => setCustom(event.target.value)} aria-label="Expires at" /> : null}
          <Button onClick={() => void create()} busy={busy} busyLabel={"Creating…"}>
            Create link
          </Button>
        </div>
      ) : null}
      {created ? (
        <div className="stack-tight">
          <span className="field-label">Share page</span>
          <CopyField value={created.url} autoSelect label="Share page" />
          {created.directUrl ? (
            <>
              <span className="field-label">Direct link to the file</span>
              <CopyField value={created.directUrl} label="Direct link to the file" />
            </>
          ) : null}
          <ResultText>Link created. Anyone with it can view and download this artifact. The direct link opens the file itself.</ResultText>
        </div>
      ) : null}
      {error ? <p className="field-error">{error}</p> : null}
      {shares.isError ? (
        <p className="field-error">{errorText(shares.error, "load", "the share links", projectId)}</p>
      ) : shares.data ? (
        active.length ? (
          <ul className="share-list">
            {active.map((share) => (
              <li key={share.token} className="share-item">
                <span className="share-item-head">
                  <Mono title={share.url}>{share.token.slice(0, 8)}{"…"}</Mono>
                  <CopyLink value={share.url} label="Copy page" />
                  {share.directUrl ? <CopyLink value={share.directUrl} label="Copy file" /> : null}
                  {editable ? (
                    <button type="button" className="link share-revoke" onClick={() => setRevoking(share)}>
                      Revoke
                    </button>
                  ) : null}
                </span>
                <span className="share-item-meta">
                  <span>{expiryText(share)}</span>
                  <span>{plural(share.views, "view")}</span>
                  {share.lastViewedAt ? (
                    <>
                      , last <RelTime value={share.lastViewedAt} />
                    </>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No share links.</p>
        )
      ) : null}
      {revoking ? (
        <ConfirmDialog
          title="Revoke this share link?"
          body={'People who have it will see "This link was revoked."'}
          confirmLabel="Revoke link"
          busyLabel={"Revoking…"}
          onClose={() => setRevoking(null)}
          errorFor={(err) => (err instanceof ApiError && err.status === 404 ? "The link no longer exists." : errorText(err, "revoke", "the share link", projectId))}
          onConfirm={async () => {
            await api.delete(`/api/shares/${encodeSegment(revoking.token)}`);
            if (created?.token === revoking.token) setCreated(null);
            void client.invalidateQueries({ queryKey: keys.shares(artifact.id) });
            void client.invalidateQueries({ queryKey: keys.artifact(artifact.id) });
          }}
        />
      ) : null}
    </section>
  );
}

function markdownFor(artifact: ArtifactOut, pageLink: string): string {
  const label = artifact.caption || artifact.filename;
  const direct = absoluteUrl(localPath(artifact.rawUrl));
  if (viewerKind(artifact) === "image") return `[![${label}](${direct})](${pageLink})`;
  return `[${label}](${pageLink})`;
}

const FILMSTRIP_RADIUS = 8;

function Filmstrip({ items, currentId, hrefFor }: { items: ArtifactOut[]; currentId: string; hrefFor: (id: string) => string }) {
  const ref = useRef<HTMLOListElement>(null);
  const index = items.findIndex((item) => item.id === currentId);
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>('[aria-current="true"]')?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [currentId, items.length]);
  if (items.length < 2 || index < 0) return null;
  const start = Math.max(0, index - FILMSTRIP_RADIUS);
  const shown = items.slice(start, Math.min(items.length, index + FILMSTRIP_RADIUS + 1));
  return (
    <nav className="filmstrip" aria-label="Nearby artifacts">
      <ol ref={ref}>
        {shown.map((item) => (
          <li key={item.id}>
            <Link to={hrefFor(item.id)} className="filmstrip-frame" aria-current={item.id === currentId ? "true" : undefined} data-unseen={item.seen ? undefined : ""} title={item.caption || item.filename}>
              <Thumb item={item} size="strip" />
            </Link>
          </li>
        ))}
      </ol>
    </nav>
  );
}
