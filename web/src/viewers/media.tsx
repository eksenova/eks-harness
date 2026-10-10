import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { IconButton } from "../components/Button";
import { Select } from "../components/Form";
import { formatSeconds } from "../lib/format";
import { isTypingTarget, overlayOpen, useKeydown } from "../lib/hooks";

const VOLUME_KEY = "eks.media.volume";
const MUTED_KEY = "eks.media.muted";
const FRAME = 1 / 30;

function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    return;
  }
}

function storedVolume(): number {
  const value = Number(readStored(VOLUME_KEY));
  return readStored(VOLUME_KEY) !== null && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 1;
}

export interface MediaState {
  playing: boolean;
  time: number;
  duration: number;
  volume: number;
  muted: boolean;
  rate: string;
  fullscreen: boolean;
  setRate: (rate: string) => void;
  setVolume: (volume: number) => void;
  toggleMute: () => void;
  toggle: () => void;
  seekTo: (seconds: number) => void;
  seekBy: (delta: number) => void;
  toggleFullscreen: () => void;
  bind: {
    onPlay: () => void;
    onPause: () => void;
    onTimeUpdate: () => void;
    onLoadedMetadata: () => void;
    onDurationChange: () => void;
    onVolumeChange: () => void;
    onEnded: () => void;
  };
}

export function useMedia(
  ref: RefObject<HTMLMediaElement | null>,
  regionRef: RefObject<HTMLElement | null>,
  options: { initialTime: number; initialDuration: number; onPause?: (time: number) => void; onEnded?: () => void },
): MediaState {
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(options.initialTime);
  const [duration, setDuration] = useState(options.initialDuration);
  const [volume, setVolumeState] = useState(storedVolume);
  const [muted, setMuted] = useState(() => readStored(MUTED_KEY) === "1");
  const [rate, setRate] = useState("1");
  const [fullscreen, setFullscreen] = useState(false);
  const initialTime = useRef(options.initialTime);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    const media = ref.current;
    if (!media) return;
    media.volume = volume;
    media.muted = muted;
  }, [ref, volume, muted]);

  useEffect(() => {
    const media = ref.current;
    if (media) media.playbackRate = Number(rate);
  }, [ref, rate]);

  useEffect(() => {
    const onChange = () => setFullscreen(Boolean(regionRef.current && document.fullscreenElement === regionRef.current));
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [regionRef]);

  const limit = useCallback(() => {
    const media = ref.current;
    return media && Number.isFinite(media.duration) ? media.duration : duration;
  }, [ref, duration]);

  const seekTo = useCallback(
    (seconds: number) => {
      const media = ref.current;
      if (!media) return;
      const target = Math.max(0, Math.min(limit() || 0, seconds));
      media.currentTime = target;
      setTime(target);
    },
    [ref, limit],
  );

  const seekBy = useCallback((delta: number) => seekTo((ref.current?.currentTime ?? 0) + delta), [ref, seekTo]);

  const toggle = useCallback(() => {
    const media = ref.current;
    if (!media) return;
    if (media.paused || media.ended) void media.play().catch(() => undefined);
    else media.pause();
  }, [ref]);

  const setVolume = useCallback((value: number) => {
    const next = Math.max(0, Math.min(1, Math.round(value * 100) / 100));
    setVolumeState(next);
    writeStored(VOLUME_KEY, String(next));
    if (next > 0) {
      setMuted(false);
      writeStored(MUTED_KEY, "0");
    }
  }, []);

  const toggleMute = useCallback(() => {
    setMuted((current) => {
      const next = !current;
      writeStored(MUTED_KEY, next ? "1" : "0");
      return next;
    });
    if (volume === 0) setVolume(0.5);
  }, [volume, setVolume]);

  const toggleFullscreen = useCallback(() => {
    const region = regionRef.current;
    if (!region) return;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    else void region.requestFullscreen?.().catch(() => undefined);
  }, [regionRef]);

  const bind = {
    onPlay: () => setPlaying(true),
    onPause: () => {
      setPlaying(false);
      const media = ref.current;
      if (media && !media.ended) optionsRef.current.onPause?.(media.currentTime);
    },
    onTimeUpdate: () => setTime(ref.current?.currentTime ?? 0),
    onLoadedMetadata: () => {
      const media = ref.current;
      if (!media) return;
      if (Number.isFinite(media.duration)) setDuration(media.duration);
      media.volume = volume;
      media.muted = muted;
      media.playbackRate = Number(rate);
      if (initialTime.current > 0) {
        media.currentTime = Math.min(initialTime.current, Number.isFinite(media.duration) ? media.duration : initialTime.current);
        initialTime.current = 0;
      }
    },
    onDurationChange: () => {
      const media = ref.current;
      if (media && Number.isFinite(media.duration)) setDuration(media.duration);
    },
    onVolumeChange: () => {
      const media = ref.current;
      if (!media) return;
      if (media.muted !== muted) {
        setMuted(media.muted);
        writeStored(MUTED_KEY, media.muted ? "1" : "0");
      }
    },
    onEnded: () => {
      setPlaying(false);
      optionsRef.current.onEnded?.();
    },
  };

  return { playing, time, duration, volume, muted, rate, fullscreen, setRate, setVolume, toggleMute, toggle, seekTo, seekBy, toggleFullscreen, bind };
}

