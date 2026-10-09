import { useEffect, useRef, useState } from "react";
import { IconButton } from "../components/Button";
import { Select } from "../components/Form";
import { formatSeconds } from "../lib/format";
import type { ViewerProps } from "./types";

export function VideoViewer({ artifact, rawUrl, params, setParams, shareMode, onEnded }: ViewerProps) {
  const ref = useRef<HTMLVideoElement>(null);
  const regionRef = useRef<HTMLDivElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(Number(params.t) || 0);
  const [duration, setDuration] = useState((artifact.durationMs ?? 0) / 1000);
  const [rate, setRate] = useState("1");
  const [failed, setFailed] = useState(false);
  const initialT = useRef(Number(params.t) || 0);

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    video.playbackRate = Number(rate);
  }, [rate]);

  const toggle = () => {
    const video = ref.current;
    if (!video) return;
    if (video.paused) void video.play();
    else video.pause();
  };

  const seekBy = (delta: number) => {
    const video = ref.current;
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(video.duration || duration, video.currentTime + delta));
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    const target = event.target as HTMLElement;
    if (target.tagName === "SELECT") return;
    const onRange = target instanceof HTMLInputElement && target.type === "range";
    if ((event.key === " " && !onRange && target.tagName !== "BUTTON") || event.key === "k") {
      event.preventDefault();
      event.stopPropagation();
      toggle();
    } else if (event.shiftKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
      event.preventDefault();
      event.stopPropagation();
      seekBy(event.key === "ArrowLeft" ? -5 : 5);
    } else if ((event.key === "," || event.key === ".") && ref.current?.paused) {
      event.preventDefault();
      event.stopPropagation();
      seekBy(event.key === "," ? -1 / 30 : 1 / 30);
    } else if (event.key === "f") {
      event.preventDefault();
      event.stopPropagation();
      const video = ref.current;
      if (document.fullscreenElement) void document.exitFullscreen();
      else void video?.requestFullscreen?.();
    } else if (onRange && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
      event.stopPropagation();
    } else if (onRange && (event.key === "PageUp" || event.key === "PageDown")) {
      event.preventDefault();
      event.stopPropagation();
      seekBy(event.key === "PageUp" ? 10 : -10);
    }
  };

  const played = duration > 0 ? (time / duration) * 100 : 0;
  const aspect = artifact.width && artifact.height ? `${artifact.width} / ${artifact.height}` : "16 / 9";

  return (
    <div className="viewer" ref={regionRef} onKeyDown={onKeyDown}>
      <div className="video-stage">
        {failed ? (
          <p className="stage-message">The video could not be played in this browser. Download it instead.</p>
        ) : (
          <video
            ref={ref}
            className="video"
            src={rawUrl}
            poster={artifact.thumbnailUrl ?? undefined}
            preload="metadata"
            playsInline
            style={{ aspectRatio: aspect }}
            onClick={toggle}
            onPlay={() => setPlaying(true)}
            onPause={() => {
              setPlaying(false);
              const video = ref.current;
              if (video && !video.ended) setParams({ t: video.currentTime > 0 ? video.currentTime.toFixed(1) : null }, { replace: true });
            }}
            onTimeUpdate={() => setTime(ref.current?.currentTime ?? 0)}
            onLoadedMetadata={() => {
              const video = ref.current;
              if (!video) return;
              if (Number.isFinite(video.duration)) setDuration(video.duration);
              if (initialT.current > 0) video.currentTime = Math.min(initialT.current, video.duration || initialT.current);
            }}
            onEnded={() => {
              setPlaying(false);
              if (!shareMode) onEnded?.();
            }}
            onError={() => setFailed(true)}
          />
        )}
      </div>
      <div className="video-chrome">
        <IconButton icon={playing ? "pause" : "play"} label={playing ? "Pause" : "Play"} shortcut="Space" onClick={toggle} />
        <span className="video-time">
          {formatSeconds(time)} / {formatSeconds(duration)}
        </span>
        <input
          type="range"
          className="seek"
          min={0}
          max={duration || 0}
          step={1}
          value={Math.min(time, duration || 0)}
          aria-label="Seek"
          aria-valuetext={`${formatSeconds(time)} of ${formatSeconds(duration)}`}
          style={{ "--played": `${played}%` } as React.CSSProperties}
          onChange={(event) => {
            const video = ref.current;
            const value = Number(event.target.value);
            if (video) video.currentTime = value;
            setTime(value);
          }}
        />
        <Select value={rate} onChange={(event) => setRate(event.target.value)} aria-label="Playback speed" wrapClassName="speed-select">
          <option value="0.5">0.5x</option>
          <option value="1">1x</option>
          <option value="1.5">1.5x</option>
          <option value="2">2x</option>
        </Select>
      </div>
      {!shareMode ? <p className="viewer-note">{artifact.seen ? "Seen" : "Marked as seen when played to the end."}</p> : null}
    </div>
  );
}
