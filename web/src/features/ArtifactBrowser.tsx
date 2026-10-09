import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { errorText } from "../api/client";
import { useArtifacts, useTags, type ArtifactParams } from "../api/queries";
import { ARTIFACT_KINDS, type ArtifactOut } from "../api/types";
import { Button, Segmented } from "../components/Button";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Checkbox, NumberInput, Radio, SearchInput, Select } from "../components/Form";
import { OverflowMenu, separator } from "../components/Menu";
import { Pagination, RelTime, TagInput, TagToken, Unseen, UnseenLegend } from "../components/Misc";
import { AnnotationBadge } from "./annotations";
import { EmptyState, ErrorState, Loading, Notice } from "../components/Notice";
import { DataTable, type Column } from "../components/Table";
import { dimensions, flatMeta, formatDuration, formatSize, kindLabel, metaNumber, plural, sourceLabel } from "../lib/format";
import { announce, isTypingTarget, overlayOpen, shortcutAllowed, useDebounced, useIsSmall, useKeydown } from "../lib/hooks";
import { useEventListener } from "../lib/events";
import { parseTagList, useTagLook } from "../lib/tags";
import { artifactUrl, hrefWith, localPath, LIST_CONTEXT_KEYS, pick, SHARED_CONTEXT_KEYS, sharedArtifactUrl, usePathname, useSearchParams, useSetParams } from "../lib/url";
import { bulkPatch, bulkRetention, bulkTag, collectAll, deleteArtifacts, deleteSummary, downloadZip, resolveArtifacts, setSeen } from "./artifactActions";
import { TagColorsDialog } from "./tagColors";

export const SORTS: { value: string; label: string; sort: string; dir: "asc" | "desc" }[] = [
  { value: "created:desc", label: "Newest first", sort: "created", dir: "desc" },
  { value: "created:asc", label: "Oldest first", sort: "created", dir: "asc" },
  { value: "name:asc", label: "Name A to Z", sort: "name", dir: "asc" },
  { value: "size:desc", label: "Largest first", sort: "size", dir: "desc" },
  { value: "kind:asc", label: "Kind", sort: "kind", dir: "asc" },
];

export function listSort(params: Record<string, string>): { sort: string; dir: "asc" | "desc" } {
  const sort = params.sort || "created";
  const natural = sort === "created" || sort === "size" ? "desc" : "asc";
  const dir = params.dir === "asc" || params.dir === "desc" ? params.dir : natural;
  return { sort, dir };
}

export function artifactDetails(artifact: ArtifactOut): string {
  const meta = flatMeta(artifact.meta);
  if (artifact.kind === "video" || artifact.durationMs) {
    if (artifact.durationMs) return formatDuration(artifact.durationMs);
  }
  const dims = dimensions(artifact.width, artifact.height);
  if (dims) return dims;
  if (artifact.kind === "har") {
    const count = metaNumber(meta, "requests", "requestCount", "entries");
    if (count !== null) return plural(count, "request");
  }
  if (artifact.kind === "site" || artifact.kind === "mhtml") {
    const count = metaNumber(meta, "fileCount", "files");
    if (count !== null) return plural(count, "file");
  }
  const lines = metaNumber(meta, "lines", "lineCount");
  if (lines !== null) return plural(lines, "line");
  return "";
}

const startIndex = new Map<string, number>();
const PAGE_SIZE = 100;

interface BrowserProps {
  projectId: string | null;
  sessionSlug: string | null;
  sessionName: string | null;
  canEdit: boolean;
  onUpload: () => void;
  emptyText: ReactNode;
  emptyHint?: ReactNode;
  uploadLabel?: string;
  onCountChange?: (count: number) => void;
  shared?: boolean;
}

