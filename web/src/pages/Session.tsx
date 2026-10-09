import { PluginSlot } from "../shell/plugins";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, errorText } from "../api/client";
import { keys, sessionPath, useSession, useTimeline } from "../api/queries";
import type { EventOut, LeaseBrief, SessionOut, TimelineEntry } from "../api/types";
import { Button } from "../components/Button";
import { Checkbox, Textarea } from "../components/Form";
import { OverflowMenu, separator } from "../components/Menu";
import { AbsTime, CopyLink, Forbidden, MetaItem, Mono, PageHeader, queryState, RelTime, StateWord, Tabs, Unseen } from "../components/Misc";
import { EmptyState, Loading, Notice, ResultText } from "../components/Notice";
import { ArtifactBrowser, Thumb } from "../features/ArtifactBrowser";
import { downloadZip, setSessionSeen } from "../features/artifactActions";
import { DropTarget } from "../features/DropTarget";
import { DeleteSessionsConfirm, RenameSessionDialog } from "../features/projectDialogs";
import { UploadDialog, type PickedFile } from "../features/UploadDialog";
import { canEdit, useAuth } from "../lib/auth";
import { formatAgoShort, formatClock, formatDayHeading, formatFull, formatIdle, kindLabel, leaseKindLabel, plural, resourceName, toDate } from "../lib/format";
import { nowTime, useCopy, useMinute, useTitle } from "../lib/hooks";
import { absoluteUrl, artifactUrl, hrefWith, localPath, projectUrl, sessionUrl, sharedArtifactUrl, sharedSessionUrl, usePathname, useSearchParams, useSetParams } from "../lib/url";

export function leaseStateWords(lease: LeaseBrief, now: Date): string {
  if (lease.state === "queued") return "Queued";
  if (lease.state === "idle") return formatIdle(lease.idleSeconds ?? (lease.heartbeatAt ? (now.getTime() - Date.parse(lease.heartbeatAt)) / 1000 : 0));
  if (lease.phase === "preparing") return "Preparing";
  if (lease.phase === "failed") return "Failed";
  if (lease.heartbeatAt) return `Active, heartbeat ${formatAgoShort(lease.heartbeatAt, now)}`;
  return "Active";
}

export function leaseState(lease: LeaseBrief): string {
  if (lease.phase === "failed") return "failed";
  if (lease.state === "active" && lease.phase !== "preparing") return "active";
  return lease.state;
}

