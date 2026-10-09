import { useNavigate } from "@tanstack/react-router";
import { useRef, type ReactNode } from "react";
import { isTypingTarget, shortcutAllowed, useIsSmall, useKeydown } from "../lib/hooks";
import { Checkbox } from "./Form";
import { Icon } from "./Icon";

export interface Column<T> {
  key: string;
  header: ReactNode;
  headerLabel?: string;
  align?: "left" | "right";
  sortKey?: string;
  naturalDir?: "asc" | "desc";
  width?: string;
  render: (row: T) => ReactNode;
  title?: (row: T) => string | undefined;
  className?: string;
}

export interface Selection {
  selected: Set<string>;
  onToggle: (id: string, extend: boolean) => void;
  onToggleAll: (checked: boolean) => void;
  onClear: () => void;
}

interface TableProps<T> {
  rows: T[];
  columns: Column<T>[];
  getId: (row: T) => string;
  getHref?: (row: T) => string | undefined;
  onOpen?: (row: T) => void;
  sort?: string;
  dir?: "asc" | "desc";
  onSort?: (key: string, dir: "asc" | "desc") => void;
  selection?: Selection;
  onRowKey?: (row: T, key: string, event: React.KeyboardEvent) => boolean;
  label: string;
  empty?: ReactNode;
  mobileRow?: (row: T) => ReactNode;
  primary?: boolean;
  rowClassName?: (row: T) => string | undefined;
  compact?: boolean;
  isSelectedRow?: (row: T) => boolean;
  onFocusRow?: (row: T) => void;
  footer?: ReactNode;
}

