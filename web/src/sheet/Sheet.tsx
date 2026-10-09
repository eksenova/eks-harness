import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Icon } from "../components/Icon";
import { Timecode } from "../components/Workbench";
import { useIsSmall } from "../lib/hooks";

export type TrackKind = "edit" | "blender" | "web" | "device" | "audio" | "plugin";

export interface SheetClip {
  id: string;
  start: number;
  end: number;
  label: string;
  detail?: string;
}

export interface SheetEvent {
  id: string;
  time: number;
  name: string;
  dir: "in" | "out";
  detail?: string;
}

export interface SheetTrack {
  id: string;
  code: string;
  kind: TrackKind;
  name: string;
  meta?: string;
  clips: SheetClip[];
  events?: SheetEvent[];
}

export interface SheetLink {
  id: string;
  from: { track: string; time: number };
  to: { track: string; time: number };
}

export interface SheetMarker {
  name: string;
  time: number;
}

export interface SheetSelection {
  track: string;
  clip?: string;
  event?: string;
}

const TRACK_HEIGHT = 54;
const EVENT_LANE = 16;
const RULER_TIME = 22;
const RULER_BARS = 18;

export function kindCode(kind: TrackKind): string {
  return { edit: "E", blender: "B", web: "H", device: "D", audio: "A", plugin: "P" }[kind];
}

function niceStep(pxPerSec: number): number {
  const steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  return steps.find((step) => step * pxPerSec >= 72) ?? 600;
}

function nearest(values: number[], time: number): number | null {
  let best: number | null = null;
  for (const value of values) {
    if (best === null || Math.abs(value - time) < Math.abs(best - time)) best = value;
  }
  return best;
}

export interface SheetProps {
  duration: number;
  fps: number;
  tracks: SheetTrack[];
  beats?: number[];
  downbeats?: number[];
  markers?: SheetMarker[];
  links?: SheetLink[];
  position: number;
  onSeek: (time: number) => void;
  selection?: SheetSelection | null;
  onSelect?: (selection: SheetSelection | null) => void;
  label: string;
  toolbar?: ReactNode;
  playing?: boolean;
}