function ownsKey(event: KeyboardEvent, region: HTMLElement | null, keyboard: boolean): boolean {
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  if (overlayOpen()) return false;
  const target = event.target as HTMLElement | null;
  const inside = Boolean(region && target && region.contains(target));
  if (!keyboard && !inside) return false;
  if (target && isTypingTarget(target)) return false;
  if (target?.tagName === "SELECT") return false;
  if (target && !inside && target !== document.body && target.closest?.("input, textarea, select, [contenteditable], [role='menu'], [role='listbox'], [role='dialog']")) return false;
  return true;
}

export function useMediaKeys(media: MediaState, ref: RefObject<HTMLMediaElement | null>, regionRef: RefObject<HTMLElement | null>, keyboard: boolean): void {
  useKeydown((event) => {
    if (!ownsKey(event, regionRef.current, keyboard)) return;
    const target = event.target as HTMLElement | null;
    const onButton = target?.tagName === "BUTTON";
    const paused = ref.current?.paused ?? true;
    const step = event.shiftKey ? 1 : 5;
    switch (event.key) {
      case " ":
        if (onButton) return;
        media.toggle();
        break;
      case "k":
        media.toggle();
        break;
      case "ArrowLeft":
        media.seekBy(-step);
        break;
      case "ArrowRight":
        media.seekBy(step);
        break;
      case "j":
        media.seekBy(-10);
        break;
      case "l":
        media.seekBy(10);
        break;
      case "ArrowUp":
        media.setVolume(media.volume + 0.1);
        break;
      case "ArrowDown":
        media.setVolume(media.volume - 0.1);
        break;
      case "m":
        media.toggleMute();
        break;
      case "f":
        media.toggleFullscreen();
        break;
      case "Home":
        media.seekTo(0);
        break;
      case "End":
        media.seekTo(media.duration);
        break;
      case ",":
      case ".":
        if (!paused) return;
        media.seekBy(event.key === "," ? -FRAME : FRAME);
        break;
      default:
        if (/^[0-9]$/.test(event.key) && !event.shiftKey) {
          media.seekTo((Number(event.key) / 10) * media.duration);
          break;
        }
        return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  });
}

export function MediaChrome({ media, label }: { media: MediaState; label: string }) {
  const played = media.duration > 0 ? (media.time / media.duration) * 100 : 0;
  const level = media.muted ? 0 : media.volume;
  return (
    <div className="video-chrome">
      <IconButton icon={media.playing ? "pause" : "play"} label={media.playing ? "Pause" : "Play"} shortcut="Space" onClick={media.toggle} />
      <span className="video-time">
        {formatSeconds(media.time)} / {formatSeconds(media.duration)}
      </span>
      <input
        type="range"
        className="seek"
        min={0}
        max={media.duration || 0}
        step={0.1}
        value={Math.min(media.time, media.duration || 0)}
        aria-label={`Seek ${label}`}
        aria-valuetext={`${formatSeconds(media.time)} of ${formatSeconds(media.duration)}`}
        style={{ "--played": `${played}%` } as React.CSSProperties}
        onChange={(event) => media.seekTo(Number(event.target.value))}
      />
      <div className="volume">
        <IconButton icon={level === 0 ? "volume-muted" : "volume"} label={media.muted ? "Unmute" : "Mute"} shortcut="M" onClick={media.toggleMute} />
        <input
          type="range"
          className="seek volume-slider"
          min={0}
          max={1}
          step={0.05}
          value={level}
          aria-label="Volume"
          aria-valuetext={`${Math.round(level * 100)}%`}
          title={`Volume ${Math.round(level * 100)}% (Up and Down)`}
          style={{ "--played": `${level * 100}%` } as React.CSSProperties}
          onChange={(event) => media.setVolume(Number(event.target.value))}
        />
      </div>
      <Select value={media.rate} onChange={(event) => media.setRate(event.target.value)} aria-label="Playback speed" wrapClassName="speed-select">
        <option value="0.25">0.25x</option>
        <option value="0.5">0.5x</option>
        <option value="1">1x</option>
        <option value="1.5">1.5x</option>
        <option value="2">2x</option>
      </Select>
      <IconButton icon={media.fullscreen ? "shrink" : "expand"} label={media.fullscreen ? "Exit full screen" : "Full screen"} shortcut="F" onClick={media.toggleFullscreen} />
    </div>
  );
}
