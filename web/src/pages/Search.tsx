import { EvidenceTabs } from "../features/EvidenceTabs";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useRef, type ReactNode } from "react";
import { errorText } from "../api/client";
import { useProjects, useSearch } from "../api/queries";
import { ARTIFACT_KINDS, type SearchHitOut } from "../api/types";
import { Button } from "../components/Button";
import { SearchInput, Select } from "../components/Form";
import { RelTime, Unseen } from "../components/Misc";
import { EmptyState, ErrorState, Loading } from "../components/Notice";
import { Thumb } from "../features/ArtifactBrowser";
import { formatDuration, formatSize, kindLabel } from "../lib/format";
import { shortcutAllowed, useKeydown, useTitle } from "../lib/hooks";
import { artifactUrl, localPath, projectUrl, sessionUrl, useSearchParams, useSetParams, useSyncedText } from "../lib/url";

const PAGE = 100;

function emphasize(text: string, terms: string[]): ReactNode {
  const lower = text.toLowerCase();
  for (const term of terms) {
    const index = lower.indexOf(term);
    if (index >= 0) {
      return (
        <>
          {text.slice(0, index)}
          <span className="strong">{text.slice(index, index + term.length)}</span>
          {text.slice(index + term.length)}
        </>
      );
    }
  }
  return text;
}

