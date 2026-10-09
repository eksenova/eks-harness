import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "../components/Button";
import { Checkbox, SearchInput } from "../components/Form";
import { useDebounced, useIsDesktop } from "../lib/hooks";

const LINE_HEIGHT = 20;
const OVERSCAN = 30;
const MAX_MATCHES = 20000;

export interface Match {
  line: number;
  start: number;
  end: number;
}

export function parseLineSpec(spec: string | undefined): [number, number] | null {
  if (!spec) return null;
  const match = spec.match(/^(\d+)(?:-(\d+))?$/);
  if (!match) return null;
  const a = Number(match[1]);
  const b = match[2] ? Number(match[2]) : a;
  return [Math.min(a, b), Math.max(a, b)];
}

export function findMatches(lines: string[], query: string, matchCase: boolean): Match[] {
  if (!query) return [];
  const needle = matchCase ? query : query.toLowerCase();
  const out: Match[] = [];
  for (let i = 0; i < lines.length && out.length < MAX_MATCHES; i += 1) {
    const hay = matchCase ? lines[i] : lines[i].toLowerCase();
    let from = 0;
    for (;;) {
      const index = hay.indexOf(needle, from);
      if (index < 0) break;
      out.push({ line: i, start: index, end: index + needle.length });
      from = index + Math.max(1, needle.length);
      if (out.length >= MAX_MATCHES) break;
    }
  }
  return out;
}