export function Sheet({ duration, fps, tracks, beats = [], downbeats = [], markers = [], links = [], position, onSeek, selection, onSelect, label, toolbar, playing }: SheetProps) {
  const small = useIsSmall();
  const scroller = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [zoom, setZoom] = useState<number | null>(null);
  const [snap, setSnap] = useState(true);
  const [snapLine, setSnapLine] = useState<number | null>(null);
  const headWidth = small ? 40 : 168;
  const safeDuration = Math.max(duration, 1);
  const fit = Math.max(8, (width - headWidth - 24) / safeDuration);
  const pxPerSec = zoom ?? fit;
  const contentWidth = Math.ceil(safeDuration * pxPerSec) + (small ? width : 48);
  const lead = small ? Math.max(0, (width - headWidth) / 2) : 0;
  const x = useCallback((time: number) => lead + time * pxPerSec, [lead, pxPerSec]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    setWidth(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const view = element.clientWidth - headWidth;
    if (small) {
      element.scrollLeft = x(position) - lead;
      return;
    }
    const at = x(position);
    if (playing && (at < element.scrollLeft || at > element.scrollLeft + view - 40)) element.scrollLeft = Math.max(0, at - view * 0.2);
  }, [position, small, x, lead, headWidth, playing]);

  const snapTargets = useMemo(() => {
    const out = [...beats, ...downbeats, ...markers.map((m) => m.time)];
    for (const track of tracks) for (const clip of track.clips) out.push(clip.start, clip.end);
    return out;
  }, [beats, downbeats, markers, tracks]);

  const timeAt = (clientX: number): number => {
    const element = scroller.current!;
    const rect = element.getBoundingClientRect();
    const raw = (clientX - rect.left - headWidth + element.scrollLeft - lead) / pxPerSec;
    let time = Math.max(0, Math.min(safeDuration, raw));
    if (snap) {
      const near = nearest(snapTargets, time);
      if (near !== null && Math.abs(near - time) * pxPerSec < 8) {
        time = near;
        setSnapLine(near);
      } else setSnapLine(null);
    }
    return Math.round(time * fps) / fps;
  };

  const startScrub = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    onSeek(timeAt(event.clientX));
    const move = (e: PointerEvent) => onSeek(timeAt(e.clientX));
    const up = () => {
      setSnapLine(null);
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };

  const zoomBy = (factor: number) => setZoom((current) => Math.max(4, Math.min(2400, (current ?? fit) * factor)));
  const step = niceStep(pxPerSec);
  const ticks = [];
  for (let t = 0; t <= safeDuration + 1e-6; t += step) ticks.push(t);
  const visibleBeats = pxPerSec * (beats[1] - beats[0] || 1) >= 6 ? beats : downbeats;
  const rows = tracks.map((track, index) => ({ track, top: index * TRACK_HEIGHT }));
  const rowOf = new Map(rows.map((r) => [r.track.id, r.top]));
  const bodyHeight = Math.max(rows.length * TRACK_HEIGHT, TRACK_HEIGHT);
  const barOf = (time: number) => {
    const index = downbeats.findIndex((d, i) => time >= d - 1e-6 && (i === downbeats.length - 1 || time < downbeats[i + 1] - 1e-6));
    if (index < 0) return null;
    const inBar = beats.filter((b) => b >= downbeats[index] - 1e-6 && b <= time + 1e-6).length;
    return `${index + 1}.${Math.max(1, inBar)}`;
  };

  const onKey = (event: React.KeyboardEvent) => {
    const frame = 1 / fps;
    if (event.key === "ArrowRight") onSeek(Math.min(safeDuration, position + (event.shiftKey ? 1 : frame)));
    else if (event.key === "ArrowLeft") onSeek(Math.max(0, position - (event.shiftKey ? 1 : frame)));
    else if (event.key === "]") onSeek(beats.find((b) => b > position + 1e-6) ?? position);
    else if (event.key === "[") onSeek([...beats].reverse().find((b) => b < position - 1e-6) ?? 0);
    else if (event.key === "=" || event.key === "+") zoomBy(1.5);
    else if (event.key === "-") zoomBy(1 / 1.5);
    else if (event.key.toLowerCase() === "z" && event.shiftKey) setZoom(null);
    else if (event.key.toLowerCase() === "n") setSnap((s) => !s);
    else if (event.key === "Home") onSeek(0);
    else if (event.key === "End") onSeek(safeDuration);
    else return;
    event.preventDefault();
  };

  return (
    <div className="sheet" data-small={small || undefined} style={{ ["--head" as string]: `${headWidth}px` }}>
      <div className="sheet-bar">
        <span className="sheet-readout">
          <Timecode seconds={position} fps={fps} />
          {downbeats.length ? <span className="timecode sheet-bars">{barOf(position) ?? "0.0"}</span> : null}
          <span className="ink-3 num">f {Math.round(position * fps)}</span>
        </span>
        {toolbar}
        <span className="sheet-fill" />
        <button type="button" className="sheet-tool" aria-pressed={snap} onClick={() => setSnap(!snap)} title="Snap to beats, cues and clip edges (N)">
          <span>Snap</span>
        </button>
        <button type="button" className="sheet-tool" onClick={() => zoomBy(1 / 1.5)} title="Zoom out (-)" aria-label="Zoom out">
          <Icon name="zoom-out" size={14} />
        </button>
        <button type="button" className="sheet-tool" onClick={() => setZoom(null)} title="Fit (Shift Z)">
          <span>Fit</span>
        </button>
        <button type="button" className="sheet-tool" onClick={() => zoomBy(1.5)} title="Zoom in (+)" aria-label="Zoom in">
          <Icon name="zoom-in" size={14} />
        </button>
      </div>
      <div
        className="sheet-scroll"
        ref={scroller}
        tabIndex={0}
        role="group"
        aria-label={`${label}. Arrows step frames, brackets step beats, plus and minus zoom.`}
        onKeyDown={onKey}
        onWheel={(event) => {
          if (event.ctrlKey || event.metaKey) {
            event.preventDefault();
            zoomBy(event.deltaY < 0 ? 1.15 : 1 / 1.15);
          }
        }}
      >
        <div className="sheet-canvas" style={{ width: contentWidth + headWidth }}>
          <div className="sheet-rulers" onPointerDown={startScrub}>
            <div className="sheet-corner">
              {!small ? (
                <>
                  <span className="sheet-corner-row">time</span>
                  {downbeats.length ? <span className="sheet-corner-row">bar.beat</span> : null}
                </>
              ) : null}
            </div>
            <div className="sheet-ruler" style={{ width: contentWidth }}>
              <div className="sheet-ruler-time" style={{ height: RULER_TIME }}>
                {ticks.map((t) => (
                  <span key={t} className="sheet-tick" style={{ left: x(t) }}>
                    <Timecode seconds={t} fps={fps} frames={step < 1} />
                  </span>
                ))}
              </div>
              {downbeats.length ? (
                <div className="sheet-ruler-bars" style={{ height: RULER_BARS }}>
                  {visibleBeats.map((b, i) => {
                    const isBar = downbeats.some((d) => Math.abs(d - b) < 1e-4);
                    const barIndex = downbeats.findIndex((d) => Math.abs(d - b) < 1e-4);
                    return <span key={`${i}-${b}`} className={`sheet-beat-tick${isBar ? " sheet-beat-tick--bar" : ""}`} style={{ left: x(b) }}>{isBar && pxPerSec * (downbeats[1] - downbeats[0] || 2) > 28 ? barIndex + 1 : ""}</span>;
                  })}
                </div>
              ) : null}
              {markers.map((marker) => (
                <span key={marker.name} className="sheet-marker" style={{ left: x(marker.time) }} title={`${marker.name} at ${marker.time.toFixed(3)} s`}>
                  {marker.name}
                </span>
              ))}
              <span className="sheet-playhead-head" style={{ left: x(position) }} />
            </div>
          </div>
          <div className="sheet-body" style={{ height: bodyHeight }}>
            <div className="sheet-heads">
              {rows.map(({ track }) => (
                <div key={track.id} className="sheet-head" data-kind={track.kind} style={{ height: TRACK_HEIGHT }} aria-selected={selection?.track === track.id && !selection.clip && !selection.event} onClick={() => onSelect?.({ track: track.id })}>
                  <span className="sheet-head-code">{track.code}</span>
                  {!small ? (
                    <span className="sheet-head-text">
                      <span className="sheet-head-name">{track.name}</span>
                      {track.meta ? <span className="sheet-head-meta">{track.meta}</span> : null}
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="sheet-lanes" style={{ width: contentWidth }} onPointerDown={(event) => {
              if ((event.target as HTMLElement).closest(".sheet-clip, .sheet-event")) return;
              startScrub(event);
            }}>
              {visibleBeats.map((b, i) => (
                <span key={`r${i}`} className={`sheet-rule${downbeats.some((d) => Math.abs(d - b) < 1e-4) ? " sheet-rule--bar" : ""}`} style={{ left: x(b) }} />
              ))}
              {markers.map((marker) => (
                <span key={`m${marker.name}`} className="sheet-rule sheet-rule--marker" style={{ left: x(marker.time) }} />
              ))}
              {rows.map(({ track, top }) => (
                <div key={track.id} className="sheet-lane" data-kind={track.kind} style={{ top, height: TRACK_HEIGHT }}>
                  {track.clips.map((clip) => (
                    <button
                      type="button"
                      key={clip.id}
                      className="sheet-clip"
                      data-kind={track.kind}
                      aria-pressed={selection?.track === track.id && selection.clip === clip.id}
                      style={{ left: x(clip.start), width: Math.max(3, (clip.end - clip.start) * pxPerSec), height: TRACK_HEIGHT - EVENT_LANE - 6 }}
                      title={`${clip.label} ${clip.start.toFixed(2)} to ${clip.end.toFixed(2)} s${clip.detail ? `, ${clip.detail}` : ""}`}
                      onClick={() => onSelect?.({ track: track.id, clip: clip.id })}
                    >
                      <span className="sheet-clip-label">{clip.label}</span>
                      {clip.detail && (clip.end - clip.start) * pxPerSec > 120 ? <span className="sheet-clip-detail">{clip.detail}</span> : null}
                    </button>
                  ))}
                  {(track.events ?? []).map((event) => (
                    <button
                      type="button"
                      key={event.id}
                      className="sheet-event"
                      data-dir={event.dir}
                      aria-pressed={selection?.event === event.id}
                      style={{ left: x(event.time) }}
                      title={`${event.dir === "in" ? "receives" : "emits"} ${event.name} at ${event.time.toFixed(3)} s${event.detail ? `, ${event.detail}` : ""}`}
                      onClick={() => onSelect?.({ track: track.id, event: event.id })}
                    >
                      <span className="sheet-event-flag" />
                      {pxPerSec > 40 ? <span className="sheet-event-name">{event.name}</span> : null}
                    </button>
                  ))}
                </div>
              ))}
              <svg className="sheet-links" width={contentWidth} height={bodyHeight} aria-hidden="true">
                {links.map((link) => {
                  const fromTop = rowOf.get(link.from.track);
                  const toTop = rowOf.get(link.to.track);
                  if (fromTop === undefined || toTop === undefined) return null;
                  const y1 = fromTop + TRACK_HEIGHT - EVENT_LANE / 2;
                  const y2 = toTop + TRACK_HEIGHT - EVENT_LANE / 2;
                  return <path key={link.id} d={`M${x(link.from.time)} ${y1} L${x(link.from.time)} ${(y1 + y2) / 2} L${x(link.to.time)} ${(y1 + y2) / 2} L${x(link.to.time)} ${y2}`} />;
                })}
              </svg>
              {snapLine !== null ? <span className="sheet-snap" style={{ left: x(snapLine) }} /> : null}
              <span className="sheet-playhead" style={{ left: x(position) }} />
            </div>
          </div>
        </div>
      </div>
      {!small ? (
        <div
          className="sheet-overview"
          style={{ height: tracks.length * 3 + 6 }}
          onPointerDown={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            onSeek(Math.max(0, Math.min(safeDuration, ((event.clientX - rect.left) / rect.width) * safeDuration)));
          }}
        >
          {tracks.flatMap((track, row) =>
            track.clips.map((clip) => (
              <span key={`${track.id}-${clip.id}`} className="sheet-overview-clip" data-kind={track.kind} style={{ top: 3 + row * 3, left: `${(clip.start / safeDuration) * 100}%`, width: `${Math.max(0.3, ((clip.end - clip.start) / safeDuration) * 100)}%` }} />
            )),
          )}
          <span className="sheet-overview-head" style={{ left: `${(position / safeDuration) * 100}%` }} />
        </div>
      ) : null}
    </div>
  );
}
