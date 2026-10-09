import { Link } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ApiError, errorText } from "../api/client";
import { formatAbsolute, formatFull, formatRelative, toDate } from "../lib/format";
import { isMac, useCopy, useIsSmall, useMinute } from "../lib/hooks";
import { useTagLook } from "../lib/tags";
import { Button } from "./Button";
import { Icon } from "./Icon";
import { EmptyState, ErrorState, Loading } from "./Notice";

export function Unseen() {
  return <span className="unseen" role="img" aria-label="Not seen" />;
}

export function UnseenLegend() {
  return (
    <span className="ink-3" style={{ fontSize: "var(--fs-12)" }}>
      <span className="unseen" aria-hidden="true" />
      not seen by you
    </span>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="mono" title={title}>
      {children}
    </span>
  );
}

export function RelTime({ value, empty = "" }: { value: string | null | undefined; empty?: string }) {
  const now = useMinute();
  const date = toDate(value);
  if (!date) return <>{empty}</>;
  return (
    <time dateTime={date.toISOString()} title={formatFull(date)} className="num">
      {formatRelative(date, now)}
    </time>
  );
}

export function AbsTime({ value, full = false, empty = "" }: { value: string | null | undefined; full?: boolean; empty?: string }) {
  const now = useMinute();
  const date = toDate(value);
  if (!date) return <>{empty}</>;
  return (
    <time dateTime={date.toISOString()} title={formatFull(date)} className="num">
      {full ? formatFull(date) : formatAbsolute(date, now)}
    </time>
  );
}

export function CopyField({ value, mono = true, label, autoSelect = false }: { value: string; mono?: boolean; label?: string; autoSelect?: boolean }) {
  const [state, copy] = useCopy();
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (autoSelect) {
      ref.current?.focus();
      ref.current?.select();
    }
  }, [autoSelect, value]);
  const failed = state?.startsWith("fail");
  return (
    <span className="copy-field">
      <input ref={ref} className={`input ${mono ? "mono" : ""}`} readOnly value={value} aria-label={label ?? "Value to copy"} onFocus={(event) => event.currentTarget.select()} />
      <Button
        onClick={async () => {
          await copy(value);
          ref.current?.select();
        }}
      >
        {state === "default" ? "Copied" : failed ? (isMac() ? "Press Cmd+C" : "Press Ctrl+C") : "Copy"}
      </Button>
    </span>
  );
}

export function CopyLink({ value, label = "Copy", className }: { value: string; label?: string; className?: string }) {
  const [state, copy] = useCopy();
  return (
    <button type="button" className={`link copy-action ${className ?? ""}`} onClick={() => void copy(value)}>
      {state === "default" ? "Copied" : state?.startsWith("fail") ? "Copy failed" : label}
    </button>
  );
}

export interface DefItem {
  label: string;
  value: ReactNode;
  copy?: string;
  mono?: boolean;
  title?: string;
}

