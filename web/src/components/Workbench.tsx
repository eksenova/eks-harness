import { useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { formatSeconds } from "../lib/format";
import { Icon } from "./Icon";

export type Tone = "ok" | "busy" | "wait" | "fail" | "idle";

export function toneFor(state: string | null | undefined): Tone {
  const value = (state ?? "").toLowerCase();
  if (/(fail|error|broken|offline|lost|denied|changed|cancel)/.test(value)) return "fail";
  if (/(run|render|assign|start|boot|prepar|record|playing|connect)/.test(value)) return "busy";
  if (/(queue|pending|wait|idle|paused|hold)/.test(value)) return "wait";
  if (/(done|active|online|ok|ready|in use|leased|approved|trusted|succeed)/.test(value)) return "ok";
  return "idle";
}

export function StateBar({ tone }: { tone: Tone }) {
  return <span className="state-bar" data-tone={tone} aria-hidden="true" />;
}

export function StateLine({ state, tone, children, detail }: { state: string; tone?: Tone; children?: ReactNode; detail?: ReactNode }) {
  const resolved = tone ?? toneFor(state);
  return (
    <span className="state-line" data-tone={resolved}>
      <StateBar tone={resolved} />
      <span className="state-line-word">{children ?? state}</span>
      {detail ? <span className="state-line-detail">{detail}</span> : null}
    </span>
  );
}

export function Timecode({ seconds, fps = 30, frames = true }: { seconds: number | null | undefined; fps?: number; frames?: boolean }) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return <span className="timecode">--:--:--</span>;
  const total = Math.max(0, seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = Math.floor(total % 60);
  const f = Math.floor((total - Math.floor(total)) * fps + 1e-6);
  const pad = (value: number) => String(value).padStart(2, "0");
  return <span className="timecode">{`${pad(h)}:${pad(m)}:${pad(s)}${frames ? `:${pad(f)}` : ""}`}</span>;
}

export function Duration({ seconds }: { seconds: number | null | undefined }) {
  if (seconds === null || seconds === undefined) return null;
  return <span className="num">{formatSeconds(seconds)}</span>;
}

export function RunBlock({
  title,
  state,
  tone,
  facts,
  progress,
  children,
  defaultOpen = false,
  to,
  id,
}: {
  title: ReactNode;
  state: string;
  tone?: Tone;
  facts?: ReactNode[];
  progress?: number | null;
  children?: ReactNode;
  defaultOpen?: boolean;
  to?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const resolved = tone ?? toneFor(state);
  const hasBody = Boolean(children);
  return (
    <article className="run-block" data-tone={resolved} data-open={open || undefined} id={id}>
      <header className="run-block-head">
        {hasBody ? (
          <button type="button" className="run-block-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
            <Icon name="next" size={12} className="run-block-chevron" />
            <span className="run-block-title">{title}</span>
          </button>
        ) : to ? (
          <Link to={to} className="run-block-title run-block-link">
            {title}
          </Link>
        ) : (
          <span className="run-block-title run-block-static">{title}</span>
        )}
        <StateLine state={state} tone={resolved} />
        {facts?.length ? (
          <span className="run-block-facts">
            {facts.filter(Boolean).map((fact, index) => (
              <span key={index}>{fact}</span>
            ))}
          </span>
        ) : null}
      </header>
      {progress !== null && progress !== undefined && resolved === "busy" ? (
        <div className="run-block-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
          <span style={{ width: `${Math.max(2, Math.min(100, progress * 100))}%` }} />
        </div>
      ) : null}
      {open && hasBody ? <div className="run-block-body">{children}</div> : null}
    </article>
  );
}

export interface RackColumn<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  width?: string;
  align?: "end";
  phone?: boolean;
}

export function Rack<T>({ rows, columns, rowKey, rowTo, label, empty, onRow }: {
  rows: T[];
  columns: RackColumn<T>[];
  rowKey: (row: T) => string;
  rowTo?: (row: T) => string | null;
  label: string;
  empty?: ReactNode;
  onRow?: (row: T) => void;
}) {
  const template = columns.map((c) => c.width ?? "minmax(0, 1fr)").join(" ");
  if (!rows.length) return <div className="rack-empty">{empty}</div>;
  return (
    <div className="rack" role="table" aria-label={label} style={{ ["--rack-cols" as string]: template }}>
      <div className="rack-head" role="row">
        {columns.map((column) => (
          <span key={column.key} role="columnheader" className={column.align === "end" ? "end" : undefined}>
            {column.label}
          </span>
        ))}
      </div>
      {rows.map((row) => {
        const to = rowTo?.(row) ?? null;
        const cells = columns.map((column) => (
          <span key={column.key} role="cell" data-label={column.label} className={`${column.align === "end" ? "end" : ""} ${column.phone === false ? "rack-wide-only" : ""}`}>
            {column.render(row)}
          </span>
        ));
        return to ? (
          <Link key={rowKey(row)} to={to} className="rack-row rack-row--link" role="row">
            {cells}
          </Link>
        ) : (
          <div key={rowKey(row)} className="rack-row" role="row" onClick={onRow ? () => onRow(row) : undefined}>
            {cells}
          </div>
        );
      })}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function SlotFrame({ children }: { children: ReactNode }) {
  return <div className="slot-frame">{children}</div>;
}
