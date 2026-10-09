import { Link } from "@tanstack/react-router";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, errorText } from "../api/client";
import { useProjects, useSharedSessions, useTagCatalog } from "../api/queries";
import type { ArtifactOut } from "../api/types";
import { Button, Segmented } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { Checkbox, Field, NumberInput, SearchInput, Select, TextInput } from "../components/Form";
import { AbsTime, Meter, Mono, PageHeader, RelTime, Section, TagList, TagInput } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { DataTable, type Column } from "../components/Table";
import { EvidenceTabs } from "../features/EvidenceTabs";
import { formatCount, formatSize, KIND_LABELS, kindLabel, plural, SOURCE_LABELS } from "../lib/format";
import { useDebounced, useTitle } from "../lib/hooks";
import { localPath } from "../lib/url";

type ProjectLevel = "include" | "exclude" | "only";
type SeenFilter = "" | "unseen" | "seen" | "never";
type Order = "oldest" | "newest" | "largest";

interface Filter {
  projects: string[];
  sessions: string[];
  sessionPattern: string;
  excludeSessions: string[];
  projectLevel: ProjectLevel;
  olderThanDays: string;
  sessionIdleDays: string;
  createdAfter: string;
  createdBefore: string;
  kinds: string[];
  tagsAny: string[];
  tagsAll: string[];
  tagsNone: string[];
  sources: string[];
  minSizeMb: string;
  maxSizeMb: string;
  q: string;
  seen: SeenFilter;
  includePinned: boolean;
  includeShared: boolean;
  includeLive: boolean;
}

interface Group {
  count: number;
  bytes: number;
}

interface Preview {
  count: number;
  bytes: number;
  oldest: number | null;
  newest: number | null;
  matched: Group;
  skipped: { pinned: number; shared: number; live: number };
  projects: (Group & { project: string })[];
  kinds: (Group & { kind: string })[];
  tags: (Group & { tag: string })[];
  sessions: (Group & { slug: string | null; name: string; lastActiveAt: number | null })[];
  more: Record<string, boolean>;
  emptySessions: number;
  empty: boolean;
  sample: ArtifactOut[];
}

interface Applied {
  artifacts: number;
  bytes: number;
  shares: number;
  sessions: number;
  notes: number;
}

const BLANK: Filter = {
  projects: [],
  sessions: [],
  sessionPattern: "",
  excludeSessions: [],
  projectLevel: "include",
  olderThanDays: "",
  sessionIdleDays: "",
  createdAfter: "",
  createdBefore: "",
  kinds: [],
  tagsAny: [],
  tagsAll: [],
  tagsNone: [],
  sources: [],
  minSizeMb: "",
  maxSizeMb: "",
  q: "",
  seen: "",
  includePinned: false,
  includeShared: false,
  includeLive: false,
};

const PRESETS: { label: string; filter: Partial<Filter> }[] = [
  { label: "Older than 30 days", filter: { olderThanDays: "30" } },
  { label: "Videos older than 14 days", filter: { olderThanDays: "14", kinds: ["video"] } },
  { label: "Sessions idle for 60 days", filter: { sessionIdleDays: "60" } },
  { label: "Never opened, older than 7 days", filter: { olderThanDays: "7", seen: "never" } },
  { label: "Larger than 100 MB", filter: { minSizeMb: "100" } },
];

const STORE_KEY = "ehx.cleanup.filter";
const MB = 1024 * 1024;

function loadFilter(): Filter {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? { ...BLANK, ...(JSON.parse(raw) as Partial<Filter>) } : BLANK;
  } catch {
    return BLANK;
  }
}

function saveFilter(filter: Filter): void {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(filter));
  } catch {
    return;
  }
}

