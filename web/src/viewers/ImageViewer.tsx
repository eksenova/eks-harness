import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Segmented } from "../components/Button";
import { shortcutAllowed, useIsSmall, useKeydown } from "../lib/hooks";
import type { ViewerProps } from "./types";

const STEPS = [25, 50, 75, 100, 150, 200, 300, 400];

export function ImageViewer({ artifact, rawUrl, params, setParams, keyboard = true }: ViewerProps) {
  const zoomParam = params.zoom && STEPS.includes(Number(params.zoom)) ? Number(params.zoom) : null;
  const zoom = zoomParam;
  const stageRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(artifact.width && artifact.height ? { w: artifact.width, h: artifact.height } : null);
  const [fitPct, setFitPct] = useState(100);
  const [failed, setFailed] = useState(false);
  const drag = useRef<{ x: number; y: number; left: number; top: number; moved: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);
  const anchor = useRef<{ fx: number; fy: number; px: number; py: number } | null>(null);
  const small = useIsSmall();

  const setZoom = useCallback(
    (value: number | null, point?: { x: number; y: number }) => {
      const stage = stageRef.current;
      const img = imgRef.current;
      if (stage && img && point) {
        const rect = img.getBoundingClientRect();
        anchor.current = {
          fx: Math.min(1, Math.max(0, (point.x - rect.left) / Math.max(1, rect.width))),
          fy: Math.min(1, Math.max(0, (point.y - rect.top) / Math.max(1, rect.height))),
          px: point.x - stage.getBoundingClientRect().left,
          py: point.y - stage.getBoundingClientRect().top,
        };
      } else {
        anchor.current = null;
      }
      setParams({ zoom: value === null ? null : String(value) }, { replace: true });
    },
    [setParams],
  );

  useLayoutEffect(() => {
    const stage = stageRef.current;
    const img = imgRef.current;
    if (!stage || !img || !anchor.current || zoom === null) return;
    const { fx, fy, px, py } = anchor.current;
    stage.scrollLeft = img.offsetLeft + fx * img.offsetWidth - px;
    stage.scrollTop = img.offsetTop + fy * img.offsetHeight - py;
    anchor.current = null;
  }, [zoom]);

  useEffect(() => {
    const update = () => {
      const stage = stageRef.current;
      if (!stage || !natural) return;
      const scale = Math.min(1, stage.clientWidth / natural.w, stage.clientHeight / natural.h);
      setFitPct(Math.round(scale * 100));
    };
    update();
    const observer = new ResizeObserver(update);
    if (stageRef.current) observer.observe(stageRef.current);
    return () => observer.disconnect();
  }, [natural]);

  const current = zoom ?? fitPct;
  const stepIn = () => setZoom(STEPS.find((s) => s > current) ?? STEPS[STEPS.length - 1]);
  const stepOut = () => {
    const lower = [...STEPS].reverse().find((s) => s < current);
    setZoom(lower ?? STEPS[0]);
  };

  useKeydown((event) => {
    if (!keyboard || !shortcutAllowed(event)) return;
    if (event.key === "0") setZoom(null);
    else if (event.key === "1") setZoom(100);
    else if (event.key === "+" || event.key === "=") stepIn();
    else if (event.key === "-") stepOut();
    else return;
    event.preventDefault();
  });

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const base = zoom ?? fitPct;
      const target = event.deltaY < 0 ? STEPS.find((s) => s > base) ?? 400 : [...STEPS].reverse().find((s) => s < base) ?? 25;
      setZoom(target, { x: event.clientX, y: event.clientY });
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, [zoom, fitPct, setZoom]);

  const onPointerDown = (event: React.PointerEvent) => {
    const stage = stageRef.current;
    if (!stage || event.button !== 0) return;
    drag.current = { x: event.clientX, y: event.clientY, left: stage.scrollLeft, top: stage.scrollTop, moved: false };
  };
  const onPointerMove = (event: React.PointerEvent) => {
    const stage = stageRef.current;
    const state = drag.current;
    if (!stage || !state || zoom === null) return;
    const dx = event.clientX - state.x;
    const dy = event.clientY - state.y;
    if (!state.moved && Math.abs(dx) + Math.abs(dy) > 4) {
      state.moved = true;
      setDragging(true);
      stage.setPointerCapture(event.pointerId);
    }
    if (state.moved) {
      stage.scrollLeft = state.left - dx;
      stage.scrollTop = state.top - dy;
    }
  };
  const onPointerUp = (event: React.PointerEvent) => {
    const state = drag.current;
    drag.current = null;
    setDragging(false);
    if (state && !state.moved && event.target === imgRef.current) {
      setZoom(zoom === null ? 100 : null, { x: event.clientX, y: event.clientY });
    }
  };

  const width = natural && zoom !== null ? Math.round((natural.w * zoom) / 100) : undefined;
  const alt = artifact.caption || artifact.filename;

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        <Segmented
          label="Image size"
          value={zoom === null ? "fit" : zoom === 100 ? "actual" : "other"}
          onChange={(value) => setZoom(value === "fit" ? null : 100)}
          options={[
            { value: "fit", label: "Fit", title: "Fit (0)" },
            { value: "actual", label: "Actual size", title: "Actual size (1)" },
          ]}
        />
        {!small ? (
          <>
            <Button onClick={stepOut} disabled={current <= STEPS[0]} title="Zoom out (-)">
              Zoom out
            </Button>
            <Button onClick={stepIn} disabled={current >= STEPS[STEPS.length - 1]} title="Zoom in (+)">
              Zoom in
            </Button>
          </>
        ) : null}
        <span className="muted" aria-live="polite">
          {current}%
        </span>
      </div>
      <div
        ref={stageRef}
        className={`image-stage ${zoom === null ? "is-fit" : "is-zoomed"} ${dragging ? "is-dragging" : ""}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          drag.current = null;
          setDragging(false);
        }}
      >
        {failed ? (
          <p className="stage-message">The image could not be loaded. Download it instead.</p>
        ) : (
          <img
            ref={imgRef}
            src={rawUrl}
            alt={alt}
            draggable={false}
            decoding="async"
            className={`image ${zoom !== null && zoom > 200 ? "pixelated" : ""}`}
            style={zoom !== null ? { width, maxWidth: "none", maxHeight: "none" } : undefined}
            onLoad={(event) => {
              const img = event.currentTarget;
              setNatural({ w: img.naturalWidth, h: img.naturalHeight });
            }}
            onError={() => setFailed(true)}
          />
        )}
      </div>
    </div>
  );
}