export function SearchPage() {
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const [text, setText] = useSyncedText("q", 300, { cursor: null });
  const q = (params.q ?? "").trim();
  useTitle(q ? `Search: ${q}` : "Search");
  const projects = useProjects();
  const search = useSearch({ q, kind: params.kind, project: params.project }, q.length > 0);
  const listRef = useRef<HTMLUListElement>(null);
  const offset = Math.max(0, Number(params.cursor) || 0);
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);

  const hits = useMemo(() => {
    let items = search.data?.items ?? [];
    if (params.kind) items = items.filter((hit) => hit.artifact.kind === params.kind);
    if (params.project) items = items.filter((hit) => hit.artifact.projectId === params.project);
    return items;
  }, [search.data, params.kind, params.project]);
  const page = hits.slice(offset, offset + PAGE);

  const open = (hit: SearchHitOut) => void navigate({ to: localPath(hit.artifact.url) || artifactUrl(hit.artifact.projectId, hit.artifact.sessionSlug, hit.artifact.id) });

  const focusItem = (index: number) => {
    const items = listRef.current?.querySelectorAll<HTMLElement>("[data-row]");
    items?.[Math.max(0, Math.min(index, items.length - 1))]?.focus();
  };

  useKeydown((event) => {
    if (!shortcutAllowed(event) || !page.length) return;
    if ((event.key === "j" || event.key === "ArrowDown") && !listRef.current?.contains(document.activeElement)) {
      event.preventDefault();
      focusItem(0);
    } else if (event.key === "]" && offset + PAGE < hits.length) {
      setParams({ cursor: String(offset + PAGE) });
    } else if (event.key === "[" && offset > 0) {
      setParams({ cursor: offset - PAGE > 0 ? String(offset - PAGE) : null });
    }
  });

  let body: ReactNode;
  if (!q) body = <EmptyState>Search filenames, captions, tags, session names and project ids.</EmptyState>;
  else if (search.isPending) body = <Loading what="results" />;
  else if (search.isError && !search.data) body = <ErrorState message={errorText(search.error, "search", "artifacts")} onRetry={() => void search.refetch()} />;
  else if (!hits.length)
    body = (
      <EmptyState action={params.kind || params.project ? <Button onClick={() => setParams({ kind: null, project: null, cursor: null })}>Clear filters</Button> : undefined}>
        No artifacts match "{q}".
      </EmptyState>
    );
  else
    body = (
      <>
        <p className="result-count">
          {hits.length.toLocaleString("en-US")} {hits.length === 1 ? "result" : "results"} for "{q}"
        </p>
        <ul ref={listRef} className="list-rows search-results" aria-label="Search results">
          {page.map((hit, index) => {
            const a = hit.artifact;
            const href = localPath(a.url) || artifactUrl(a.projectId, a.sessionSlug, a.id);
            const captionMatch = a.caption && terms.some((t) => a.caption.toLowerCase().includes(t));
            const tagMatches = a.tags.filter((tag) => terms.some((t) => tag.toLowerCase().includes(t)));
            return (
              <li
                key={a.id}
                data-row=""
                tabIndex={index === 0 ? 0 : -1}
                className="search-row"
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest("a, button")) return;
                  open(hit);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === "o") {
                    event.preventDefault();
                    open(hit);
                  } else if (event.key === "j" || event.key === "ArrowDown") {
                    event.preventDefault();
                    focusItem(index + 1);
                  } else if (event.key === "k" || event.key === "ArrowUp") {
                    event.preventDefault();
                    focusItem(index - 1);
                  }
                }}
              >
                <Link to={href} tabIndex={-1} aria-hidden="true">
                  <Thumb item={a} size="small" />
                </Link>
                <div className="search-body">
                  <div className="row-title">
                    <Link to={href} className={`link-quiet ${a.seen ? "" : "strong"}`} tabIndex={-1}>
                      {!a.seen ? <Unseen /> : null}
                      {a.filename}
                    </Link>
                    <RelTime value={a.createdAt} />
                  </div>
                  <div className="row-meta">
                    <Link to={projectUrl(a.projectId)} className="link-quiet" tabIndex={-1}>
                      {a.projectId}
                    </Link>
                    {a.sessionSlug && a.sessionSlug !== "_project" ? (
                      <Link to={sessionUrl(a.projectId, a.sessionSlug)} className="link-quiet" tabIndex={-1}>
                        {a.sessionName ?? a.sessionSlug}
                      </Link>
                    ) : (
                      <span>Project files</span>
                    )}
                  </div>
                  <div className="row-meta">
                    <span aria-label={`Kind: ${kindLabel(a.kind)}`}>{kindLabel(a.kind)}</span>
                    {a.durationMs ? <span>{formatDuration(a.durationMs)}</span> : null}
                    <span aria-label={`Size: ${formatSize(a.size)}`}>{formatSize(a.size)}</span>
                    {tagMatches.map((tag) => (
                      <span key={tag}>Tag: {emphasize(tag, terms)}</span>
                    ))}
                    {captionMatch ? <span>Caption: "{emphasize(a.caption, terms)}"</span> : null}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <div className="pagination">
          <span className="pagination-range">
            {offset + 1}-{Math.min(hits.length, offset + PAGE)} of {hits.length.toLocaleString("en-US")}
          </span>
          <span className="button-row">
            <Button disabled={offset === 0} onClick={() => setParams({ cursor: offset - PAGE > 0 ? String(offset - PAGE) : null })}>
              Previous
            </Button>
            <Button disabled={offset + PAGE >= hits.length} onClick={() => setParams({ cursor: String(offset + PAGE) })}>
              Next
            </Button>
          </span>
        </div>
      </>
    );

  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title" tabIndex={-1} data-page-title="">
          Evidence
        </h1>
      </div>
      <EvidenceTabs active="search" />
      <form
        className="toolbar page-tools search-bar"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          setParams({ q: text.trim() || null, cursor: null });
        }}
      >
        <SearchInput value={text} onValueChange={setText} label="Search artifacts" placeholder="Search artifacts" className="search-large" autoFocus />
        <Select value={params.kind ?? ""} onChange={(event) => setParams({ kind: event.target.value || null, cursor: null })} aria-label="Kind">
          <option value="">All kinds</option>
          {ARTIFACT_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kindLabel(kind)}
            </option>
          ))}
        </Select>
        <Select value={params.project ?? ""} onChange={(event) => setParams({ project: event.target.value || null, cursor: null })} aria-label="Project">
          <option value="">All projects</option>
          {(projects.data?.items ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.id}
            </option>
          ))}
        </Select>
      </form>
      {body}
    </div>
  );
}
