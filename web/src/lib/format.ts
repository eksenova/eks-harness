const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DAYS_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

export const ELLIPSIS = "…";
export const TIMES = "×";

const countFormat = new Intl.NumberFormat("en-US");

export function formatCount(value: number | null | undefined): string {
  return countFormat.format(value ?? 0);
}

export function plural(count: number, one: string, many?: string): string {
  return `${formatCount(count)} ${count === 1 ? one : many ?? `${one}s`}`;
}

export function formatSize(bytes: number | null | undefined): string {
  const value = bytes ?? 0;
  if (value < 1000) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let scaled = value / 1000;
  let unit = 0;
  while (scaled >= 1000 && unit < units.length - 1) {
    scaled /= 1000;
    unit += 1;
  }
  const text = scaled < 10 ? scaled.toFixed(1).replace(/\.0$/, "") : Math.round(scaled).toString();
  return `${text} ${units[unit]}`;
}

export function formatDuration(ms: number | null | undefined): string {
  const total = Math.max(0, Math.round((ms ?? 0) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const ss = seconds.toString().padStart(2, "0");
  if (hours > 0) return `${hours}:${minutes.toString().padStart(2, "0")}:${ss}`;
  return `${minutes}:${ss}`;
}

export function formatSeconds(seconds: number): string {
  return formatDuration(seconds * 1000);
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1).replace(/\.0$/, "")} s`;
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 3600) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
  }
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

export function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

export function formatClock(date: Date, seconds = false): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}${seconds ? `:${pad(date.getSeconds())}` : ""}`;
}

export function formatAbsolute(value: string | number | Date | null | undefined, now = new Date()): string {
  const date = toDate(value);
  if (!date) return "";
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return formatClock(date);
  if (date.getFullYear() === now.getFullYear()) return `${date.getDate()} ${MONTHS[date.getMonth()]}, ${formatClock(date)}`;
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

export function formatDate(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return "";
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

export function formatFull(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return "";
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}, ${formatClock(date, true)}`;
}

export function formatFullMinutes(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return "";
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}, ${formatClock(date)}`;
}

export function formatDayHeading(date: Date): string {
  return `${DAYS_LONG[date.getDay()]} ${date.getDate()} ${MONTHS_LONG[date.getMonth()]} ${date.getFullYear()}`;
}

export function formatRelative(value: string | number | Date | null | undefined, now = new Date()): string {
  const date = toDate(value);
  if (!date) return "";
  const seconds = (now.getTime() - date.getTime()) / 1000;
  if (seconds < 0) return formatFuture(date, now);
  if (seconds < 60) return "Just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return formatAbsolute(date, now);
}

export function formatFuture(date: Date, now = new Date()): string {
  const seconds = (date.getTime() - now.getTime()) / 1000;
  if (seconds < 60) return "In under 1 min";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `In ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `In ${hours} h`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "In 1 day" : `In ${days} days`;
}

export function formatAgoShort(value: string | number | Date | null | undefined, now = new Date()): string {
  const date = toDate(value);
  if (!date) return "";
  const seconds = Math.max(0, Math.round((now.getTime() - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.floor(minutes / 60)} h ago`;
}

export function formatIdle(seconds: number | null | undefined): string {
  const minutes = Math.floor((seconds ?? 0) / 60);
  if (minutes > 90) return `Idle ${Math.floor(minutes / 60)} h`;
  return `Idle ${minutes} min`;
}

export function formatWaiting(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h`;
}

export const KIND_LABELS: Record<string, string> = {
  screenshot: "Screenshot",
  video: "Video",
  audio: "Audio",
  dom: "DOM",
  mhtml: "MHTML",
  a11y: "Accessibility tree",
  har: "HAR",
  console: "Console",
  log: "Log",
  site: "Site",
  file: "File",
  image: "Image",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? (kind ? kind[0].toUpperCase() + kind.slice(1) : "File");
}

export const SOURCE_LABELS: Record<string, string> = { agent: "Agent", cli: "CLI", ui: "UI", mcp: "MCP" };

export function sourceLabel(source: string | null | undefined): string {
  return source ? SOURCE_LABELS[source] ?? source : "";
}

export function resourceName(resource: string | null | undefined): string {
  if (!resource) return "";
  const parts = resource.split(":");
  if (parts[0] === "browser") {
    if (parts.length === 3) return `Browser ${parts[1]}.${parts[2]}`;
    if (parts.length === 2) return `Browser ${parts[1]}`;
  }
  if (parts[0] === "ios" && parts[1]) return `iOS ${parts[1]}`;
  if (parts[0] === "android" && parts[1]) return `Android ${parts[1]}`;
  if (parts[0] === "backend") return parts.slice(1).join(":");
  return resource;
}

export function leaseKindLabel(kind: string): string {
  if (kind === "ios") return "iOS";
  if (kind === "android") return "Android";
  if (kind === "browser") return "Browser";
  return kind;
}

export function dimensions(width: number | null | undefined, height: number | null | undefined): string {
  if (!width || !height) return "";
  return `${width} ${TIMES} ${height}`;
}

export function shortHash(value: string, length = 16): string {
  return value.length > length ? `${value.slice(0, length)}${ELLIPSIS}` : value;
}

export function projectParts(projectId: string): { owner: string; name: string } {
  const index = projectId.indexOf("/");
  return index < 0 ? { owner: projectId, name: "" } : { owner: projectId.slice(0, index), name: projectId.slice(index + 1) };
}

export function slugify(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[\/\s]+/g, "-")
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "")
    .slice(0, 100);
}

export function stripScheme(url: string | null | undefined): string {
  if (!url) return "";
  return url.replace(/^[a-z]+:\/\//i, "");
}

export function metaString(meta: Record<string, unknown> | null | undefined, ...keys: string[]): string {
  if (!meta) return "";
  for (const key of keys) {
    const value = meta[key];
    if (typeof value === "string" && value) return value;
    if (typeof value === "number") return String(value);
  }
  return "";
}

export function metaNumber(meta: Record<string, unknown> | null | undefined, ...keys: string[]): number | null {
  if (!meta) return null;
  for (const key of keys) {
    const value = meta[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (Array.isArray(value)) return value.length;
  }
  return null;
}

export function flatMeta(meta: Record<string, unknown> | null | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = { ...(meta ?? {}) };
  for (const value of Object.values(meta ?? {})) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
        if (!(key in out)) out[key] = inner;
      }
    }
  }
  return out;
}
