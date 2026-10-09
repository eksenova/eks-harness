import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "../components/Button";
import { SearchInput } from "../components/Form";
import { Icon } from "../components/Icon";
import { useCopy, useDebounced } from "../lib/hooks";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Segment = string | number;

interface Row {
  kind: "node" | "more";
  path: Segment[];
  pathKey: string;
  depth: number;
  label: Segment | null;
  value: Json;
  shown?: number;
  total?: number;
  parentKey: string | null;
}

const CHUNK = 100;

function keyOf(path: Segment[]): string {
  return JSON.stringify(path);
}

function isContainer(value: Json): value is Json[] | { [key: string]: Json } {
  return value !== null && typeof value === "object";
}

function jsonPath(path: Segment[]): string {
  let out = "$";
  for (const segment of path) {
    if (typeof segment === "number") out += `[${segment}]`;
    else if (/^[A-Za-z_$][\w$]*$/.test(segment)) out += `.${segment}`;
    else out += `[${JSON.stringify(segment)}]`;
  }
  return out;
}

function summary(value: Json): string {
  if (Array.isArray(value)) return `[${value.length.toLocaleString("en-US")} ${value.length === 1 ? "item" : "items"}]`;
  if (value && typeof value === "object") {
    const count = Object.keys(value).length;
    return `{${count.toLocaleString("en-US")} ${count === 1 ? "key" : "keys"}}`;
  }
  return "";
}

function primitiveText(value: Json): string {
  if (typeof value === "string") return JSON.stringify(value);
  return String(value);
}

function initialExpanded(root: Json): Set<string> {
  const set = new Set<string>();
  const walk = (value: Json, path: Segment[], depth: number) => {
    if (!isContainer(value) || depth > 1) return;
    if (Array.isArray(value) && value.length > CHUNK) return;
    set.add(keyOf(path));
    const entries: [Segment, Json][] = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    for (const [k, v] of entries.slice(0, CHUNK)) walk(v, [...path, k], depth + 1);
  };
  walk(root, [], 0);
  return set;
}

function matchPaths(root: Json, query: string): { hits: Set<string>; ancestors: Set<string> } {
  const hits = new Set<string>();
  const ancestors = new Set<string>();
  if (!query) return { hits, ancestors };
  const needle = query.toLowerCase();
  let budget = 200_000;
  const walk = (value: Json, path: Segment[], label: Segment | null): boolean => {
    if (budget-- <= 0) return false;
    let matched = label !== null && String(label).toLowerCase().includes(needle);
    if (!isContainer(value)) matched = matched || primitiveText(value).toLowerCase().includes(needle);
    let childMatched = false;
    if (isContainer(value)) {
      const entries: [Segment, Json][] = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
      for (const [k, v] of entries) if (walk(v, [...path, k], k)) childMatched = true;
    }
    if (matched) hits.add(keyOf(path));
    if (childMatched) ancestors.add(keyOf(path));
    return matched || childMatched;
  };
  walk(root, [], null);
  return { hits, ancestors };
}