function num(value: string): number | null {
  const parsed = Number.parseFloat(value.replace(",", "."));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function day(value: string, end = false): number | null {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime() / 1000 + (end ? 86400 : 0);
}

function toBody(f: Filter): Record<string, unknown> {
  const minSize = num(f.minSizeMb);
  const maxSize = num(f.maxSizeMb);
  return {
    projects: f.projects,
    sessions: f.sessions,
    sessionPattern: f.sessionPattern.trim() || null,
    excludeSessions: f.excludeSessions,
    projectLevel: f.projectLevel,
    olderThanDays: num(f.olderThanDays),
    sessionIdleDays: num(f.sessionIdleDays),
    createdAfter: day(f.createdAfter),
    createdBefore: day(f.createdBefore, true),
    kinds: f.kinds,
    tagsAny: f.tagsAny,
    tagsAll: f.tagsAll,
    tagsNone: f.tagsNone,
    sources: f.sources,
    minSize: minSize === null ? null : Math.round(minSize * MB),
    maxSize: maxSize === null ? null : Math.round(maxSize * MB),
    q: f.q.trim() || null,
    seen: f.seen || null,
    includePinned: f.includePinned,
    includeShared: f.includeShared,
    includeLive: f.includeLive,
  };
}

function iso(seconds: number | null | undefined): string | null {
  return seconds ? new Date(seconds * 1000).toISOString() : null;
}

function toggle(list: string[], value: string, on: boolean): string[] {
  return on ? [...new Set([...list, value])] : list.filter((v) => v !== value);
}

function Checks({ options, value, onChange, label }: { options: { value: string; label: string }[]; value: string[]; onChange: (next: string[]) => void; label: string }) {
  return (
    <div className="cleanup-checks" role="group" aria-label={label}>
      {options.map((o) => (
        <Checkbox key={o.value} label={o.label} checked={value.includes(o.value)} onChange={(e) => onChange(toggle(value, o.value, e.target.checked))} />
      ))}
    </div>
  );
}

function Breakdown<T extends Group>({ title, rows, label, more, total }: { title: string; rows: T[]; label: (row: T) => ReactNode; more?: boolean; total: number }) {
  if (!rows.length) return null;
  const max = Math.max(...rows.map((r) => r.bytes), 1);
  return (
    <div className="cleanup-breakdown">
      <h3 className="cleanup-breakdown-title">{title}</h3>
      <table className="cleanup-bars">
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <th scope="row">{label(row)}</th>
              <td className="num">{formatCount(row.count)}</td>
              <td className="num">{formatSize(row.bytes)}</td>
              <td className="cleanup-bar">
                <Meter value={row.bytes} max={max} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {more ? <p className="ink-3 cleanup-more">Showing the {rows.length} largest of more; {formatCount(total)} artifacts in all.</p> : null}
    </div>
  );
}

export function CleanupPage() {
  useTitle("Clean up");
  const client = useQueryClient();
  const projects = useProjects();
  const sessions = useSharedSessions();
  const catalog = useTagCatalog();
  const [filter, setFilter] = useState<Filter>(loadFilter);
  const [order, setOrder] = useState<Order>("oldest");
  const [confirm, setConfirm] = useState(false);
  const [removeEmpty, setRemoveEmpty] = useState(true);
  const [done, setDone] = useState<Applied | null>(null);
  const body = useMemo(() => toBody(filter), [filter]);
  const debounced = useDebounced(body, 300);
  const update = (patch: Partial<Filter>) => {
    setDone(null);
    setFilter((current) => ({ ...current, ...patch }));
  };

  useEffect(() => saveFilter(filter), [filter]);

  const preview = useQuery({
    queryKey: ["cleanup", debounced, order],
    queryFn: () => api.post<Preview>("/api/cleanup/preview", { filter: debounced, sample: 50, order }),
    placeholderData: keepPreviousData,
  });
  const data = preview.data;
  const stale = preview.isFetching || debounced !== body;
  const projectOptions = (projects.data?.items ?? []).map((p) => p.id);
  const sessionOptions = (sessions.data?.items ?? []).map((s) => s.slug);
  const tagOptions = (catalog.data?.items ?? []).map((t) => t.tag);
  const kindOptions = Object.keys(KIND_LABELS).map((k) => ({ value: k, label: kindLabel(k) }));
  const sourceOptions = Object.entries(SOURCE_LABELS).map(([value, label]) => ({ value, label }));
  const skipped = data ? Object.entries(data.skipped).filter(([, n]) => n > 0) : [];
  const canDelete = Boolean(data && !data.empty && data.count > 0 && !stale);

  const apply = async () => {
    if (!data) return;
    try {
      const result = await api.post<Applied>("/api/cleanup/apply", { filter: debounced, expectCount: data.count, removeEmptySessions: removeEmpty });
      setDone(result);
      void client.invalidateQueries();
    } catch (err) {
      if (err instanceof ApiError && err.error === "cleanup_changed") void preview.refetch();
      throw err;
    }
  };

  const columns: Column<ArtifactOut>[] = [
    { key: "name", header: "Artifact", render: (a) => <Link to={localPath(a.url)} className="link-quiet">{a.caption || a.filename}</Link>, title: (a) => a.caption || a.filename },
    { key: "where", header: "Session", render: (a) => (a.sessionSlug ? <Mono>{a.sessionSlug}</Mono> : <span className="muted">Project files</span>), title: (a) => `${a.projectId} ${a.sessionSlug ?? ""}` },
    { key: "kind", header: "Kind", width: "112px", render: (a) => kindLabel(a.kind) },
    { key: "tags", header: "Tags", render: (a) => <TagList tags={a.tags} /> },
    { key: "size", header: "Size", align: "right", width: "96px", render: (a) => formatSize(a.size) },
    { key: "created", header: "Created", align: "right", width: "120px", render: (a) => <RelTime value={a.createdAt} /> },
  ];

  return (
    <div className="page">
      <PageHeader title="Evidence" />
      <EvidenceTabs active="cleanup" />
      <div className="cleanup-presets action-row" role="group" aria-label="Presets">
        {PRESETS.map((p) => (
          <Button key={p.label} size="sm" onClick={() => update({ ...BLANK, ...p.filter })}>
            {p.label}
          </Button>
        ))}
        <Button size="sm" variant="quiet" onClick={() => update(BLANK)}>
          Clear conditions
        </Button>
      </div>
      <div className="cleanup-grid">
        <form className="cleanup-form" onSubmit={(e) => e.preventDefault()} aria-label="Cleanup conditions">
          <Section title="Where">
            <Field label="Projects" helper="None selected: every project you can edit.">
              <Checks label="Projects" options={projectOptions.map((p) => ({ value: p, label: p }))} value={filter.projects} onChange={(projects) => update({ projects })} />
            </Field>
            <Field label="Only these sessions">
              <TagInput tags={filter.sessions} onChange={(sessions) => update({ sessions })} suggestions={sessionOptions} label="Add session" />
            </Field>
            <Field label="Sessions matching" helper="Wildcards on the session name or slug, for example feature-* or *demo*.">
              <TextInput value={filter.sessionPattern} onChange={(e) => update({ sessionPattern: e.target.value })} placeholder="feature-*" mono />
            </Field>
            <Field label="Except these sessions">
              <TagInput tags={filter.excludeSessions} onChange={(excludeSessions) => update({ excludeSessions })} suggestions={sessionOptions} label="Add session" />
            </Field>
            <Field label="Project files (no session)">
              <Segmented<ProjectLevel>
                label="Project files"
                value={filter.projectLevel}
                onChange={(projectLevel) => update({ projectLevel })}
                options={[
                  { value: "include", label: "Include" },
                  { value: "exclude", label: "Exclude" },
                  { value: "only", label: "Only" },
                ]}
              />
            </Field>
          </Section>
          <Section title="When">
            <Field label="Created more than">
              <NumberInput value={filter.olderThanDays} onChange={(e) => update({ olderThanDays: e.target.value })} unit="days ago" aria-label="Created more than days ago" />
            </Field>
            <Field label="Session inactive for">
              <NumberInput value={filter.sessionIdleDays} onChange={(e) => update({ sessionIdleDays: e.target.value })} unit="days" aria-label="Session inactive for days" />
            </Field>
            <div className="field-row">
              <Field label="Created from">
                <TextInput type="date" className="date-input" value={filter.createdAfter} onChange={(e) => update({ createdAfter: e.target.value })} />
              </Field>
              <Field label="Created until">
                <TextInput type="date" className="date-input" value={filter.createdBefore} onChange={(e) => update({ createdBefore: e.target.value })} />
              </Field>
            </div>
          </Section>
          <Section title="What">
            <Field label="Kinds" helper="None selected: every kind.">
              <Checks label="Kinds" options={kindOptions} value={filter.kinds} onChange={(kinds) => update({ kinds })} />
            </Field>
            <Field label="Has any of these tags">
              <TagInput tags={filter.tagsAny} onChange={(tagsAny) => update({ tagsAny })} suggestions={tagOptions} />
            </Field>
            <Field label="Has all of these tags">
              <TagInput tags={filter.tagsAll} onChange={(tagsAll) => update({ tagsAll })} suggestions={tagOptions} />
            </Field>
            <Field label="Has none of these tags">
              <TagInput tags={filter.tagsNone} onChange={(tagsNone) => update({ tagsNone })} suggestions={tagOptions} />
            </Field>
            <Field label="Uploaded from">
              <Checks label="Sources" options={sourceOptions} value={filter.sources} onChange={(sources) => update({ sources })} />
            </Field>
            <div className="field-row">
              <Field label="At least">
                <NumberInput value={filter.minSizeMb} onChange={(e) => update({ minSizeMb: e.target.value })} unit="MB" aria-label="At least MB" />
              </Field>
              <Field label="At most">
                <NumberInput value={filter.maxSizeMb} onChange={(e) => update({ maxSizeMb: e.target.value })} unit="MB" aria-label="At most MB" />
              </Field>
            </div>
            <Field label="Name, caption or tag contains">
              <SearchInput value={filter.q} onValueChange={(q) => update({ q })} label="Text" hideLabel placeholder="Search text" />
            </Field>
            <Field label="Viewed">
              <Select value={filter.seen} onChange={(e) => update({ seen: e.target.value as SeenFilter })}>
                <option value="">Any</option>
                <option value="never">Never opened by anyone</option>
                <option value="unseen">Not opened by me</option>
                <option value="seen">Opened by me</option>
              </Select>
            </Field>
          </Section>
          <Section title="Protected">
            <div className="fieldset">
              <Checkbox label="Also delete pinned artifacts" checked={filter.includePinned} onChange={(e) => update({ includePinned: e.target.checked })} />
              <Checkbox label="Also delete artifacts with live share links" checked={filter.includeShared} onChange={(e) => update({ includeShared: e.target.checked })} />
              <Checkbox label="Also delete captures of leases that are still live" checked={filter.includeLive} onChange={(e) => update({ includeLive: e.target.checked })} />
            </div>
          </Section>
        </form>
        <div className="cleanup-result" aria-live="polite" aria-busy={stale}>
          {done ? (
            <Notice title="Cleanup done">
              Deleted {plural(done.artifacts, "artifact")} ({formatSize(done.bytes)}){done.shares ? `, ${plural(done.shares, "share link")}` : ""}
              {done.sessions ? `, and removed ${plural(done.sessions, "empty session")}` : ""}.
            </Notice>
          ) : null}
          {preview.error ? <Notice variant="error">{errorText(preview.error, "preview", "the cleanup")}</Notice> : null}
          {data ? (
            <>
              <div className={`cleanup-total ${stale ? "is-stale" : ""}`}>
                <div>
                  <div className="cleanup-total-number">{formatCount(data.count)}</div>
                  <div className="ink-3">{data.count === 1 ? "artifact" : "artifacts"} match</div>
                </div>
                <div>
                  <div className="cleanup-total-number">{formatSize(data.bytes)}</div>
                  <div className="ink-3">to free</div>
                </div>
                <div className="cleanup-total-range">
                  {data.oldest ? (
                    <>
                      <AbsTime value={iso(data.oldest)} /> to <AbsTime value={iso(data.newest)} />
                    </>
                  ) : (
                    <span className="ink-3">Nothing matches.</span>
                  )}
                  {data.emptySessions ? <div className="ink-3">{plural(data.emptySessions, "session")} would be left empty</div> : null}
                </div>
                <Button variant="danger" disabled={!canDelete} onClick={() => setConfirm(true)}>
                  Delete {formatCount(data.count)}
                </Button>
              </div>
              {data.empty ? <Notice variant="attention">No conditions set: this shows everything you can edit. Set at least one condition to delete.</Notice> : null}
              {skipped.length ? (
                <p className="ink-2 cleanup-skipped">
                  Kept although they match: {skipped.map(([key, n]) => `${formatCount(n)} ${key === "live" ? "in live leases" : key}`).join(", ")}. Turn them on under Protected to include them.
                </p>
              ) : null}
              {data.count ? (
                <>
                  <div className="cleanup-breakdowns">
                    <Breakdown title="Projects" rows={data.projects} label={(r) => <Mono>{r.project}</Mono>} more={data.more.projects} total={data.count} />
                    <Breakdown title="Sessions" rows={data.sessions} label={(r) => (r.slug ? <Link to="/sessions/$slug" params={{ slug: r.slug }} className="link-quiet">{r.name}</Link> : <span className="muted">{r.name}</span>)} more={data.more.sessions} total={data.count} />
                    <Breakdown title="Kinds" rows={data.kinds} label={(r) => kindLabel(r.kind)} more={data.more.kinds} total={data.count} />
                    <Breakdown title="Tags" rows={data.tags} label={(r) => r.tag} more={data.more.tags} total={data.count} />
                  </div>
                  <Section
                    title="Sample"
                    count={data.sample.length < data.count ? `${data.sample.length} of ${formatCount(data.count)}` : data.sample.length}
                    actions={<Segmented<Order> label="Order" value={order} onChange={setOrder} options={[{ value: "oldest", label: "Oldest" }, { value: "newest", label: "Newest" }, { value: "largest", label: "Largest" }]} />}
                  >
                    <DataTable rows={data.sample} columns={columns} getId={(a) => a.id} getHref={(a) => localPath(a.url)} label="Matching artifacts" compact />
                  </Section>
                </>
              ) : (
                <EmptyState hint="Loosen a condition or include protected artifacts.">No artifacts match these conditions.</EmptyState>
              )}
            </>
          ) : preview.isLoading ? (
            <p className="ink-3">Counting...</p>
          ) : null}
        </div>
      </div>
      {confirm && data ? (
        <ConfirmDialog
          title={`Delete ${plural(data.count, "artifact")}?`}
          body={
            <>
              This permanently deletes {plural(data.count, "artifact")} ({formatSize(data.bytes)}) with their files and share links. It cannot be undone.
            </>
          }
          extra={
            data.emptySessions ? (
              <Checkbox label={`Also remove the ${plural(data.emptySessions, "session")} left empty (sessions with notes or live leases stay)`} checked={removeEmpty} onChange={(e) => setRemoveEmpty(e.target.checked)} />
            ) : null
          }
          typeToConfirm={data.count >= 100 ? String(data.count) : undefined}
          confirmLabel={`Delete ${formatCount(data.count)}`}
          busyLabel="Deleting"
          errorFor={(err) => errorText(err, "delete", "the artifacts")}
          onConfirm={apply}
          onClose={() => setConfirm(false)}
        />
      ) : null}
    </div>
  );
}