export function ArtifactBrowser({ projectId, sessionSlug, sessionName, canEdit, onUpload, emptyText, emptyHint, uploadLabel = "Upload files", shared = false }: BrowserProps) {
  const params = useSearchParams();
  const setParams = useSetParams();
  const pathname = usePathname();
  const navigate = useNavigate();
  const client = useQueryClient();
  const small = useIsSmall();
  const { sort, dir } = listSort(params);
  const view = params.view === "list" ? "list" : "grid";
  const [text, setText] = useState(params.q ?? "");
  const debouncedText = useDebounced(text, 250);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filterRef = useRef<HTMLInputElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const [bulkResult, setBulkResult] = useState("");
  const [bulkError, setBulkError] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string[] | null>(null);
  const [tagDialog, setTagDialog] = useState<string[] | null>(null);
  const [retentionDialog, setRetentionDialog] = useState<string[] | null>(null);
  const [tagColorsOpen, setTagColorsOpen] = useState(false);
  const [localSelection, setLocalSelection] = useState<Set<string> | null>(null);
  const lastToggled = useRef<string | null>(null);
  const [frozen, setFrozen] = useState<Set<string> | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  const base: ArtifactParams = useMemo(
    () => ({
      project: projectId ?? undefined,
      session: sessionSlug ?? undefined,
      projectLevel: projectId !== null && sessionSlug === null,
      kind: params.kind,
      tag: params.tag,
      q: params.q,
      unseen: params.unseen === "1",
      sort,
      dir,
    }),
    [projectId, sessionSlug, params.kind, params.tag, params.q, params.unseen, sort, dir],
  );
  const filterKey = JSON.stringify(base);
  const listParams = useMemo(() => ({ ...base, cursor: params.cursor, limit: PAGE_SIZE, facets: true }), [base, params.cursor]);
  const query = useArtifacts(listParams);
  const tags = useTags(projectId, sessionSlug);

  useEffect(() => {
    if ((params.q ?? "") !== debouncedText) setParams({ q: debouncedText || null, cursor: null, sel: null }, { replace: true });
  }, [debouncedText]);

  const firstFilterKey = useRef(filterKey);
  useEffect(() => {
    if (firstFilterKey.current === filterKey) return;
    firstFilterKey.current = filterKey;
    setLocalSelection(null);
  }, [filterKey]);

  const fresh = useMemo(() => query.data?.items ?? [], [query.data]);
  const items = useMemo(() => (frozen ? fresh.filter((item) => frozen.has(item.id)) : fresh), [fresh, frozen]);
  const pending = frozen ? fresh.filter((item) => !frozen.has(item.id)).length : 0;
  const known = useMemo(() => new Map(fresh.map((item) => [item.id, item])), [fresh]);

  useEventListener((event) => {
    if (event.type !== "artifact.created" || (projectId !== null && event.projectId !== projectId)) return;
    const grid = gridRef.current;
    if (!grid) return;
    const top = grid.getBoundingClientRect().top;
    const scrolledAway = top < 0;
    const insertsAbove = sort === "created" && dir === "desc";
    if ((scrolledAway || !insertsAbove) && !frozen && sort === "created" && !params.cursor) {
      setFrozen(new Set(fresh.map((item) => item.id)));
    }
  });

  useEffect(() => {
    if (pending > 0) announce(`${pending} new ${pending === 1 ? "artifact" : "artifacts"}.`);
  }, [pending]);

  const urlSelection = useMemo(() => {
    if (!params.sel || params.sel === "all") return new Set<string>();
    return new Set(params.sel.split(",").filter(Boolean));
  }, [params.sel]);
  const allMatching = params.sel === "all";
  const selected = localSelection ?? urlSelection;
  const selectionCount = allMatching ? query.data?.total ?? selected.size : selected.size;
  const tooLarge = Boolean(localSelection && localSelection.size > 200);

  const writeSelection = useCallback(
    (next: Set<string>) => {
      if (next.size > 200) {
        setLocalSelection(next);
        setParams({ sel: null }, { replace: true });
      } else {
        setLocalSelection(null);
        setParams({ sel: next.size ? Array.from(next).join(",") : null }, { replace: true });
      }
    },
    [setParams],
  );

  const toggle = (id: string, extend: boolean) => {
    const next = new Set(allMatching ? items.map((i) => i.id) : selected);
    if (extend && lastToggled.current) {
      const a = items.findIndex((i) => i.id === lastToggled.current);
      const b = items.findIndex((i) => i.id === id);
      if (a >= 0 && b >= 0) {
        for (let i = Math.min(a, b); i <= Math.max(a, b); i += 1) next.add(items[i].id);
        writeSelection(next);
        lastToggled.current = id;
        return;
      }
    }
    if (next.has(id)) next.delete(id);
    else next.add(id);
    lastToggled.current = id;
    writeSelection(next);
  };

  const clearSelection = () => {
    setLocalSelection(null);
    setParams({ sel: null }, { replace: true });
  };

  const selectAllOnPage = (checked: boolean) => {
    if (!checked) {
      clearSelection();
      return;
    }
    const next = new Set(selected);
    for (const item of items) next.add(item.id);
    writeSelection(next);
  };

  const selectedIds = async (): Promise<string[]> => {
    if (allMatching) return (await collectAll(base)).map((item) => item.id);
    return Array.from(selected);
  };

  const runBulk = async (action: string, fn: (ids: string[]) => Promise<string>) => {
    setBusyAction(action);
    setBulkError("");
    try {
      const ids = await selectedIds();
      const result = await fn(ids);
      clearSelection();
      setBulkResult(result);
      announce(result);
      window.setTimeout(() => setBulkResult(""), 5000);
    } catch (err) {
      setBulkError(errorText(err, action, "the selected artifacts", projectId ?? undefined));
    } finally {
      setBusyAction(null);
    }
  };

  const zipName = sessionSlug ?? `${(projectId ?? "artifacts").replace("/", "-")}-files`;

  const onZip = () =>
    runBulk("download", async (ids) => {
      await downloadZip({ ids }, zipName);
      return `Downloaded ${plural(ids.length, "artifact")} as a zip.`;
    });

  const onSeen = (seen: boolean, ids?: string[]) =>
    ids
      ? setSeen(client, ids, seen, projectId).catch((err) => setBulkError(errorText(err, "update", "the artifact", projectId ?? undefined)))
      : runBulk("mark", async (list) => {
          await setSeen(client, list, seen, projectId);
          return seen ? `Marked ${plural(list.length, "artifact")} as seen.` : `Marked ${plural(list.length, "artifact")} as not seen.`;
        });

  const onPin = () =>
    runBulk("pin", async (ids) => {
      await bulkPatch(client, ids, { pinned: true }, projectId);
      return `Pinned ${plural(ids.length, "artifact")}.`;
    });

  const openDelete = async (ids?: string[]) => {
    if (ids) {
      setConfirmDelete(ids);
      return;
    }
    setConfirmDelete(await selectedIds());
  };

  const openTags = async () => setTagDialog(await selectedIds());
  const openRetention = async () => setRetentionDialog(await selectedIds());

  const pageKey = (cursor: string | undefined) => `${pathname}:${filterKey}:${cursor ?? ""}`;
  const pageStart = params.cursor ? startIndex.get(pageKey(params.cursor)) ?? null : 1;
  const hasPrevious = Boolean(query.data?.prevCursor);

  const goPage = (direction: "next" | "prev") => {
    const data = query.data;
    if (!data) return;
    const current = pageStart ?? 1;
    if (direction === "next" && data.nextCursor) {
      startIndex.set(pageKey(data.nextCursor), current + data.items.length);
      setParams({ cursor: data.nextCursor, sel: params.sel === "all" ? null : params.sel });
    } else if (direction === "prev" && data.prevCursor) {
      const start = Math.max(1, current - PAGE_SIZE);
      if (start === 1 && current <= PAGE_SIZE + 1 && pageStart !== null) {
        setParams({ cursor: null });
      } else {
        startIndex.set(pageKey(data.prevCursor), start);
        setParams({ cursor: data.prevCursor });
      }
    }
    window.scrollTo({ top: 0 });
  };

  const artifactHref = (item: ArtifactOut) =>
    shared && sessionSlug
      ? hrefWith(sharedArtifactUrl(sessionSlug, item.id), pick(params, SHARED_CONTEXT_KEYS))
      : hrefWith(artifactUrl(item.projectId, item.sessionSlug ?? sessionSlug, item.id), pick(params, LIST_CONTEXT_KEYS));
  const showProject = projectId === null;

  const activeFilters = [params.kind, params.tag, params.unseen, params.q].filter(Boolean).length;
  const clearFilters = () => {
    setText("");
    setParams({ q: null, kind: null, tag: null, unseen: null, cursor: null, sel: null });
  };

  const filterSummary = () => {
    const parts: string[] = [];
    if (params.kind) parts.push(`kind ${kindLabel(params.kind)}`);
    const chosen = parseTagList(params.tag);
    if (chosen.length) parts.push(`${chosen.length === 1 ? "tag" : "tags"} ${chosen.join(", ")}`);
    if (params.unseen) parts.push("unseen only");
    if (params.q) parts.push(`"${params.q}"`);
    return parts.join(" and ");
  };

  const facetTags = query.data?.facets?.tag;
  const tagOptions = (facetTags
    ? Object.entries(facetTags).map(([tag, count]) => ({ tag, count }))
    : tags.data && tags.data.length
      ? tags.data
      : Array.from(new Set(fresh.flatMap((i) => i.tags))).map((tag) => ({ tag, count: 0 }))
  ).sort((a, b) => a.tag.localeCompare(b.tag));
  const kindCounts = query.data?.facets?.kind ?? null;
  for (const chosen of parseTagList(params.tag)) if (!tagOptions.some((t) => t.tag === chosen)) tagOptions.push({ tag: chosen, count: 0 });

  useKeydown((event) => {
    if (!shortcutAllowed(event)) return;
    if (event.key === "v") {
      event.preventDefault();
      setParams({ view: view === "grid" ? "list" : null }, { replace: true });
    } else if (event.key === "f") {
      event.preventDefault();
      filterRef.current?.focus();
    } else if (event.key === "]" && query.data?.nextCursor) {
      event.preventDefault();
      goPage("next");
    } else if (event.key === "[" && hasPrevious) {
      event.preventDefault();
      goPage("prev");
    } else if (event.key === "Escape" && (selected.size || allMatching)) {
      event.preventDefault();
      clearSelection();
    } else if ((event.key === "s" || event.key === "x") && (selected.size || allMatching) && !gridRef.current?.contains(document.activeElement)) {
      event.preventDefault();
      if (event.key === "s") void onSeen(true);
      else void openDelete();
    }
  });

  const itemKey = (item: ArtifactOut, key: string): boolean => {
    if (key === "s") {
      if (selected.size || allMatching) void onSeen(true);
      else void onSeen(!item.seen, [item.id]);
      return true;
    }
    if (key === "p" && canEdit) {
      void bulkPatch(client, selected.size ? Array.from(selected) : [item.id], { pinned: !item.pinned }, projectId).catch((err) => setBulkError(errorText(err, "pin", "the artifact", projectId ?? undefined)));
      return true;
    }
    if (key === "x" && canEdit) {
      void openDelete(selected.size || allMatching ? undefined : [item.id]);
      return true;
    }
    if (key === "Escape" && (selected.size || allMatching)) {
      clearSelection();
      return true;
    }
    return false;
  };

  const hasItems = items.length > 0;
  const anyUnseen = items.some((item) => !item.seen);
  const showBulk = selected.size > 0 || allMatching;

  const toolbar = showBulk ? (
    <div className="bulk-bar" role="toolbar" aria-label="Bulk actions">
      <span className="bulk-count">{allMatching ? `All ${selectionCount.toLocaleString("en-US")} selected` : `${selectionCount.toLocaleString("en-US")} selected`}</span>
      <Button onClick={onZip} busy={busyAction === "download"} busyLabel={"Preparing zip…"}>
        Download as zip
      </Button>
      <Button onClick={() => void onSeen(true)} busy={busyAction === "mark"} busyLabel={"Marking…"}>
        Mark seen
      </Button>
      <Button onClick={() => void onSeen(false)} disabled={busyAction === "mark"}>
        Mark not seen
      </Button>
      {canEdit ? (
        <>
          <Button onClick={() => void openTags()}>{"Tag…"}</Button>
          <Button onClick={() => void openRetention()}>{"Retention…"}</Button>
          <Button onClick={onPin} busy={busyAction === "pin"} busyLabel={"Pinning…"}>
            Pin
          </Button>
          <Button onClick={() => void openDelete()}>
            Delete {plural(selectionCount, "artifact")}
          </Button>
        </>
      ) : null}
      {!allMatching && query.data?.total && selected.size >= items.length && query.data.total > selected.size && items.every((i) => selected.has(i.id)) ? (
        <button type="button" className="link" onClick={() => {
          setLocalSelection(null);
          setParams({ sel: "all" }, { replace: true });
        }}>
          Select all {query.data.total.toLocaleString("en-US")} matching
        </button>
      ) : null}
      {tooLarge ? <span>Selection is too large to keep in the link.</span> : null}
      <span className="toolbar-spacer" />
      <button type="button" className="link" onClick={clearSelection}>
        Clear selection
      </button>
    </div>
  ) : bulkResult ? (
    <div className="sheet-result">
      <span role="status">{bulkResult}</span>
    </div>
  ) : (
    <div className="sheet-controls">
      <div className="toolbar">
        <SearchInput ref={filterRef} value={text} onValueChange={setText} label="Filter by name, caption or tag" placeholder="Filter by name, caption or tag" className="sheet-search" />
        {!small ? (
          <FilterControls params={params} setParams={setParams} tagOptions={tagOptions} kindCounts={kindCounts} onManageTags={() => setTagColorsOpen(true)} />
        ) : (
          <Button onClick={() => setFiltersOpen(true)}>{activeFilters ? `Filters (${activeFilters})` : "Filters"}</Button>
        )}
        {activeFilters && !small ? (
          <button type="button" className="link" onClick={clearFilters}>
            Clear filters
          </button>
        ) : null}
        <span className="toolbar-spacer" />
        {anyUnseen && !small ? <UnseenLegend /> : null}
        <Segmented
          label="View"
          value={view}
          onChange={(value) => setParams({ view: value === "grid" ? null : value }, { replace: true })}
          options={[
            { value: "grid", label: "Sheet", icon: "grid", title: "Contact sheet (v)" },
            { value: "list", label: "List", icon: "list", title: "List (v)" },
          ]}
        />
      </div>
      {!small ? (
        <TagFilter options={tagOptions} selected={parseTagList(params.tag)} onChange={(next) => setParams({ tag: next.join(",") || null, cursor: null, sel: null })} onManage={() => setTagColorsOpen(true)} />
      ) : null}
    </div>
  );

  let content: ReactNode;
  if (query.isPending) content = <Loading what="artifacts" />;
  else if (query.isError && !query.data) content = <ErrorState message={errorText(query.error, "load", "artifacts")} onRetry={() => void query.refetch()} />;
  else if (!hasItems && activeFilters) {
    content = (
      <EmptyState action={<Button onClick={clearFilters}>Clear filters</Button>}>No artifacts match {filterSummary()}.</EmptyState>
    );
  } else if (!hasItems) {
    content = (
      <EmptyState action={canEdit ? <Button onClick={onUpload}>{uploadLabel}</Button> : undefined} hint={emptyHint}>
        {emptyText}
      </EmptyState>
    );
  } else if (view === "list") {
    content = (
      <ArtifactTable
        items={items}
        href={artifactHref}
        selection={{ selected: allMatching ? new Set(items.map((i) => i.id)) : selected, onToggle: toggle, onToggleAll: selectAllOnPage, onClear: clearSelection }}
        onKey={itemKey}
        canEdit={canEdit}
        onDelete={(item) => void openDelete([item.id])}
        onSeen={(item) => void onSeen(!item.seen, [item.id])}
        showProject={showProject}
      />
    );
  } else {
    content = (
      <ArtifactGrid
        items={items}
        href={artifactHref}
        selected={allMatching ? new Set(items.map((i) => i.id)) : selected}
        onToggle={toggle}
        onKey={itemKey}
        onOpen={(item) => void navigate({ to: artifactHref(item) })}
        canEdit={canEdit}
        onDelete={(item) => void openDelete([item.id])}
        onSeen={(item) => void onSeen(!item.seen, [item.id])}
        focusId={focusId}
        setFocusId={setFocusId}
        onSelectAll={() => selectAllOnPage(true)}
        showProject={showProject}
      />
    );
  }

  return (
    <div className="artifact-browser">
      {toolbar}
      {bulkError ? <Notice variant="error">{bulkError}</Notice> : null}
      {pending > 0 ? (
        <div className="new-notice">
          <Notice
            variant="info"
            action={
              <button
                type="button"
                className="link"
                onClick={() => {
                  setFrozen(null);
                  gridRef.current?.scrollIntoView({ block: "start" });
                  window.scrollTo({ top: 0 });
                }}
              >
                Show
              </button>
            }
          >
            {plural(pending, "new artifact")}.
          </Notice>
        </div>
      ) : null}
      <div ref={gridRef}>{content}</div>
      {hasItems || hasPrevious ? (
        <Pagination
          start={pageStart}
          count={items.length}
          total={query.data?.total ?? null}
          hasPrevious={hasPrevious}
          hasNext={Boolean(query.data?.nextCursor)}
          onPrevious={() => goPage("prev")}
          onNext={() => goPage("next")}
          loading={query.isFetching && query.isPlaceholderData}
        />
      ) : null}
      {filtersOpen ? (
        <Dialog
          title="Filters"
          onClose={() => setFiltersOpen(false)}
          bottomSheet
          footer={
            <>
              <Button onClick={clearFilters}>Clear filters</Button>
              <Button variant="primary" onClick={() => setFiltersOpen(false)}>
                Show {plural(query.data?.total ?? items.length, "artifact")}
              </Button>
            </>
          }
        >
          <div className="stack">
            <FilterControls params={params} setParams={setParams} tagOptions={tagOptions} kindCounts={kindCounts} onManageTags={() => setTagColorsOpen(true)} stacked />
          </div>
        </Dialog>
      ) : null}
      {confirmDelete ? (
        <DeleteArtifactsConfirm
          ids={confirmDelete}
          known={known}
          sessionName={sessionName}
          projectId={projectId}
          onClose={() => setConfirmDelete(null)}
          onDone={(count) => {
            clearSelection();
            const message = `Deleted ${plural(count, "artifact")}.`;
            setBulkResult(message);
            announce(message);
            window.setTimeout(() => setBulkResult(""), 5000);
          }}
        />
      ) : null}
      {tagColorsOpen ? <TagColorsDialog editable={canEdit} onClose={() => setTagColorsOpen(false)} /> : null}
      {retentionDialog ? (
        <RetentionDialog
          ids={retentionDialog}
          known={known}
          projectId={projectId}
          onClose={() => setRetentionDialog(null)}
          onDone={(message) => {
            clearSelection();
            setBulkResult(message);
            announce(message);
            window.setTimeout(() => setBulkResult(""), 5000);
          }}
        />
      ) : null}
      {tagDialog ? (
        <TagDialog
          ids={tagDialog}
          known={known}
          projectId={projectId}
          suggestions={tagOptions.map((t) => t.tag)}
          onClose={() => setTagDialog(null)}
          onDone={(message) => {
            clearSelection();
            setBulkResult(message);
            announce(message);
            window.setTimeout(() => setBulkResult(""), 5000);
          }}
        />
      ) : null}
    </div>
  );
}