export function DefList({ items, compact }: { items: (DefItem | null | false | undefined)[]; compact?: boolean }) {
  return (
    <dl className={`dl ${compact ? "dl--compact" : ""}`}>
      {items.filter(Boolean).map((raw) => {
        const item = raw as DefItem;
        return (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd>
              <span className={item.mono ? "mono" : undefined} title={item.title}>
                {item.value}
              </span>
              {item.copy ? <CopyLink value={item.copy} /> : null}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export interface Crumb {
  label: string;
  to: string;
}

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  const small = useIsSmall();
  if (!items.length) return null;
  const shown = small ? items.slice(-1) : items;
  return (
    <nav aria-label="Breadcrumb" className="crumbs">
      <ol>
        {shown.map((item) => (
          <li key={item.to}>
            <Link to={item.to}>{small ? `Back to ${item.label}` : item.label}</Link>
          </li>
        ))}
      </ol>
    </nav>
  );
}

export interface TabItem {
  label: string;
  to: string;
  count?: number | null;
  active: boolean;
  search?: Record<string, string>;
}

export const EXACT_ACTIVE = { exact: true, includeSearch: true } as const;
export const NO_ACTIVE_PROPS = {};

export function Tabs({ items, label, sub }: { items: TabItem[]; label: string; sub?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const links = Array.from(ref.current?.querySelectorAll<HTMLAnchorElement>("a") ?? []);
    const index = links.indexOf(document.activeElement as HTMLAnchorElement);
    if (index < 0) return;
    event.preventDefault();
    links[(index + (event.key === "ArrowRight" ? 1 : -1) + links.length) % links.length].focus();
  };
  return (
    <nav ref={ref} className={`tabs ${sub ? "tabs--sub" : ""}`} aria-label={label} onKeyDown={onKeyDown}>
      {items.map((item) => (
        <Link
          key={item.to + JSON.stringify(item.search ?? {})}
          to={item.to}
          search={(item.search ?? {}) as never}
          className="tab"
          activeOptions={EXACT_ACTIVE}
          activeProps={NO_ACTIVE_PROPS}
          aria-current={item.active ? "page" : undefined}
        >
          {item.label}
          {item.count !== undefined && item.count !== null ? <span className="tab-count">{item.count.toLocaleString("en-US")}</span> : null}
        </Link>
      ))}
    </nav>
  );
}

export function TagToken({ tag, onRemove, to, onColor }: { tag: string; onRemove?: () => void; to?: string; onColor?: () => void }) {
  const look = useTagLook()(tag);
  const style = look.color ? ({ "--tag-color": look.color } as CSSProperties) : undefined;
  const title = look.builtin ? `${look.label}: ${look.description} (built-in tag)` : look.label !== tag ? tag : undefined;
  return (
    <span className="tag" data-colored={look.color ? "" : undefined} style={style} title={title}>
      {onColor ? (
        <button type="button" className="tag-swatch" aria-label={`Color of tag ${look.label}`} title={`Color of tag ${look.label}`} onClick={onColor} />
      ) : look.color ? (
        <span className="tag-swatch" aria-hidden="true" />
      ) : null}
      {to ? (
        <Link to={to} className="tag-label">
          {look.label}
        </Link>
      ) : (
        <span className="tag-label">{look.label}</span>
      )}
      {onRemove ? (
        <button type="button" className="tag-remove" aria-label={`Remove tag ${look.label}`} title={`Remove tag ${look.label}`} onClick={onRemove}>
          <Icon name="close" />
        </button>
      ) : null}
    </span>
  );
}

export function TagList({ tags }: { tags: string[] }) {
  if (!tags.length) return null;
  return (
    <span className="tag-list">
      {tags.map((tag) => (
        <TagToken key={tag} tag={tag} />
      ))}
    </span>
  );
}

export function normalizeTag(tag: string): string {
  return tag.trim().replace(/,/g, "").slice(0, 64);
}

export function TagInput({ tags, onChange, suggestions = [], inputRef, label = "Add tag", disabled, onColor }: { tags: string[]; onChange: (tags: string[]) => void; suggestions?: string[]; inputRef?: React.Ref<HTMLInputElement>; label?: string; disabled?: boolean; onColor?: (tag: string) => void }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listId = useId();
  const tokensRef = useRef<HTMLDivElement>(null);
  const matches = suggestions.filter((s) => !tags.includes(s) && s.toLowerCase().includes(text.trim().toLowerCase())).slice(0, 8);
  const commit = (value: string) => {
    const tag = normalizeTag(value);
    if (tag && !tags.includes(tag)) onChange([...tags, tag]);
    setText("");
    setActive(0);
  };
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") {
      if (event.key === "Enter" && !text.trim() && !(open && matches[active])) return;
      event.preventDefault();
      commit(open && matches[active] && event.key === "Enter" ? matches[active] : text);
    } else if (event.key === "Backspace" && !text) {
      const buttons = tokensRef.current?.querySelectorAll<HTMLButtonElement>(".tag-remove");
      buttons?.[buttons.length - 1]?.focus();
    } else if (event.key === "ArrowDown" && matches.length) {
      event.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, matches.length - 1));
    } else if (event.key === "ArrowUp" && matches.length) {
      event.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (event.key === "Escape" && open) {
      event.stopPropagation();
      setOpen(false);
    }
  };
  return (
    <div className="tag-input" ref={tokensRef}>
      {tags.map((tag) => (
        <TagToken key={tag} tag={tag} onRemove={disabled ? undefined : () => onChange(tags.filter((t) => t !== tag))} onColor={onColor && !disabled ? () => onColor(tag) : undefined} />
      ))}
      <span className="combo">
        <input
          ref={inputRef}
          className="input"
          value={text}
          placeholder={label}
          aria-label={label}
          disabled={disabled}
          role="combobox"
          aria-expanded={open && matches.length > 0}
          aria-controls={listId}
          aria-autocomplete="list"
          onChange={(event) => {
            setText(event.target.value.replace(",", ""));
            setOpen(true);
            setActive(0);
            if (event.target.value.includes(",")) commit(event.target.value);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          onKeyDown={onKeyDown}
        />
        {open && text && matches.length ? (
          <ul id={listId} role="listbox" className="combo-list">
            {matches.map((match, index) => (
              <li
                key={match}
                role="option"
                aria-selected={index === active}
                className="combo-option"
                onMouseDown={(event) => {
                  event.preventDefault();
                  commit(match);
                }}
              >
                {match}
              </li>
            ))}
          </ul>
        ) : null}
      </span>
    </div>
  );
}

export interface ComboOption {
  value: string;
  label: string;
}

export function Combobox({ options, value, onChange, label, placeholder, createLabel, id, autoFocusTarget }: { options: ComboOption[]; value: string; onChange: (value: string, created: boolean) => void; label: string; placeholder?: string; createLabel?: (text: string) => string; id?: string; autoFocusTarget?: boolean }) {
  const selected = options.find((o) => o.value === value);
  const [text, setText] = useState(selected?.label ?? value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listId = useId();
  useEffect(() => {
    setText(options.find((o) => o.value === value)?.label ?? value);
  }, [value, options]);
  const query = text.trim().toLowerCase();
  const filtered = options.filter((o) => !query || o.label.toLowerCase().includes(query) || o.value.toLowerCase().includes(query) || o.label === selected?.label);
  const exact = options.some((o) => o.label.toLowerCase() === query || o.value.toLowerCase() === query);
  const rows: { option?: ComboOption; create?: string }[] = filtered.map((option) => ({ option }));
  if (createLabel && text.trim() && !exact) rows.push({ create: text.trim() });
  const pickRow = (row: { option?: ComboOption; create?: string }) => {
    if (row.option) {
      onChange(row.option.value, false);
      setText(row.option.label);
    } else if (row.create) {
      onChange(row.create, true);
      setText(row.create);
    }
    setOpen(false);
  };
  return (
    <span className="combo">
      <input
        id={id}
        className="input select"
        data-autofocus={autoFocusTarget ? "" : undefined}
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        value={text}
        placeholder={placeholder}
        autoComplete="off"
        onChange={(event) => {
          setText(event.target.value);
          setOpen(true);
          setActive(0);
        }}
        onFocus={(event) => {
          event.currentTarget.select();
          setOpen(true);
        }}
        onBlur={() =>
          window.setTimeout(() => {
            setOpen(false);
            setText(options.find((o) => o.value === value)?.label ?? value);
          }, 150)
        }
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            setActive((a) => Math.min(a + 1, rows.length - 1));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
          } else if (event.key === "Enter") {
            if (open && rows[active]) {
              event.preventDefault();
              pickRow(rows[active]);
            }
          } else if (event.key === "Escape" && open) {
            event.stopPropagation();
            event.preventDefault();
            setOpen(false);
          }
        }}
      />
      <Icon name="chevron-down" className="select-chevron" />
      {open && rows.length ? (
        <ul id={listId} role="listbox" className="combo-list">
          {rows.map((row, index) => (
            <li
              key={row.option?.value ?? `create:${row.create}`}
              role="option"
              aria-selected={index === active}
              className="combo-option"
              onMouseEnter={() => setActive(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                pickRow(row);
              }}
            >
              {row.option ? row.option.label : createLabel?.(row.create ?? "")}
            </li>
          ))}
        </ul>
      ) : null}
    </span>
  );
}

export function Progress({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <span className="progress">
      <span className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span className="progress-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="progress-text">{pct}%</span>
    </span>
  );
}

export function Meter({ value, max, over }: { value: number; max: number; over?: boolean }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <span className="meter" data-over={over ? "" : undefined} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      <span className="meter-fill" style={{ width: `${pct}%` }} />
    </span>
  );
}

export function Pagination({ start, count, total, hasPrevious, hasNext, onPrevious, onNext, loading }: { start: number | null; count: number; total: number | null; hasPrevious: boolean; hasNext: boolean; onPrevious: () => void; onNext: () => void; loading?: boolean }) {
  if (!count && !hasPrevious) return null;
  if (!hasPrevious && !hasNext && total !== null && total <= count) {
    return (
      <div className="pagination">
        <span className="pagination-range">{plainCount(count)}</span>
      </div>
    );
  }
  const range = !count ? "" : start === null ? `${count.toLocaleString("en-US")} shown` : `${start.toLocaleString("en-US")}-${(start + count - 1).toLocaleString("en-US")}`;
  return (
    <div className="pagination">
      <span className="pagination-range">{loading ? "Loading…" : total !== null && total !== undefined ? `${range} of ${total.toLocaleString("en-US")}` : range}</span>
      <span className="button-row">
        <Button onClick={onPrevious} disabled={!hasPrevious} title="Previous page ([)" icon="previous">
          Previous
        </Button>
        <Button onClick={onNext} disabled={!hasNext} title="Next page (])">
          Next
        </Button>
      </span>
    </div>
  );
}

function plainCount(count: number): string {
  return `${count.toLocaleString("en-US")} ${count === 1 ? "item" : "items"}`;
}

export function PageHeader({ title, actions, crumbs, meta, unseen, mono }: { title: ReactNode; actions?: ReactNode; crumbs?: Crumb[]; meta?: ReactNode; unseen?: boolean; mono?: boolean }) {
  return (
    <header className="page-head">
      {crumbs ? <Breadcrumbs items={crumbs} /> : null}
      <div className="page-title-row">
        <h1 className={`page-title ${mono ? "mono" : ""}`} tabIndex={-1} data-page-title="">
          {unseen ? <Unseen /> : null}
          {title}
        </h1>
        {actions ? <div className="page-actions">{actions}</div> : null}
      </div>
      {meta ? <div className="page-meta">{meta}</div> : null}
    </header>
  );
}

export function MetaItem({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <span className="meta-item">
      {label ? <span className="meta-key">{label}</span> : null}
      <span>{children}</span>
    </span>
  );
}

export function Section({ title, count, actions, children, id, flush, className }: { title: ReactNode; count?: number | string | null; actions?: ReactNode; children: ReactNode; id?: string; flush?: boolean; className?: string }) {
  const headingId = useId();
  return (
    <section className={`section ${flush ? "section--flush" : ""} ${className ?? ""}`} aria-labelledby={headingId} id={id}>
      <div className="section-head">
        <h2 className="section-title" id={headingId}>
          {title}
          {count !== undefined && count !== null ? <span className="section-count">{typeof count === "number" ? count.toLocaleString("en-US") : count}</span> : null}
        </h2>
        {actions ? <div className="section-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function StateWord({ state, children }: { state: string; children?: ReactNode }) {
  return (
    <span className="state-word" data-state={state}>
      {children ?? state}
    </span>
  );
}

interface QueryLike {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
  data?: unknown;
}

export function queryState(query: QueryLike, what: string, options: { verb?: string; object?: string; notFound?: ReactNode; forbidden?: ReactNode } = {}): ReactNode | null {
  if (query.data !== undefined) return null;
  if (query.isPending) return <Loading what={what} />;
  if (query.isError) {
    const err = query.error;
    if (err instanceof ApiError && err.status === 404 && options.notFound) return options.notFound;
    if (err instanceof ApiError && err.status === 403 && options.forbidden) return options.forbidden;
    return <ErrorState message={errorText(err, options.verb ?? "load", options.object ?? what)} onRetry={() => void query.refetch()} />;
  }
  return null;
}

export function Forbidden({ what }: { what: string }) {
  return <EmptyState hint="Ask an admin to grant you access.">You do not have access to {what}.</EmptyState>;
}

export function LabelledSpan({ label, children, strong }: { label: string; children: ReactNode; strong?: boolean }) {
  return (
    <span aria-label={`${label}: ${typeof children === "string" ? children : ""}`.replace(/: $/, "")} className={strong ? "strong" : undefined}>
      {children}
    </span>
  );
}
