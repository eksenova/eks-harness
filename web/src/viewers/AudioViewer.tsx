import { useEffect, useRef, useState } from "react";
import { formatSeconds, formatSize } from "../lib/format";
import { MediaChrome, useMedia, useMediaKeys } from "./media";
import type { ViewerProps } from "./types";

const DECODE_LIMIT_BYTES = 256 * 1024 * 1024;
const PEAK_COLUMNS = 2000;
const SPECTRUM_BARS = 64;

type Peaks = { min: Float32Array; max: Float32Array };

function computePeaks(buffer: AudioBuffer, columns: number): Peaks {
  const length = buffer.length;
  const count = Math.max(1, Math.min(columns, length));
  const min = new Float32Array(count);
  const max = new Float32Array(count);
  const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) => buffer.getChannelData(index));
  const size = length / count;
  for (let column = 0; column < count; column += 1) {
    const start = Math.floor(column * size);
    const end = Math.max(start + 1, Math.floor((column + 1) * size));
    let low = 0;
    let high = 0;
    for (const data of channels) {
      for (let index = start; index < end; index += 1) {
        const value = data[index];
        if (value < low) low = value;
        if (value > high) high = value;
      }
    }
    min[column] = low;
    max[column] = high;
  }
  return { min, max };
}

function token(element: Element, name: string, fallback: string): string {
  return getComputedStyle(element).getPropertyValue(name).trim() || fallback;
}

function sizeCanvas(canvas: HTMLCanvasElement): { width: number; height: number; ratio: number } {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  return { width, height, ratio };
}

function drawWaveform(canvas: HTMLCanvasElement, peaks: Peaks | null, progress: number): void {
  const context = canvas.getContext("2d");
  if (!context) return;
  const { width, height, ratio } = sizeCanvas(canvas);
  context.clearRect(0, 0, width, height);
  const played = token(canvas, "--mark", "#d4a017");
  const rest = token(canvas, "--stage-ink-2", "#8a8a8a");
  const rule = token(canvas, "--stage-rule", "#444444");
  const middle = height / 2;
  context.fillStyle = rule;
  context.fillRect(0, Math.round(middle), width, Math.max(1, Math.round(ratio)));
  if (!peaks) return;
  const count = peaks.min.length;
  const bar = Math.max(1, Math.round(2 * ratio));
  const gap = Math.max(1, Math.round(ratio));
  const columns = Math.max(1, Math.floor(width / (bar + gap)));
  const cut = progress * width;
  for (let column = 0; column < columns; column += 1) {
    const from = Math.floor((column / columns) * count);
    const to = Math.max(from + 1, Math.floor(((column + 1) / columns) * count));
    let low = 0;
    let high = 0;
    for (let index = from; index < to; index += 1) {
      if (peaks.min[index] < low) low = peaks.min[index];
      if (peaks.max[index] > high) high = peaks.max[index];
    }
    const x = column * (bar + gap);
    const top = middle - Math.max(high, 0.004) * middle * 0.95;
    const bottom = middle - Math.min(low, -0.004) * middle * 0.95;
    context.fillStyle = x + bar / 2 <= cut ? played : rest;
    context.fillRect(x, top, bar, Math.max(ratio, bottom - top));
  }
  context.fillStyle = token(canvas, "--stage-ink", "#ffffff");
  context.fillRect(Math.min(width - ratio, Math.round(cut)), 0, Math.max(1, Math.round(ratio)), height);
}

function drawSpectrum(canvas: HTMLCanvasElement, bins: Uint8Array | null): void {
  const context = canvas.getContext("2d");
  if (!context) return;
  const { width, height, ratio } = sizeCanvas(canvas);
  context.clearRect(0, 0, width, height);
  const gap = Math.max(1, Math.round(2 * ratio));
  const bar = Math.max(1, (width - gap * (SPECTRUM_BARS - 1)) / SPECTRUM_BARS);
  context.fillStyle = token(canvas, "--mark", "#d4a017");
  for (let index = 0; index < SPECTRUM_BARS; index += 1) {
    let value = 0;
    if (bins) {
      const from = Math.floor(Math.pow(index / SPECTRUM_BARS, 2) * bins.length);
      const to = Math.max(from + 1, Math.floor(Math.pow((index + 1) / SPECTRUM_BARS, 2) * bins.length));
      for (let bin = from; bin < to; bin += 1) value = Math.max(value, bins[bin]);
    }
    const level = Math.max(ratio * 2, (value / 255) * height);
    context.fillRect(index * (bar + gap), height - level, bar, level);
  }
}