const TAG_FILTER_PREVIEW = 12;

function TagFilter({ options, selected, onChange, onManage }: { options: { tag: string; count: number }[]; selected: string[]; onChange: (tags: string[]) => void; onManage: () => void }) {
  const look = useTagLook();
  const [expanded, setExpanded] = useState(false);
  const ordered = [...options].sort((a, b) => Number(look(b.tag).builtin) - Number(look(a.tag).builtin) || a.tag.localeCompare(b.tag));
  const shown = expanded ? ordered : ordered.filter((option, index) => index < TAG_FILTER_PREVIEW || selected.includes(option.tag));
  const toggle = (tag: string) => onChange(selected.includes(tag) ? selected.filter((t) => t !== tag) : [...selected, tag]);
  if (!ordered.length) return null;
  return (
    <div className="tag-filter" role="group" aria-label="Filter by tags (artifacts must have every selected tag)">
      <span className="tag-filter-label" aria-hidden="true">
        Tags
      </span>
      {shown.map((option) => {
        const tagLook = look(option.tag);
        return (
          <button
            key={option.tag}
            type="button"
            className="tag-filter-option"
            aria-pressed={selected.includes(option.tag)}
            data-colored={tagLook.color ? "" : undefined}
            style={tagLook.color ? ({ "--tag-color": tagLook.color } as CSSProperties) : undefined}
            title={tagLook.builtin ? `${tagLook.label}: ${tagLook.description}` : option.tag}
            onClick={() => toggle(option.tag)}
          >
            {tagLook.color ? <span className="tag-swatch" aria-hidden="true" /> : null}
            {tagLook.label}
            {option.count ? <span className="tag-filter-count">{option.count.toLocaleString("en-US")}</span> : null}
          </button>
        );
      })}
      {ordered.length > shown.length || expanded ? (
        <button type="button" className="link" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "Fewer tags" : `${ordered.length - shown.length} more`}
        </button>
      ) : null}
      <button type="button" className="link" onClick={onManage}>
        {"Tag colors…"}
      </button>
    </div>
  );
}