function renderLine(text: string, matches: Match[] | undefined, current: Match | null): ReactNode {
  if (!matches || !matches.length) return text || "​";
  const parts: ReactNode[] = [];
  let cursor = 0;
  matches.forEach((m, index) => {
    if (m.start > cursor) parts.push(text.slice(cursor, m.start));
    const isCurrent = current !== null && current.line === m.line && current.start === m.start;
    parts.push(
      <mark key={index} className={isCurrent ? "find-hit is-current" : "find-hit"}>
        {text.slice(m.start, m.end)}
      </mark>,
    );
    cursor = m.end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

interface TextContentProps {
  text: string;
  find: string;
  onFind: (value: string) => void;
  lineSpec?: string;
  onLine?: (spec: string | null) => void;
  lineHref?: (spec: string) => string;
  wrap: boolean;
  onWrap: (value: boolean) => void;
  follow?: boolean;
  onScrolledAway?: (away: boolean) => void;
  extraToolbar?: ReactNode;
  height?: string;
  compact?: boolean;
  label?: string;
}

export function TextContent({ text, find, onFind, lineSpec, onLine, lineHref, wrap, onWrap, follow, onScrolledAway, extraToolbar, height, compact, label = "Text" }: TextContentProps) {
  const lines = useMemo(() => {
    const split = text.split(/\r?\n/);
    if (split.length > 1 && split[split.length - 1] === "") split.pop();
    return split;
  }, [text]);
  const [findText, setFindText] = useState(find);
  const debouncedFind = useDebounced(findText, 200);
  const [matchCase, setMatchCase] = useState(false);
  const [currentIndex, setCurrentIndex] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const findRef = useRef<HTMLInputElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewHeight, setViewHeight] = useState(600);
  const desktop = useIsDesktop();
  const anchorLine = useRef<number | null>(null);
  const range = parseLineSpec(lineSpec);

  useEffect(() => setFindText(find), [find]);
  useEffect(() => {
    if (debouncedFind !== find) onFind(debouncedFind);
  }, [debouncedFind, find, onFind]);

  const matches = useMemo(() => findMatches(lines, debouncedFind, matchCase), [lines, debouncedFind, matchCase]);
  const byLine = useMemo(() => {
    const map = new Map<number, Match[]>();
    for (const m of matches) {
      const list = map.get(m.line);
      if (list) list.push(m);
      else map.set(m.line, [m]);
    }
    return map;
  }, [matches]);
  useEffect(() => setCurrentIndex(0), [debouncedFind, matchCase]);
  const current = matches.length ? matches[Math.min(currentIndex, matches.length - 1)] : null;

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const update = () => setViewHeight(node.clientHeight);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const scrollToLine = (line: number, position: "third" | "center" = "third") => {
    const node = scrollRef.current;
    if (!node) return;
    if (wrap) {
      const el = node.querySelector<HTMLElement>(`[data-line="${line}"]`);
      if (el) node.scrollTop = el.offsetTop - node.clientHeight / (position === "third" ? 3 : 2);
      return;
    }
    node.scrollTop = Math.max(0, line * LINE_HEIGHT - node.clientHeight / (position === "third" ? 3 : 2));
  };

  useEffect(() => {
    if (range && anchorLine.current === null) {
      anchorLine.current = range[0];
      window.setTimeout(() => scrollToLine(range[0] - 1), 0);
    }
  }, [range, lines.length]);

  useEffect(() => {
    if (current) scrollToLine(current.line, "center");
  }, [current?.line, current?.start]);

  useEffect(() => {
    if (!follow) return;
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [follow, lines.length]);

  const onScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    setScrollTop(node.scrollTop);
    if (onScrolledAway) onScrolledAway(node.scrollHeight - node.scrollTop - node.clientHeight > LINE_HEIGHT * 2);
  };

  const step = (delta: number) => {
    if (!matches.length) return;
    setCurrentIndex((i) => (i + delta + matches.length) % matches.length);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      findRef.current?.focus();
      findRef.current?.select();
    }
  };

  const gutterDigits = String(lines.length).length;
  const first = wrap ? 0 : Math.max(0, Math.floor(scrollTop / LINE_HEIGHT) - OVERSCAN);
  const last = wrap ? lines.length : Math.min(lines.length, Math.ceil((scrollTop + viewHeight) / LINE_HEIGHT) + OVERSCAN);

  const clickLine = (event: React.MouseEvent, n: number) => {
    event.preventDefault();
    if (!onLine) return;
    if (event.shiftKey && range) {
      const a = Math.min(range[0], n);
      const b = Math.max(range[1], n);
      onLine(a === b ? String(a) : `${a}-${b}`);
    } else if (range && range[0] === n && range[1] === n) {
      onLine(null);
    } else {
      onLine(String(n));
    }
  };

  const rows: ReactNode[] = [];
  for (let i = first; i < last; i += 1) {
    const n = i + 1;
    const highlighted = range !== null && n >= range[0] && n <= range[1];
    rows.push(
      <div key={i} data-line={i} className={`text-line ${highlighted ? "is-highlighted" : ""}`} style={wrap ? undefined : { top: i * LINE_HEIGHT }}>
        <span className="text-gutter" style={{ width: `calc(${gutterDigits}ch + var(--s-3) * 2)` }}>
          {onLine ? (
            <a href={lineHref ? lineHref(String(n)) : `#L${n}`} onClick={(event) => clickLine(event, n)} tabIndex={-1} aria-label={`Line ${n}`}>
              {n}
            </a>
          ) : (
            n
          )}
        </span>
        <span className="text-content">{renderLine(lines[i], byLine.get(i), current)}</span>
      </div>,
    );
  }

  return (
    <div className="text-viewer" onKeyDown={onKeyDown}>
      <div className="viewer-toolbar">
        <SearchInput
          ref={findRef}
          value={findText}
          onValueChange={setFindText}
          label="Find"
          placeholder="Find"
          className="find-input"
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              step(event.shiftKey ? -1 : 1);
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setFindText("");
              onFind("");
              (event.target as HTMLInputElement).blur();
            }
          }}
        />
        {debouncedFind ? <span className="muted">{matches.length ? `${currentIndex + 1} of ${matches.length.toLocaleString("en-US")}${matches.length >= MAX_MATCHES ? "+" : ""}` : "No matches"}</span> : null}
        {!compact ? (
          <>
            <Button onClick={() => step(-1)} disabled={!matches.length} title="Previous match (Shift+Enter)">
              Previous
            </Button>
            <Button onClick={() => step(1)} disabled={!matches.length} title="Next match (Enter)">
              Next
            </Button>
          </>
        ) : null}
        {desktop ? <Checkbox label="Match case" checked={matchCase} onChange={(event) => setMatchCase(event.target.checked)} /> : null}
        <Checkbox label="Wrap lines" checked={wrap} onChange={(event) => onWrap(event.target.checked)} />
        <span className="muted">{lines.length.toLocaleString("en-US")} lines</span>
        {extraToolbar}
      </div>
      <div ref={scrollRef} className={`text-frame ${wrap ? "is-wrapped" : ""}`} style={height ? { height } : undefined} onScroll={onScroll} tabIndex={0} role="region" aria-label={label}>
        <div className="text-body" style={wrap ? undefined : { height: lines.length * LINE_HEIGHT }}>
          {rows}
        </div>
      </div>
    </div>
  );
}