function LeasesNow({ session, highlight }: { session: SessionOut; highlight?: string }) {
  const now = useMinute();
  if (!session.activeLeases.length) return null;
  return (
    <table className="lease-table" aria-label="Machines leased to this session">
      <tbody>
        {session.activeLeases.map((lease) => (
          <tr key={lease.sid} aria-current={highlight === lease.sid ? "true" : undefined}>
            <td className="lease-resource">{resourceName(lease.resource) || kindLabel(lease.kind)}</td>
            <td>
              <Mono>{lease.sid}</Mono> <CopyLink value={lease.sid} />
            </td>
            <td>
              <StateWord state={leaseState(lease)}>{leaseStateWords(lease, now)}</StateWord>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function projectName(projectId: string): string {
  return projectId.slice(projectId.indexOf("/") + 1);
}

function SessionProjects({ session, projectId }: { session: SessionOut; projectId: string | null }) {
  const listed = projectId !== null ? session.projects.filter((p) => p.project.id !== projectId) : session.projects;
  if (projectId !== null && !listed.length) return null;
  return (
    <MetaItem label={projectId !== null ? "Also in" : "Projects"}>
      {listed.length ? (
        <span className="cluster">
          {listed.map((p) => (
            <Link key={p.project.id} to={localPath(p.url) || sessionUrl(p.project.id, session.slug)} className="link">
              {p.project.id} <span className="num ink-3">{p.artifactCount.toLocaleString("en-US")}</span>
            </Link>
          ))}
          {projectId !== null ? (
            <Link to={sharedSessionUrl(session.slug)} className="link">
              all projects
            </Link>
          ) : null}
        </span>
      ) : (
        <span className="ink-3">none</span>
      )}
    </MetaItem>
  );
}

export function SessionPage({ projectId, slug, tab }: { projectId: string | null; slug: string; tab: "artifacts" | "timeline" }) {
  const auth = useAuth();
  const session = useSession(projectId, slug);
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [copied, copy] = useCopy();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [dropped, setDropped] = useState<{ files: PickedFile[]; folder: boolean } | null>(null);
  const [actionResult, setActionResult] = useState("");
  const [actionError, setActionError] = useState("");
  const timeline = useTimeline(projectId, slug);
  const where = projectId ?? "Sessions";
  useTitle(session.data ? `${session.data.name} - ${where}` : where);

  useEffect(() => {
    if (projectId !== null && slug === "_project") void navigate({ to: `${projectUrl(projectId)}/files`, replace: true });
  }, [slug, projectId, navigate]);

  const back = projectId !== null ? { to: projectUrl(projectId), label: `Back to ${projectId}` } : { to: "/sessions", label: "Back to sessions" };
  const state = queryState(session, "session", {
    notFound: (
      <EmptyState action={<Link to={back.to} className="link">{back.label}</Link>}>
        There is no session {slug}{projectId !== null ? ` in ${projectId}` : ""}. It may have been deleted.
      </EmptyState>
    ),
    forbidden: <Forbidden what={projectId ?? slug} />,
  });
  if (state) return <div className="page">{state}</div>;
  const data = session.data!;
  const editable = canEdit(data.access, auth);
  const base = sessionUrl(projectId, slug);
  const shared = projectId === null;
  const scope = shared && params.project && data.projectIds.includes(params.project) ? params.project : projectId;
  const uploadProject = scope ?? (data.projectIds.length === 1 ? data.projectIds[0] : undefined);
  const errorProject = projectId ?? undefined;

  const run = async (label: string, fn: () => Promise<string>) => {
    setActionError("");
    try {
      const message = await fn();
      setActionResult(message);
      window.setTimeout(() => setActionResult(""), 5000);
    } catch (err) {
      setActionError(errorText(err, label, "the session", errorProject));
    }
  };

  const menu = [
    { label: copied ? "Copied" : "Copy session link", onSelect: () => void copy(absoluteUrl(base)) },
    {
      label: "Download all as zip",
      onSelect: () =>
        void run("download", async () => {
          setActionResult("Preparing zip…");
          await downloadZip({ project: scope ?? undefined, session: slug }, slug);
          return "Downloaded the session as a zip.";
        }),
    },
    {
      label: "Mark all seen",
      onSelect: () =>
        void run("mark", async () => {
          const updated = await setSessionSeen(client, projectId, slug, true);
          return `Marked ${plural(updated, "artifact")} as seen.`;
        }),
    },
    ...(editable ? [{ label: "Rename…", onSelect: () => setRenaming(true) }, separator(), { label: "Delete session…", danger: true, onSelect: () => setDeleting(true) }] : []),
  ];

  const timelineCount = timeline.data?.items.filter((entry) => timelineCategory(entry) !== null).length;

  return (
    <DropTarget label={data.name} enabled={editable && tab === "artifacts"} onDrop={(files, folder) => setDropped({ files, folder })}>
      <div className="page">
        <PageHeader
          crumbs={shared ? [{ label: "Sessions", to: "/sessions" }] : [
            { label: "Projects", to: "/projects" },
            { label: projectId, to: projectUrl(projectId) },
          ]}
          title={data.name}
          mono
          actions={
            <>
              {editable ? <Button onClick={() => setParams({ upload: "1" })}>Upload</Button> : null}
              <OverflowMenu label={`More actions for ${data.name}`} items={menu} />
            </>
          }
          meta={
            <>
              <SessionProjects session={data} projectId={projectId} />
              <MetaItem label="Last activity">
                <RelTime value={data.lastActiveAt} />
              </MetaItem>
              <MetaItem label="Created">
                <AbsTime value={data.createdAt} full />
              </MetaItem>
              <MetaItem label="Link">
                <CopyLink value={absoluteUrl(base)} label="Copy" />
              </MetaItem>
              {!data.activeLeases.length ? <MetaItem label="Machines">none leased</MetaItem> : null}
            </>
          }
        />
        <LeasesNow session={data} highlight={params.lease} />
        {actionResult ? <ResultText>{actionResult}</ResultText> : null}
        {actionError ? <Notice variant="error">{actionError}</Notice> : null}
        <Tabs
          label="Session sections"
          items={[
            { label: "Captures", to: base, count: data.artifactCount, active: tab === "artifacts" },
            { label: "Timeline", to: `${base}/timeline`, count: timelineCount ?? null, active: tab === "timeline" },
          ]}
        />
        {shared && tab === "artifacts" && data.projects.length > 1 ? (
          <Tabs
            label="Projects in this session"
            sub
            items={[
              { label: "All projects", to: base, count: data.artifactCount, active: scope === null },
              ...data.projects.map((p) => ({ label: projectName(p.project.id), to: base, search: { project: p.project.id }, count: p.artifactCount, active: scope === p.project.id })),
            ]}
          />
        ) : null}
        <PluginSlot slot="session.panel" session={{ projectId: projectId ?? null, slug }} />
        {tab === "artifacts" ? (
          <ArtifactBrowser
            key={scope ?? "*"}
            projectId={scope}
            shared={shared}
            sessionSlug={slug}
            sessionName={data.name}
            canEdit={editable}
            onUpload={() => setParams({ upload: "1" })}
            emptyText="No artifacts in this session yet."
            emptyHint={
              <>
                Agents add them with <span className="mono">eks-harness upload --sid {data.activeLeases[0]?.sid ?? "<sid>"}</span>.
              </>
            }
          />
        ) : (
          <TimelineTab session={data} projectId={projectId} editable={editable} />
        )}
        {renaming ? <RenameSessionDialog projectId={projectId} session={data} onClose={() => setRenaming(false)} /> : null}
        {deleting ? <DeleteSessionsConfirm projectId={projectId} sessions={[data]} onClose={() => setDeleting(false)} onDeleted={() => void navigate({ to: back.to })} /> : null}
        {params.upload === "1" ? <UploadDialog projectId={uploadProject} sessionSlug={slug} onClose={() => setParams({ upload: null })} /> : null}
        {dropped ? <UploadDialog projectId={uploadProject} sessionSlug={slug} initialFiles={dropped.files} initialSite={dropped.folder} onClose={() => setDropped(null)} /> : null}
      </div>
    </DropTarget>
  );
}

type Category = "lease" | "capture" | "note" | "backend";
const CATEGORIES: [Category, string][] = [
  ["lease", "Leases"],
  ["capture", "Captures"],
  ["note", "Notes"],
  ["backend", "Backends"],
];
const TYPE_WORDS: Record<Category, string> = { lease: "Lease", capture: "Capture", note: "Note", backend: "Backend" };

export function timelineCategory(entry: TimelineEntry): Category | null {
  if (entry.type === "note") return "note";
  if (entry.type === "artifact") return "capture";
  const type = entry.event?.type ?? "";
  if (type.includes("heartbeat")) return null;
  const category = type.split(".")[0];
  if (category === "backend") return "backend";
  if (category === "artifact" || category === "share" || category === "session" || category === "project" || category === "settings") return null;
  if (category === "note") return null;
  return "lease";
}

function entryId(entry: TimelineEntry): string {
  if (entry.type === "note" && entry.note) return `n${entry.note.id}`;
  if (entry.type === "artifact" && entry.artifact) return `a${entry.artifact.id}`;
  return `e${entry.event?.id ?? entry.ts}`;
}

function str(detail: Record<string, unknown>, key: string): string {
  const value = detail[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

const CAPTURE_KINDS = new Set(["screenshot", "video", "dom", "mhtml", "a11y", "har", "console", "log"]);

export function isMachineActor(actor: string): boolean {
  return /[:/]/.test(actor);
}

export function eventSentence(event: EventOut): ReactNode {
  const detail = event.detail ?? {};
  const resource = resourceName(event.resource) || str(detail, "resource") || "";
  const instance = str(detail, "instance") || event.actor || "";
  const reason = str(detail, "reason");
  const sid = event.leaseSid;
  const withReason = (text: ReactNode) => (reason ? <>{text} ({reason})</> : text);
  switch (event.type) {
    case "lease.acquired":
      return (
        <>
          {resource || leaseKindLabel(str(detail, "kind"))} acquired{instance ? <> by instance <Mono>{instance}</Mono></> : null}
          {sid ? <> sid <Mono>{sid}</Mono></> : null}
        </>
      );
    case "lease.queued":
      return <>Queued for {leaseKindLabel(str(detail, "kind")) || resource}{instance ? <> by instance <Mono>{instance}</Mono></> : null}</>;
    case "artifact.created":
    case "artifact.deleted": {
      const kind = str(detail, "kind");
      const filename = str(detail, "filename");
      const verb = event.type === "artifact.deleted" ? "deleted" : CAPTURE_KINDS.has(kind) ? "captured" : "uploaded";
      return (
        <>
          {kindLabel(kind)} {verb}
          {filename ? `: ${filename}` : ""}
        </>
      );
    }
    case "lease.ready":
      return <>{resource} ready</>;
    case "lease.failed":
      return <>Could not prepare {resource}: {str(detail, "error") || reason || "unknown error"}</>;
    case "lease.idle":
      return withReason(<>{resource} idle</>);
    case "lease.released":
      return withReason(<>{resource} released</>);
    case "lease.broken":
      return withReason(<>{resource} lease broken</>);
    case "backend.status": {
      const status = str(detail, "status");
      const ports = detail.ports && typeof detail.ports === "object" ? Object.entries(detail.ports as Record<string, number>) : [];
      return (
        <>
          <Mono>{resource}</Mono> {status || "changed"}
          {ports.length ? (
            <>
              {" "}on {ports.map(([name, port], i) => (
                <Fragment key={name}>
                  {i ? ", " : ""}
                  {name} port <Mono>{port}</Mono>
                </Fragment>
              ))}
            </>
          ) : null}
          {reason ? ` (${reason})` : ""}
        </>
      );
    }
    default: {
      const status = str(detail, "status") || str(detail, "action");
      const words = event.type.split(".").slice(1).join(" ");
      return withReason(
        <>
          {resource ? `${resource} ` : ""}
          {status || words}
        </>,
      );
    }
  }
}

function autolink(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const regex = /(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  while ((match = regex.exec(text))) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(
      <a key={n++} href={match[1]} className="link" target="_blank" rel="noopener noreferrer">
        {match[1]}
      </a>,
    );
    last = match.index + match[1].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function TimelineTab({ session, projectId, editable }: { session: SessionOut; projectId: string | null; editable: boolean }) {
  const timeline = useTimeline(projectId, session.slug);
  const params = useSearchParams();
  const setParams = useSetParams();
  const pathname = usePathname();
  const client = useQueryClient();
  const listRef = useRef<HTMLDivElement>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [noteResult, setNoteResult] = useState("");
  const [noteError, setNoteError] = useState("");
  const [shownCount, setShownCount] = useState<number | null>(null);
  const atBottom = useRef(true);
  const entryHref = (artifact: { id: string; projectId: string; url: string }) =>
    projectId === null ? sharedArtifactUrl(session.slug, artifact.id) : localPath(artifact.url) || artifactUrl(artifact.projectId, session.slug, artifact.id);
  const initialScroll = useRef(false);

  const types = useMemo(() => {
    if (!params.types) return new Set<Category>(CATEGORIES.map(([c]) => c));
    return new Set(params.types.split(",").filter(Boolean) as Category[]);
  }, [params.types]);

  const allEntries = useMemo(() => {
    const items = [...(timeline.data?.items ?? [])];
    items.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    return items.filter((entry) => {
      const category = timelineCategory(entry);
      return category !== null && types.has(category);
    });
  }, [timeline.data, types]);

  const visible = shownCount !== null && shownCount < allEntries.length ? allEntries.slice(0, shownCount) : allEntries;
  const pending = allEntries.length - visible.length;

  useEffect(() => {
    const onScroll = () => {
      const node = listRef.current;
      if (!node) return;
      const rect = node.getBoundingClientRect();
      atBottom.current = rect.bottom <= window.innerHeight + 64;
      if (atBottom.current && shownCount !== null) setShownCount(null);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [shownCount]);

  useEffect(() => {
    if (!timeline.data) return;
    if (shownCount === null && !atBottom.current && initialScroll.current) setShownCount(visible.length);
  }, [allEntries.length]);

  useLayoutEffect(() => {
    if (!timeline.data || initialScroll.current) return;
    initialScroll.current = true;
    if (params.at) {
      document.getElementById(`t-${params.at}`)?.scrollIntoView({ block: "center" });
      atBottom.current = false;
    } else {
      window.scrollTo({ top: document.body.scrollHeight });
    }
  }, [timeline.data]);

  useLayoutEffect(() => {
    if (initialScroll.current && atBottom.current && shownCount === null) window.scrollTo({ top: document.body.scrollHeight });
  }, [visible.length]);

  const toggleType = (category: Category, on: boolean) => {
    const next = new Set(types);
    if (on) next.add(category);
    else next.delete(category);
    const all = CATEGORIES.every(([c]) => next.has(c));
    setParams({ types: all ? null : CATEGORIES.map(([c]) => c).filter((c) => next.has(c)).join(",") || "none" });
  };

  const addNote = async () => {
    const text = body.trim();
    if (!text || busy) return;
    setBusy(true);
    setNoteError("");
    try {
      await api.post(`${sessionPath(projectId, session.slug)}/notes`, { body: text });
      setBody("");
      setNoteResult(`Added at ${nowTime()}`);
      atBottom.current = true;
      await client.invalidateQueries({ queryKey: keys.timeline(projectId, session.slug) });
    } catch (err) {
      setNoteError(errorText(err, "add", "the note", projectId ?? undefined));
    } finally {
      setBusy(false);
    }
  };

  let content: ReactNode;
  if (timeline.isPending) content = <Loading what="timeline" />;
  else if (timeline.isError) content = queryState(timeline, "timeline");
  else if (!visible.length) {
    content = (timeline.data?.items.length ?? 0) > 0 ? (
      <EmptyState action={<Button onClick={() => setParams({ types: null })}>Clear filters</Button>}>No events match these filters.</EmptyState>
    ) : (
      <EmptyState>Nothing has happened in this session yet.</EmptyState>
    );
  } else {
    let lastDay = "";
    content = (
      <div className="timeline" ref={listRef}>
        {visible.map((entry) => {
          const date = toDate(entry.ts) ?? new Date();
          const day = date.toDateString();
          const heading = day !== lastDay ? formatDayHeading(date) : null;
          lastDay = day;
          const id = entryId(entry);
          const category = timelineCategory(entry)!;
          const highlighted = params.at === id;
          return (
            <Fragment key={id}>
              {heading ? <h3 className="timeline-day">{heading}</h3> : null}
              <div id={`t-${id}`} className="timeline-row" data-category={category} aria-current={highlighted ? "true" : undefined}>
                <a
                  className="timeline-time"
                  href={hrefWith(pathname, { ...params, at: id })}
                  title={formatFull(date)}
                  onClick={(event) => {
                    event.preventDefault();
                    setParams({ at: id }, { replace: true });
                  }}
                >
                  <time dateTime={date.toISOString()}>{formatClock(date, true)}</time>
                </a>
                <span className="timeline-type">{TYPE_WORDS[category]}</span>
                <div className="timeline-body">
                  {entry.type === "note" && entry.note ? (
                    <>
                      <p className="timeline-note">{autolink(entry.note.body)}</p>
                      <p className="timeline-author">{entry.note.author}</p>
                    </>
                  ) : entry.type === "artifact" && entry.artifact ? (
                    <div className="timeline-capture">
                      {entry.artifact.thumbnailUrl ? (
                        <Link to={entryHref(entry.artifact)} tabIndex={-1} aria-hidden="true" className="timeline-thumb">
                          <Thumb item={entry.artifact} size="small" />
                        </Link>
                      ) : (
                        <span className="timeline-thumb" aria-hidden="true" />
                      )}
                      <span className="timeline-capture-text">
                        <Link to={entryHref(entry.artifact)} className="link-quiet strong">
                          {!entry.artifact.seen ? <Unseen /> : null}
                          {entry.artifact.caption || entry.artifact.filename}
                        </Link>
                        <span className="frame-meta">
                          {projectId === null ? <span className="frame-meta-strong">{projectName(entry.artifact.projectId)}</span> : null}
                          <span>{kindLabel(entry.artifact.kind)}</span>
                          {entry.artifact.meta?.annotation ? <span>annotated</span> : null}
                        </span>
                      </span>
                    </div>
                  ) : entry.event ? (
                    <p>{eventSentence(entry.event)}</p>
                  ) : null}
                </div>
              </div>
            </Fragment>
          );
        })}
      </div>
    );
  }

  return (
    <div>
      <div className="toolbar timeline-filter" role="group" aria-label="Event types">
        <span className="tag-filter-label">Show</span>
        {CATEGORIES.map(([category, label]) => (
          <Checkbox key={category} label={label} checked={types.has(category)} onChange={(event) => toggleType(category, event.target.checked)} />
        ))}
      </div>
      {content}
      {pending > 0 ? (
        <div className="timeline-new">
          <Notice
            variant="info"
            action={
              <button
                type="button"
                className="link"
                onClick={() => {
                  setShownCount(null);
                  atBottom.current = true;
                  window.setTimeout(() => window.scrollTo({ top: document.body.scrollHeight }), 0);
                }}
              >
                Show
              </button>
            }
          >
            {plural(pending, "new event")}.
          </Notice>
        </div>
      ) : null}
      {editable ? (
        <form
          className="note-form"
          onSubmit={(event) => {
            event.preventDefault();
            void addNote();
          }}
        >
          <label htmlFor="note-body" className="subhead">
            Add a note
          </label>
          <Textarea
            id="note-body"
            value={body}
            onChange={(event) => {
              setBody(event.target.value);
              setNoteResult("");
            }}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void addNote();
              }
            }}
          />
          <div className="button-row">
            <Button type="submit" variant="primary" busy={busy} busyLabel={"Adding…"} disabled={!body.trim()}>
              Add note
            </Button>
            <ResultText>{noteResult}</ResultText>
          </div>
          {noteError ? <Notice variant="error">{noteError}</Notice> : null}
        </form>
      ) : null}
    </div>
  );
}