function FilterControls({ params, setParams, tagOptions, kindCounts, stacked, onManageTags }: { params: Record<string, string>; setParams: ReturnType<typeof useSetParams>; tagOptions: { tag: string; count: number }[]; kindCounts: Record<string, number> | null; stacked?: boolean; onManageTags: () => void }) {
  const sortValue = `${listSort(params).sort}:${listSort(params).dir}`;
  return (
    <div className={stacked ? "stack" : "cluster"}>
      <Select value={params.kind ?? ""} onChange={(event) => setParams({ kind: event.target.value || null, cursor: null, sel: null })} aria-label="Kind">
        <option value="">All kinds</option>
        {ARTIFACT_KINDS.map((kind) => (
          <option key={kind} value={kind}>
            {kindCounts ? `${kindLabel(kind)} (${(kindCounts[kind] ?? 0).toLocaleString("en-US")})` : kindLabel(kind)}
          </option>
        ))}
      </Select>
      <Checkbox label="Unseen only" checked={params.unseen === "1"} onChange={(event) => setParams({ unseen: event.target.checked ? "1" : null, cursor: null, sel: null })} />
      <Select
        value={sortValue}
        aria-label="Sort"
        onChange={(event) => {
          const option = SORTS.find((s) => s.value === event.target.value) ?? SORTS[0];
          const natural = option.sort === "created" || option.sort === "size" ? "desc" : "asc";
          if (option.value === "created:desc") setParams({ sort: null, dir: null, cursor: null });
          else setParams({ sort: option.sort, dir: option.dir === natural ? null : option.dir, cursor: null });
        }}
      >
        {SORTS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </Select>
      {stacked ? (
        <TagFilter options={tagOptions} selected={parseTagList(params.tag)} onChange={(next) => setParams({ tag: next.join(",") || null, cursor: null, sel: null })} onManage={onManageTags} />
      ) : null}
    </div>
  );
}

function tileMenu(item: ArtifactOut, canEdit: boolean, onSeen: () => void, onDelete: () => void) {
  const items = [
    { label: "Open raw file", onSelect: () => window.open(localPath(item.rawUrl), "_blank", "noopener") },
    { label: "Download", onSelect: () => window.location.assign(localPath(item.downloadUrl)) },
    { label: item.seen ? "Mark not seen" : "Mark seen", onSelect: onSeen },
  ];
  if (canEdit) return [...items, separator(), { label: "Delete…", danger: true, onSelect: onDelete }];
  return items;
}

function Thumb({ item, size }: { item: ArtifactOut; size: "tile" | "row" | "small" | "strip" }) {
  const [failed, setFailed] = useState(false);
  const hasThumb = Boolean(item.thumbnailUrl) && !failed;
  return (
    <span className={`thumb thumb--${size}`}>
      {hasThumb ? (
        <img src={localPath(item.thumbnailUrl)} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
      ) : size === "tile" || size === "strip" ? (
        <span className="thumb-kind">
          <span className="thumb-ext mono">.{item.filename.includes(".") ? item.filename.split(".").pop()?.slice(0, 6) : item.kind}</span>
          {size === "tile" ? <span className="thumb-kind-label">{kindLabel(item.kind)}</span> : null}
        </span>
      ) : null}
      {size === "tile" && item.durationMs ? <span className="thumb-duration">{formatDuration(item.durationMs)}</span> : null}
    </span>
  );
}

export { Thumb };

interface GridProps {
  items: ArtifactOut[];
  href: (item: ArtifactOut) => string;
  selected: Set<string>;
  onToggle: (id: string, extend: boolean) => void;
  onKey: (item: ArtifactOut, key: string) => boolean;
  onOpen: (item: ArtifactOut) => void;
  canEdit: boolean;
  onDelete: (item: ArtifactOut) => void;
  onSeen: (item: ArtifactOut) => void;
  focusId: string | null;
  setFocusId: (id: string | null) => void;
  onSelectAll: () => void;
  showProject: boolean;
}

function projectName(projectId: string): string {
  return projectId.slice(projectId.indexOf("/") + 1);
}

function ArtifactGrid({ items, href, selected, onToggle, onKey, onOpen, canEdit, onDelete, onSeen, focusId, setFocusId, onSelectAll, showProject }: GridProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [columns, setColumns] = useState(4);
  const [tileWidth, setTileWidth] = useState(200);
  const anySelected = selected.size > 0;

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const update = () => {
      const template = getComputedStyle(node).gridTemplateColumns.split(" ").filter(Boolean);
      setColumns(Math.max(1, template.length));
      setTileWidth(parseFloat(template[0] ?? "200") || 200);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const focusIndex = (index: number) => {
    const clamped = Math.max(0, Math.min(index, items.length - 1));
    const id = items[clamped]?.id;
    if (!id) return;
    setFocusId(id);
    window.setTimeout(() => ref.current?.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`)?.focus(), 0);
  };

  useKeydown((event) => {
    if (!shortcutAllowed(event) || !items.length) return;
    if (event.key !== "j" && event.key !== "ArrowDown") return;
    if (ref.current?.contains(document.activeElement)) return;
    if (document.activeElement && document.activeElement !== document.body && !(document.activeElement as HTMLElement).dataset.pageTitle) return;
    event.preventDefault();
    focusIndex(0);
  });

  const onKeyDown = (event: React.KeyboardEvent, item: ArtifactOut, index: number) => {
    if (isTypingTarget(event.target) || overlayOpen()) return;
    const mod = event.metaKey || event.ctrlKey;
    if (!mod && !event.altKey && onKey(item, event.key)) {
      event.preventDefault();
      return;
    }
    let target: number | null = null;
    switch (event.key) {
      case "ArrowRight":
        target = index + 1;
        break;
      case "ArrowLeft":
        target = index - 1;
        break;
      case "ArrowDown":
        target = index + columns;
        break;
      case "ArrowUp":
        target = index - columns;
        break;
      case "j":
        target = index + 1;
        break;
      case "k":
        target = index - 1;
        break;
      case "Home":
        target = 0;
        break;
      case "End":
        target = items.length - 1;
        break;
      case "Enter":
      case "o":
        event.preventDefault();
        if (mod) window.open(href(item), "_blank", "noopener");
        else onOpen(item);
        return;
      case " ":
        event.preventDefault();
        onToggle(item.id, event.shiftKey);
        return;
      case "a":
        if (mod) {
          event.preventDefault();
          onSelectAll();
        }
        return;
      default:
        return;
    }
    event.preventDefault();
    if (event.shiftKey && target !== null && items[target]) onToggle(items[target].id, true);
    focusIndex(target);
  };

  const rows: ArtifactOut[][] = [];
  for (let i = 0; i < items.length; i += columns) rows.push(items.slice(i, i + columns));
  const activeId = focusId && items.some((i) => i.id === focusId) ? focusId : items[0]?.id;

  return (
    <div ref={ref} className="sheet-grid" data-selecting={anySelected ? "" : undefined} role="grid" aria-label="Artifacts" aria-multiselectable="true">
      {rows.map((row, r) => (
        <div role="row" key={r} className="sheet-row">
          {row.map((item, c) => {
            const index = r * columns + c;
            const isSelected = selected.has(item.id);
            const caption = item.caption || item.filename;
            return (
              <div
                role="gridcell"
                key={item.id}
                data-id={item.id}
                aria-selected={isSelected}
                tabIndex={item.id === activeId ? 0 : -1}
                className="frame"
                data-unseen={item.seen ? undefined : ""}
                onFocus={() => setFocusId(item.id)}
                onKeyDown={(event) => onKeyDown(event, item, index)}
              >
                <Link to={href(item)} className="frame-well" tabIndex={-1} aria-label={caption}>
                  <Thumb item={item} size="tile" />
                </Link>
                <span className="frame-check">
                  <Checkbox hiddenLabel={`Select ${item.filename}`} checked={isSelected} tabIndex={-1} onClick={(event) => {
                    event.stopPropagation();
                    onToggle(item.id, event.shiftKey);
                  }} onChange={() => undefined} />
                </span>
                <span className="frame-more">
                  <OverflowMenu label={`More actions for ${item.filename}`} items={tileMenu(item, canEdit, () => onSeen(item), () => onDelete(item))} />
                </span>
                <Link to={href(item)} className="frame-name" tabIndex={-1} title={item.caption ? item.filename : undefined}>
                  {!item.seen ? <Unseen /> : null}
                  <span className="ellipsis">{caption}</span>
                </Link>
                <span className="frame-meta">
                  <span className="frame-meta-row">
                    {showProject ? <span className="frame-meta-strong" title={item.projectId}>{projectName(item.projectId)}</span> : null}
                    <span>{kindLabel(item.kind)}</span>
                    {item.pinned ? <span className="frame-meta-strong">pinned</span> : null}
                    {item.meta?.annotation ? <span>annotated</span> : null}
                  </span>
                  <span className="frame-meta-row">
                    {tileWidth >= 200 && item.durationMs ? <span>{formatDuration(item.durationMs)}</span> : null}
                    {tileWidth >= 200 && !item.durationMs && item.width && item.height ? <span>{dimensions(item.width, item.height)}</span> : null}
                    <span>{formatSize(item.size)}</span>
                    <RelTime value={item.createdAt} />
                  </span>
                </span>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function ArtifactTable({ items, href, selection, onKey, canEdit, onDelete, onSeen, showProject }: { items: ArtifactOut[]; href: (item: ArtifactOut) => string; selection: { selected: Set<string>; onToggle: (id: string, extend: boolean) => void; onToggleAll: (checked: boolean) => void; onClear: () => void }; onKey: GridProps["onKey"]; canEdit: boolean; onDelete: (item: ArtifactOut) => void; onSeen: (item: ArtifactOut) => void; showProject: boolean }) {
  const projectColumn: Column<ArtifactOut>[] = showProject ? [{ key: "project", header: "Project", width: "140px", render: (item) => projectName(item.projectId), title: (item) => item.projectId }] : [];
  const columns: Column<ArtifactOut>[] = [
    { key: "preview", header: <span className="visually-hidden">Preview</span>, width: "var(--h-control)", render: (item) => <Thumb item={item} size="row" /> },
    {
      key: "name",
      header: "Name",
      render: (item) => (
        <span className="cluster">
          <Link to={href(item)} className={`link-quiet ${item.seen ? "" : "strong"}`}>
            {!item.seen ? <Unseen /> : null}
            {item.caption || item.filename}
          </Link>{" "}
          <AnnotationBadge artifact={item} />
        </span>
      ),
      title: (item) => item.filename,
    },
    ...projectColumn,
    { key: "kind", header: "Kind", width: "120px", render: (item) => kindLabel(item.kind) },
    { key: "size", header: "Size", align: "right", width: "88px", render: (item) => formatSize(item.size) },
    { key: "details", header: "Details", align: "right", width: "120px", render: (item) => artifactDetails(item) },
    { key: "tags", header: "Tags", width: "160px", render: (item) => <span className="tag-list tag-list--nowrap">{item.tags.map((tag) => <TagToken key={tag} tag={tag} />)}</span>, title: (item) => item.tags.join(", ") },
    { key: "source", header: "Source", width: "72px", render: (item) => sourceLabel(item.source) },
    { key: "created", header: "Created", width: "120px", render: (item) => <RelTime value={item.createdAt} /> },
    { key: "more", header: <span className="visually-hidden">Actions</span>, width: "var(--h-control)", render: (item) => <OverflowMenu label={`More actions for ${item.filename}`} items={tileMenu(item, canEdit, () => onSeen(item), () => onDelete(item))} /> },
  ];
  return (
    <DataTable
      rows={items}
      columns={columns}
      getId={(item) => item.id}
      getHref={href}
      selection={selection}
      label="Artifacts"
      onRowKey={(item, key) => onKey(item, key)}
      mobileRow={(item) => (
        <div className="row-media">
          <Thumb item={item} size="small" />
          <div className="row-body">
            <div className="row-title">
              <Link to={href(item)} className={`link-quiet ${item.seen ? "" : "strong"}`}>
                {!item.seen ? <Unseen /> : null}
                {item.caption || item.filename}
              </Link>
            </div>
            <div className="row-meta">
              {showProject ? <span title={item.projectId}>{projectName(item.projectId)}</span> : null}
              <span>{kindLabel(item.kind)}</span>
              <AnnotationBadge artifact={item} />
              <span>{formatSize(item.size)}</span>
              <RelTime value={item.createdAt} />
            </div>
          </div>
        </div>
      )}
    />
  );
}

function DeleteArtifactsConfirm({ ids, known, sessionName, projectId, onClose, onDone }: { ids: string[]; known: Map<string, ArtifactOut>; sessionName: string | null; projectId: string | null; onClose: () => void; onDone: (count: number) => void }) {
  const client = useQueryClient();
  const [summary, setSummary] = useState<{ bytes: number; shares: number; count: number } | null>(null);
  const [single, setSingle] = useState<ArtifactOut | undefined>(ids.length === 1 ? known.get(ids[0]) : undefined);
  useEffect(() => {
    let cancelled = false;
    deleteSummary(ids)
      .then((result) => !cancelled && setSummary({ bytes: result.bytes, shares: result.shares, count: result.artifacts || ids.length }))
      .catch(() => {
        if (cancelled) return;
        const items = ids.map((id) => known.get(id)).filter(Boolean) as ArtifactOut[];
        setSummary({ bytes: items.reduce((s, i) => s + i.size, 0), shares: items.reduce((s, i) => s + (i.shareCount ?? 0), 0), count: ids.length });
      });
    if (ids.length === 1 && !known.has(ids[0])) {
      resolveArtifacts(ids, known)
        .then((items) => !cancelled && setSingle(items[0]))
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [ids, known]);
  const bytes = summary?.bytes ?? 0;
  const shares = summary?.shares ?? 0;
  const list = summary;
  const title = single ? `Delete ${single.filename}?` : `Delete ${plural(ids.length, "artifact")}?`;
  const where = sessionName ?? `${projectId} (project files)`;
  const body = !list
    ? `Counting…`
    : single
      ? `This deletes the file (${formatSize(single.size)})${single.shareCount ? ` and its ${plural(single.shareCount, "share link")}` : ""}. This cannot be undone.`
      : `This deletes ${plural(ids.length, "file")} (${formatSize(bytes)})${shares ? ` and ${plural(shares, "share link")}` : ""} in ${where}. This cannot be undone.`;
  return (
    <ConfirmDialog
      title={title}
      body={body}
      confirmLabel={single ? "Delete artifact" : `Delete ${plural(ids.length, "artifact")}`}
      busyLabel={"Deleting…"}
      onClose={onClose}
      errorFor={(err) => errorText(err, "delete", ids.length === 1 ? "the artifact" : "the artifacts", projectId ?? undefined)}
      onConfirm={async () => {
        await deleteArtifacts(client, ids, projectId);
        onDone(ids.length);
      }}
    />
  );
}

function TagDialog({ ids, known, projectId, suggestions, onClose, onDone }: { ids: string[]; known: Map<string, ArtifactOut>; projectId: string | null; suggestions: string[]; onClose: () => void; onDone: (message: string) => void }) {
  const client = useQueryClient();
  const [add, setAdd] = useState<string[]>([]);
  const [remove, setRemove] = useState<Set<string>>(new Set());
  const [present, setPresent] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    resolveArtifacts(ids, known)
      .then((items) => setPresent(Array.from(new Set(items.flatMap((i) => i.tags))).sort()))
      .catch(() => setPresent([]));
  }, [ids, known]);
  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      await bulkTag(client, ids, add, Array.from(remove), projectId);
      onDone(`Tagged ${plural(ids.length, "artifact")}.`);
      onClose();
    } catch (err) {
      setError(errorText(err, "tag", "the artifacts", projectId ?? undefined));
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={`Add tags to ${plural(ids.length, "artifact")}`}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void apply()}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Applying…"} disabled={!add.length && !remove.size}>
            Apply
          </Button>
        </>
      }
    >
      <div className="stack">
        <div className="field">
          <span className="field-label">Add tags</span>
          <TagInput tags={add} onChange={setAdd} suggestions={suggestions} />
        </div>
        {present.length ? (
          <fieldset className="fieldset">
            <legend className="field-label">Remove tags</legend>
            {present.map((tag) => (
              <Checkbox
                key={tag}
                label={tag}
                checked={remove.has(tag)}
                onChange={(event) =>
                  setRemove((prev) => {
                    const next = new Set(prev);
                    if (event.target.checked) next.add(tag);
                    else next.delete(tag);
                    return next;
                  })
                }
              />
            ))}
          </fieldset>
        ) : null}
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

function RetentionDialog({ ids, known, projectId, onClose, onDone }: { ids: string[]; known: Map<string, ArtifactOut>; projectId: string | null; onClose: () => void; onDone: (message: string) => void }) {
  const client = useQueryClient();
  const first = ids.length === 1 ? known.get(ids[0]) : undefined;
  const [mode, setMode] = useState<"inherit" | "days">(first?.retentionDays ? "days" : "inherit");
  const [days, setDays] = useState(String(first?.retentionDays ?? 30));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const value = Number(days);
  const invalid = mode === "days" && (!Number.isInteger(value) || value < 1 || value > 36500);
  const apply = async () => {
    if (invalid) return;
    setBusy(true);
    setError("");
    try {
      await bulkRetention(client, ids, mode === "days" ? value : null, projectId);
      onDone(mode === "days" ? `${plural(ids.length, "artifact")} will be deleted ${plural(value, "day")} after creation unless pinned.` : `${plural(ids.length, "artifact")} follow the project retention again.`);
      onClose();
    } catch (err) {
      setError(errorText(err, "change", "the retention", projectId ?? undefined));
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={`Retention of ${plural(ids.length, "artifact")}`}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void apply()}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Applying…"} disabled={invalid}>
            Apply
          </Button>
        </>
      }
    >
      <fieldset className="fieldset stack">
        <legend className="field-label">Keep these artifacts</legend>
        <Radio name="retention" label="As the project says" helper="Project retention, or the global default when the project has none." checked={mode === "inherit"} onChange={() => setMode("inherit")} />
        <Radio name="retention" label="For a set number of days" checked={mode === "days"} onChange={() => setMode("days")} />
        <div className="retention-row">
          <NumberInput aria-label="Days" value={days} unit="days" disabled={mode !== "days"} invalid={invalid} onChange={(event) => setDays(event.target.value.replace(/[^\d]/g, ""))} />
        </div>
        <p className="field-helper">Pin an artifact to keep it forever.</p>
      </fieldset>
      {error ? <Notice variant="error">{error}</Notice> : null}
    </Dialog>
  );
}