function highlight(text: string, query: string): ReactNode {
  if (!query) return text;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: ReactNode[] = [];
  let cursor = 0;
  let index = lower.indexOf(needle);
  let n = 0;
  while (index >= 0) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(
      <mark key={n++} className="find-hit">
        {text.slice(index, index + needle.length)}
      </mark>,
    );
    cursor = index + needle.length;
    index = lower.indexOf(needle, cursor);
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

export function JsonTree({ data, find, onFind }: { data: unknown; find: string; onFind: (value: string) => void }) {
  const root = data as Json;
  const [expanded, setExpanded] = useState<Set<string>>(() => initialExpanded(root));
  const [shown, setShown] = useState<Map<string, number>>(new Map());
  const [findText, setFindText] = useState(find);
  const debounced = useDebounced(findText, 250);
  const [focusKey, setFocusKey] = useState<string>(keyOf([]));
  const [copied, copy] = useCopy();
  const treeRef = useRef<HTMLDivElement>(null);

  useEffect(() => setFindText(find), [find]);
  useEffect(() => {
    if (debounced !== find) onFind(debounced);
  }, [debounced, find, onFind]);

  const matches = useMemo(() => matchPaths(root, debounced), [root, debounced]);
  useEffect(() => {
    if (!matches.ancestors.size) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      for (const key of matches.ancestors) next.add(key);
      return next;
    });
  }, [matches]);

  const rows = useMemo(() => {
    const out: Row[] = [];
    const walk = (value: Json, path: Segment[], depth: number, label: Segment | null, parentKey: string | null) => {
      const pathKey = keyOf(path);
      out.push({ kind: "node", path, pathKey, depth, label, value, parentKey });
      if (!isContainer(value) || !expanded.has(pathKey)) return;
      const entries: [Segment, Json][] = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
      const limit = Array.isArray(value) ? shown.get(pathKey) ?? CHUNK : entries.length;
      for (const [k, v] of entries.slice(0, limit)) walk(v, [...path, k], depth + 1, k, pathKey);
      if (entries.length > limit) out.push({ kind: "more", path, pathKey: `${pathKey}:more`, depth: depth + 1, label: null, value: null, shown: limit, total: entries.length, parentKey: pathKey });
    };
    walk(root, [], 0, null, null);
    return out;
  }, [root, expanded, shown]);

  const toggle = (key: string, open?: boolean) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      const willOpen = open ?? !next.has(key);
      if (willOpen) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const expandAll = () => {
    const all = new Set<string>();
    let budget = 50_000;
    const walk = (value: Json, path: Segment[]) => {
      if (!isContainer(value) || budget-- <= 0) return;
      all.add(keyOf(path));
      const entries: [Segment, Json][] = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
      for (const [k, v] of entries.slice(0, CHUNK)) walk(v, [...path, k]);
    };
    walk(root, []);
    setExpanded(all);
  };

  useEffect(() => {
    treeRef.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(focusKey)}"]`)?.setAttribute("tabindex", "0");
  }, [focusKey, rows]);

  const focusRow = (key: string) => {
    setFocusKey(key);
    window.setTimeout(() => treeRef.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`)?.focus(), 0);
  };

  const onKeyDown = (event: React.KeyboardEvent, row: Row, index: number) => {
    const container = isContainer(row.value) && row.kind === "node";
    const open = expanded.has(row.pathKey);
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (rows[index + 1]) focusRow(rows[index + 1].pathKey);
        break;
      case "ArrowUp":
        event.preventDefault();
        if (rows[index - 1]) focusRow(rows[index - 1].pathKey);
        break;
      case "ArrowRight":
        event.preventDefault();
        if (container && !open) toggle(row.pathKey, true);
        else if (container && rows[index + 1]) focusRow(rows[index + 1].pathKey);
        break;
      case "ArrowLeft":
        event.preventDefault();
        if (container && open) toggle(row.pathKey, false);
        else if (row.parentKey) focusRow(row.parentKey);
        break;
      case "Enter":
        event.preventDefault();
        if (row.kind === "more") setShown((m) => new Map(m).set(row.parentKey!, (row.shown ?? CHUNK) + CHUNK));
        else if (container) toggle(row.pathKey);
        break;
      case "*": {
        event.preventDefault();
        const siblings = rows.filter((r) => r.parentKey === row.parentKey && r.kind === "node" && isContainer(r.value));
        setExpanded((prev) => {
          const next = new Set(prev);
          for (const s of siblings) next.add(s.pathKey);
          return next;
        });
        break;
      }
      default:
        break;
    }
  };

  return (
    <div className="json-viewer">
      <div className="viewer-toolbar">
        <Button onClick={expandAll}>Expand all</Button>
        <Button onClick={() => setExpanded(new Set())}>Collapse all</Button>
        <SearchInput value={findText} onValueChange={setFindText} label="Find key or value" placeholder="Find key or value" className="find-input" />
        {debounced ? <span className="muted">{matches.hits.size ? `${matches.hits.size.toLocaleString("en-US")} matches` : "No matches"}</span> : null}
      </div>
      <div ref={treeRef} className="json-tree" role="tree" aria-label="JSON">
        {rows.map((row, index) => {
          if (row.kind === "more") {
            return (
              <div key={row.pathKey} role="treeitem" data-key={row.pathKey} tabIndex={row.pathKey === focusKey ? 0 : -1} className="json-row" style={{ paddingLeft: `calc(var(--s-4) * ${row.depth} + var(--s-4))` }} onKeyDown={(event) => onKeyDown(event, row, index)}>
                <button type="button" className="link" onClick={() => setShown((m) => new Map(m).set(row.parentKey!, (row.shown ?? CHUNK) + CHUNK))}>
                  Show {Math.min(CHUNK, (row.total ?? 0) - (row.shown ?? 0))} more of {((row.total ?? 0) - (row.shown ?? 0)).toLocaleString("en-US")}
                </button>
              </div>
            );
          }
          const container = isContainer(row.value);
          const open = expanded.has(row.pathKey);
          const hit = matches.hits.has(row.pathKey);
          return (
            <div
              key={row.pathKey}
              role="treeitem"
              aria-expanded={container ? open : undefined}
              aria-level={row.depth + 1}
              data-key={row.pathKey}
              tabIndex={row.pathKey === focusKey ? 0 : -1}
              className={`json-row ${hit ? "is-hit" : ""}`}
              style={{ paddingLeft: `calc(var(--s-4) * ${row.depth})` }}
              onKeyDown={(event) => onKeyDown(event, row, index)}
              onFocus={() => setFocusKey(row.pathKey)}
            >
              <span className="json-toggle">
                {container ? (
                  <button type="button" className="json-disclosure" tabIndex={-1} aria-label={open ? "Collapse" : "Expand"} onClick={() => toggle(row.pathKey)}>
                    <Icon name="chevron-down" className={open ? "" : "is-collapsed"} />
                  </button>
                ) : null}
              </span>
              <span className="json-text">
                {row.label !== null ? (
                  <span className="json-key">{highlight(typeof row.label === "number" ? String(row.label) : JSON.stringify(row.label), debounced)}: </span>
                ) : null}
                {container ? <span className="muted">{summary(row.value)}</span> : <span className="json-value">{highlight(primitiveText(row.value), debounced)}</span>}
              </span>
              <span className="json-actions">
                <button type="button" className="link" tabIndex={-1} onClick={() => void copy(jsonPath(row.path), `path:${row.pathKey}`)}>
                  {copied === `path:${row.pathKey}` ? "Copied" : "Copy path"}
                </button>
                <button type="button" className="link" tabIndex={-1} onClick={() => void copy(container ? JSON.stringify(row.value, null, 2) : typeof row.value === "string" ? row.value : String(row.value), `value:${row.pathKey}`)}>
                  {copied === `value:${row.pathKey}` ? "Copied" : "Copy value"}
                </button>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