export function AudioViewer({ artifact, rawUrl, params, setParams, shareMode, onEnded, keyboard = true }: ViewerProps) {
  const ref = useRef<HTMLAudioElement>(null);
  const regionRef = useRef<HTMLDivElement>(null);
  const waveRef = useRef<HTMLCanvasElement>(null);
  const spectrumRef = useRef<HTMLCanvasElement>(null);
  const graph = useRef<{ context: AudioContext; analyser: AnalyserNode; bins: Uint8Array<ArrayBuffer> } | null>(null);
  const dragging = useRef(false);
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [waveState, setWaveState] = useState<"loading" | "ready" | "skipped" | "failed">("loading");
  const [failed, setFailed] = useState(false);
  const media = useMedia(ref, regionRef, {
    initialTime: Number(params.t) || 0,
    initialDuration: (artifact.durationMs ?? 0) / 1000,
    onPause: (time) => setParams({ t: time > 0 ? time.toFixed(1) : null }, { replace: true }),
    onEnded: () => {
      if (!shareMode) onEnded?.();
    },
  });
  useMediaKeys(media, ref, regionRef, keyboard);

  useEffect(() => {
    if (artifact.size > DECODE_LIMIT_BYTES) {
      setWaveState("skipped");
      return;
    }
    const controller = new AbortController();
    setWaveState("loading");
    fetch(rawUrl, { credentials: "same-origin", signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.arrayBuffer();
      })
      .then((data) => new OfflineAudioContext(1, 1, 44100).decodeAudioData(data))
      .then((buffer) => {
        if (controller.signal.aborted) return;
        setPeaks(computePeaks(buffer, PEAK_COLUMNS));
        setWaveState("ready");
      })
      .catch(() => {
        if (!controller.signal.aborted) setWaveState("failed");
      });
    return () => controller.abort();
  }, [rawUrl, artifact.size]);

  const progress = media.duration > 0 ? Math.min(1, media.time / media.duration) : 0;

  useEffect(() => {
    const canvas = waveRef.current;
    if (canvas) drawWaveform(canvas, peaks, progress);
  }, [peaks, progress, media.fullscreen]);

  useEffect(() => {
    const canvas = waveRef.current;
    if (!canvas) return;
    const observer = new ResizeObserver(() => {
      drawWaveform(canvas, peaks, progress);
      if (spectrumRef.current) drawSpectrum(spectrumRef.current, null);
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [peaks, progress]);

  useEffect(() => {
    const canvas = spectrumRef.current;
    if (!canvas) return;
    if (!media.playing) {
      drawSpectrum(canvas, null);
      return;
    }
    let frame = 0;
    const tick = () => {
      const current = graph.current;
      if (current) {
        current.analyser.getByteFrequencyData(current.bins);
        drawSpectrum(canvas, current.bins);
      }
      frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [media.playing]);

  useEffect(
    () => () => {
      void graph.current?.context.close().catch(() => undefined);
      graph.current = null;
    },
    [],
  );

  const ensureGraph = () => {
    const element = ref.current;
    if (!element || new URL(rawUrl, window.location.href).origin !== window.location.origin) return;
    if (graph.current) {
      if (graph.current.context.state === "suspended") void graph.current.context.resume();
      return;
    }
    try {
      const context = new AudioContext();
      const source = context.createMediaElementSource(element);
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0.78;
      source.connect(analyser);
      analyser.connect(context.destination);
      graph.current = { context, analyser, bins: new Uint8Array(analyser.frequencyBinCount) };
    } catch {
      graph.current = null;
    }
  };

  const seekFromPointer = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    media.seekTo(fraction * media.duration);
  };

  const waveNote = {
    loading: "Reading the waveform…",
    ready: "",
    skipped: `The file is ${formatSize(artifact.size)}, too large to draw its waveform here.`,
    failed: "This browser could not decode the audio to draw its waveform.",
  }[waveState];

  return (
    <div className="viewer media-viewer audio-viewer" ref={regionRef} data-fullscreen={media.fullscreen ? "" : undefined}>
      <div className="audio-stage">
        {failed ? (
          <p className="stage-message">The audio could not be played in this browser. Download it instead.</p>
        ) : (
          <>
            <canvas
              ref={waveRef}
              className="audio-wave"
              role="slider"
              tabIndex={0}
              aria-label="Waveform, click to seek"
              aria-valuemin={0}
              aria-valuemax={Math.round(media.duration)}
              aria-valuenow={Math.round(media.time)}
              aria-valuetext={`${formatSeconds(media.time)} of ${formatSeconds(media.duration)}`}
              onPointerDown={(event) => {
                if (event.button !== 0) return;
                dragging.current = true;
                event.currentTarget.setPointerCapture(event.pointerId);
                seekFromPointer(event);
              }}
              onPointerMove={(event) => {
                if (dragging.current) seekFromPointer(event);
              }}
              onPointerUp={() => {
                dragging.current = false;
              }}
              onPointerCancel={() => {
                dragging.current = false;
              }}
            />
            {waveNote ? <p className="audio-note">{waveNote}</p> : null}
            <canvas ref={spectrumRef} className="audio-spectrum" aria-hidden="true" onClick={media.toggle} />
            <audio
              ref={ref}
              src={rawUrl}
              preload="metadata"
              {...media.bind}
              onPlay={() => {
                ensureGraph();
                media.bind.onPlay();
              }}
              onError={() => setFailed(true)}
            />
          </>
        )}
      </div>
      <MediaChrome media={media} label="audio" />
      {!shareMode && !media.fullscreen ? <p className="viewer-note">{artifact.seen ? "Seen" : "Marked as seen when played to the end."}</p> : null}
    </div>
  );
}
