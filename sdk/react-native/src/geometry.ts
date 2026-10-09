export type Box = { x: number; y: number; width: number; height: number };

export const MARKER_INSET = 4;

export function relativeTo(box: Box, origin: Pick<Box, "x" | "y"> | null): Box {
  if (!origin) return box;
  return { x: box.x - origin.x, y: box.y - origin.y, width: box.width, height: box.height };
}

export function markerFrame(box: Box) {
  return {
    left: box.x - MARKER_INSET,
    top: box.y - MARKER_INSET,
    width: box.width + MARKER_INSET * 2,
    height: box.height + MARKER_INSET * 2,
  };
}

export type Span = { start: number; size: number };

export function revealOffset(
  item: Span,
  viewport: Span,
  screen: Span,
  contentStart: number,
  contentSize: number
): number | null {
  const lo = Math.max(viewport.start, screen.start);
  const hi = Math.min(viewport.start + viewport.size, screen.start + screen.size);
  if (hi <= lo) return null;
  if (item.start >= lo && item.start + item.size <= hi) return null;
  const current = contentStart - (item.start - viewport.start);
  const room = hi - lo;
  const desired = item.size < room ? lo + (room - item.size) / 2 : lo;
  const furthest = Math.max(0, contentSize - viewport.size);
  const next = Math.min(Math.max(contentStart - (desired - viewport.start), 0), furthest);
  return Math.abs(next - current) < 1 ? null : next;
}