export function DataTable<T>(props: TableProps<T>) {
  const { rows, columns, getId, getHref, onOpen, sort, dir, onSort, selection, onRowKey, label, empty, primary, rowClassName, compact, isSelectedRow, onFocusRow, footer } = props;
  const mobileRow = props.mobileRow ?? ((row: T) => <StackedFields row={row} columns={columns} />);
  const navigate = useNavigate();
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const small = useIsSmall();

  const open = (row: T, newTab = false) => {
    const href = getHref?.(row);
    if (newTab && href) {
      window.open(href, "_blank", "noopener");
      return;
    }
    if (onOpen) onOpen(row);
    else if (href) void navigate({ to: href });
  };

  const rowsIn = (): HTMLElement[] => {
    const container = small ? listRef.current : bodyRef.current;
    return Array.from(container?.querySelectorAll<HTMLElement>("[data-row]") ?? []);
  };

  const focusRow = (index: number) => {
    const all = rowsIn();
    if (!all.length) return;
    const target = all[Math.max(0, Math.min(index, all.length - 1))];
    all.forEach((el) => el.setAttribute("tabindex", "-1"));
    target.setAttribute("tabindex", "0");
    target.focus();
    const row = rows[Number(target.dataset.index)];
    if (row !== undefined) onFocusRow?.(row);
  };

  useKeydown((event) => {
    if (!primary || !shortcutAllowed(event)) return;
    if (event.key !== "j" && event.key !== "ArrowDown") return;
    const container = small ? listRef.current : bodyRef.current;
    if (container?.contains(document.activeElement)) return;
    if (document.activeElement && document.activeElement !== document.body && !(document.activeElement as HTMLElement).dataset.pageTitle) return;
    event.preventDefault();
    focusRow(0);
  });

  const onKeyDown = (event: React.KeyboardEvent, row: T, index: number) => {
    if (isTypingTarget(event.target)) return;
    if (event.target instanceof HTMLElement && event.target.closest("button, a, input, select") && event.target !== event.currentTarget) {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    }
    const id = getId(row);
    const mod = event.metaKey || event.ctrlKey;
    if (onRowKey && !mod && onRowKey(row, event.key, event)) {
      event.preventDefault();
      return;
    }
    switch (event.key) {
      case "j":
      case "ArrowDown":
        event.preventDefault();
        if (event.shiftKey && selection) selection.onToggle(getId(rows[Math.min(index + 1, rows.length - 1)]), true);
        focusRow(index + 1);
        break;
      case "k":
      case "ArrowUp":
        event.preventDefault();
        if (event.shiftKey && selection) selection.onToggle(getId(rows[Math.max(index - 1, 0)]), true);
        focusRow(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focusRow(0);
        break;
      case "End":
        event.preventDefault();
        focusRow(rows.length - 1);
        break;
      case "Enter":
      case "o":
        event.preventDefault();
        open(row, mod);
        break;
      case " ":
        if (selection) {
          event.preventDefault();
          selection.onToggle(id, event.shiftKey);
        }
        break;
      case "a":
        if (mod && selection) {
          event.preventDefault();
          selection.onToggleAll(true);
        }
        break;
      case "Escape":
        if (selection && selection.selected.size) {
          event.preventDefault();
          selection.onClear();
        }
        break;
      default:
        break;
    }
  };

  const onRowClick = (event: React.MouseEvent, row: T) => {
    const target = event.target as HTMLElement;
    if (target.closest("a, button, input, label, select, textarea")) return;
    if (window.getSelection()?.toString()) return;
    open(row, event.metaKey || event.ctrlKey);
  };

  const allSelected = selection && rows.length > 0 && rows.every((row) => selection.selected.has(getId(row)));
  const someSelected = selection && rows.some((row) => selection.selected.has(getId(row)));

  if (small) {
    return (
      <div>
        {rows.length ? (
          <ul ref={listRef} className="rows" aria-label={label}>
            {rows.map((row, index) => {
              const id = getId(row);
              const selected = selection?.selected.has(id) || isSelectedRow?.(row);
              return (
                <li
                  key={id}
                  data-row=""
                  data-index={index}
                  tabIndex={index === 0 ? 0 : -1}
                  className={`row-item ${rowClassName?.(row) ?? ""}`}
                  aria-selected={selection || isSelectedRow ? Boolean(selected) : undefined}
                  onClick={(event) => onRowClick(event, row)}
                  onKeyDown={(event) => onKeyDown(event, row, index)}
                >
                  {selection ? (
                    <Checkbox hiddenLabel={`Select ${id}`} checked={selection.selected.has(id)} onChange={(event) => selection.onToggle(id, (event.nativeEvent as MouseEvent).shiftKey)} />
                  ) : null}
                  <div className="row-body">{mobileRow(row)}</div>
                </li>
              );
            })}
          </ul>
        ) : (
          empty
        )}
        {footer}
      </div>
    );
  }

  return (
    <div className="ledger-wrap">
      <table className={`ledger ${compact ? "ledger-compact" : ""}`} role="grid" aria-label={label} aria-multiselectable={selection ? true : undefined}>
        <colgroup>
          {selection ? <col style={{ width: "36px" }} /> : null}
          {columns.map((column) => (
            <col key={column.key} style={column.width ? { width: column.width } : undefined} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {selection ? (
              <th scope="col" className="cell-check">
                <Checkbox hiddenLabel="Select all on this page" checked={Boolean(allSelected)} indeterminate={Boolean(someSelected && !allSelected)} onChange={(event) => selection.onToggleAll(event.target.checked)} />
              </th>
            ) : null}
            {columns.map((column) => {
              const sorted = sort && column.sortKey === sort;
              const natural = column.naturalDir ?? (column.align === "right" ? "desc" : "asc");
              const nextDir: "asc" | "desc" = sorted ? (dir === "asc" ? "desc" : "asc") : natural;
              return (
                <th
                  key={column.key}
                  scope="col"
                  className={`${column.align === "right" ? "align-right" : ""} ${column.className ?? ""}`}
                  aria-sort={sorted ? (dir === "asc" ? "ascending" : "descending") : column.sortKey ? "none" : undefined}
                >
                  {column.sortKey && onSort ? (
                    <button type="button" className={`sort-btn ${sorted ? "is-sorted" : ""} ${nextDir === "asc" ? "next-asc" : "next-desc"}`} onClick={() => onSort(column.sortKey!, nextDir)}>
                      <span className="sort-label">{column.header}</span>
                      <Icon name="chevron-down" className={`sort-icon ${sorted && dir === "asc" ? "is-asc" : ""}`} />
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody ref={bodyRef}>
          {rows.map((row, index) => {
            const id = getId(row);
            const selected = Boolean(selection?.selected.has(id) || isSelectedRow?.(row));
            return (
              <tr
                key={id}
                data-row=""
                data-index={index}
                tabIndex={index === 0 ? 0 : -1}
                aria-selected={selection || isSelectedRow ? selected : undefined}
                data-href={getHref || onOpen ? "" : undefined}
                className={rowClassName?.(row)}
                onClick={(event) => onRowClick(event, row)}
                onKeyDown={(event) => onKeyDown(event, row, index)}
                onFocus={(event) => {
                  if (event.target === event.currentTarget) {
                    rowsIn().forEach((el) => el.setAttribute("tabindex", el === event.currentTarget ? "0" : "-1"));
                  }
                }}
              >
                {selection ? (
                  <td className="cell-check">
                    <Checkbox
                      hiddenLabel={`Select ${id}`}
                      checked={selection.selected.has(id)}
                      onClick={(event) => {
                        event.stopPropagation();
                        selection.onToggle(id, event.shiftKey);
                      }}
                      onChange={() => undefined}
                    />
                  </td>
                ) : null}
                {columns.map((column) => (
                  <td key={column.key} className={`${column.align === "right" ? "align-right" : ""} ${column.className ?? ""}`} title={column.title?.(row)}>
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {!rows.length ? empty : null}
      {footer}
    </div>
  );
}

export type Natural = Record<string, "asc" | "desc">;

export function useSort(params: Record<string, string>, defaultSort: string, defaultDir: "asc" | "desc", natural: Natural = {}): { sort: string; dir: "asc" | "desc" } {
  const sort = params.sort || defaultSort;
  const fallback = sort === defaultSort ? defaultDir : natural[sort] ?? "asc";
  const dir = (params.dir === "asc" || params.dir === "desc" ? params.dir : fallback) as "asc" | "desc";
  return { sort, dir };
}

export function sortPatch(key: string, dir: "asc" | "desc", defaultSort: string, defaultDir: "asc" | "desc", natural: Natural = {}): { sort: string | null; dir: string | null } {
  if (key === defaultSort && dir === defaultDir) return { sort: null, dir: null };
  const naturalDir = key === defaultSort ? defaultDir : natural[key] ?? "asc";
  return { sort: key, dir: dir === naturalDir ? null : dir };
}

export function compareValues(a: unknown, b: unknown): number {
  if (a === b) return 0;
  if (a === null || a === undefined || a === "") return 1;
  if (b === null || b === undefined || b === "") return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "en", { numeric: true, sensitivity: "base" });
}

export function sortRows<T>(rows: T[], accessor: (row: T) => unknown, dir: "asc" | "desc"): T[] {
  const copy = [...rows];
  copy.sort((x, y) => {
    const a = accessor(x);
    const b = accessor(y);
    if (a === null || a === undefined || a === "") return b === null || b === undefined || b === "" ? 0 : 1;
    if (b === null || b === undefined || b === "") return -1;
    const result = compareValues(a, b);
    return dir === "asc" ? result : -result;
  });
  return copy;
}

function StackedFields<T>({ row, columns }: { row: T; columns: Column<T>[] }) {
  const [first, ...rest] = columns;
  return (
    <>
      <div className="row-title">{first.render(row)}</div>
      <dl className="row-fields">
        {rest.map((column) => {
          const label = column.headerLabel ?? (typeof column.header === "string" ? column.header : "");
          const value = column.render(row);
          if (value === null || value === undefined || value === "") return null;
          if (!label) {
            return (
              <div key={column.key} className="row-field row-field--bare">
                <dd>{value}</dd>
              </div>
            );
          }
          return (
            <div key={column.key} className="row-field">
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          );
        })}
      </dl>
    </>
  );
}
