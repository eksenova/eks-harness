import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "../components/Button";
import { formatClock } from "../lib/format";
import { useDelayed } from "../lib/hooks";

type LiveState = "connecting" | "live" | "reconnecting" | "paused" | "stopped" | "failed" | "ended";

interface LiveFrameProps {
  src: string | null;
  name: string;
  running: boolean;
  aspect: string;
  viewers?: number;
  onStart?: () => void;
  startLabel?: string;
  endedAt?: Date | null;
}

function indexOf(haystack: Uint8Array, needle: number[], from: number): number {
  outer: for (let i = from; i <= haystack.length - needle.length; i += 1) {
    for (let j = 0; j < needle.length; j += 1) if (haystack[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

const HEADER_END = [13, 10, 13, 10];
const SOI = [0xff, 0xd8];
const EOI = [0xff, 0xd9];
const decoder = new TextDecoder();

async function readMjpeg(response: Response, onFrame: (jpeg: Uint8Array) => void, signal: AbortSignal): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("the stream has no body");
  let buffer: Uint8Array<ArrayBuffer> = new Uint8Array(0);
  const append = (chunk: Uint8Array) => {
    const next = new Uint8Array(buffer.length + chunk.length);
    next.set(buffer);
    next.set(chunk, buffer.length);
    buffer = next;
  };
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) return;
    append(value);
    for (;;) {
      const soi = indexOf(buffer, SOI, 0);
      if (soi < 0) {
        if (buffer.length > 1) buffer = buffer.slice(buffer.length - 1);
        break;
      }
      const headerStart = soi > 0 ? indexOf(buffer.subarray(0, soi), HEADER_END, 0) : -1;
      let length = -1;
      if (headerStart >= 0 || soi > 0) {
        const head = decoder.decode(buffer.subarray(0, soi));
        const match = head.match(/content-length:\s*(\d+)/i);
        if (match) length = Number(match[1]);
      }
      if (length > 0) {
        if (buffer.length < soi + length) break;
        onFrame(buffer.slice(soi, soi + length));
        buffer = buffer.slice(soi + length);
        continue;
      }
      const eoi = indexOf(buffer, EOI, soi + 2);
      if (eoi < 0) {
        if (soi > 0) buffer = buffer.slice(soi);
        break;
      }
      onFrame(buffer.slice(soi, eoi + 2));
      buffer = buffer.slice(eoi + 2);
    }
    if (buffer.length > 32 * 1024 * 1024) buffer = new Uint8Array(0);
  }
  await reader.cancel().catch(() => undefined);
}

export function LiveFrame({ src, name, running, aspect, viewers, onStart, startLabel, endedAt }: LiveFrameProps) {
  const [state, setState] = useState<LiveState>(running ? "connecting" : "stopped");
  const [wanted, setWanted] = useState(true);
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [lastFrame, setLastFrame] = useState<Date | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const [hidden, setHidden] = useState(document.visibilityState === "hidden");
  const [hiddenLong, setHiddenLong] = useState(false);
  const lastFrameAt = useRef(0);
  const lastLabelUpdate = useRef(0);
  const urlRef = useRef<string | null>(null);
  const showConnecting = useDelayed(state === "connecting");
  const wasRunning = useRef(running);
  const [ended, setEnded] = useState<Date | null>(null);

  useEffect(() => {
    if (wasRunning.current && !running) setEnded(endedAt ?? new Date());
    if (running) setEnded(null);
    wasRunning.current = running;
  }, [running, endedAt]);

  useEffect(() => {
    const onVisibility = () => setHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (!hidden) {
      setHiddenLong(false);
      return;
    }
    const timer = window.setTimeout(() => setHiddenLong(true), 10_000);
    return () => window.clearTimeout(timer);
  }, [hidden]);

  const active = Boolean(src) && running && wanted && !hiddenLong && state !== "failed";

  useEffect(() => {
    if (!active || !src) return;
    const controller = new AbortController();
    let stopped = false;
    setState(attempt === 0 ? "connecting" : "reconnecting");
    (async () => {
      try {
        const response = await fetch(src, { credentials: "same-origin", signal: controller.signal, headers: { Accept: "multipart/x-mixed-replace" } });
        if (!response.ok) {
          let message = `the daemon returned ${response.status}`;
          try {
            const body = (await response.json()) as { message?: string };
            if (body.message) message = body.message.replace(/\.$/, "");
          } catch {
            message = `the daemon returned ${response.status}`;
          }
          if (response.status === 409 || response.status === 404) {
            stopped = true;
            setState("ended");
            return;
          }
          stopped = true;
          setError(message);
          setState("failed");
          return;
        }
        await readMjpeg(
          response,
          (jpeg) => {
            const blob = new Blob([jpeg as Uint8Array<ArrayBuffer>], { type: "image/jpeg" });
            const url = URL.createObjectURL(blob);
            if (urlRef.current) URL.revokeObjectURL(urlRef.current);
            urlRef.current = url;
            setFrameUrl(url);
            const t = Date.now();
            lastFrameAt.current = t;
            if (t - lastLabelUpdate.current >= 1000) {
              lastLabelUpdate.current = t;
              setLastFrame(new Date(t));
            }
            setState("live");
          },
          controller.signal,
        );
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      }
      if (!controller.signal.aborted && !stopped) {
        window.setTimeout(() => setAttempt((a) => a + 1), Math.min(10_000, 1000 * (attempt + 1)));
      }
    })();
    return () => {
      controller.abort();
    };
  }, [active, src, attempt]);

  useEffect(() => {
    if (state !== "live") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [state]);

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const retry = useCallback(() => {
    setError("");
    setState("connecting");
    setAttempt(0);
    setWanted(true);
  }, []);

  const stalledSeconds = state === "live" && lastFrameAt.current ? Math.floor((now - lastFrameAt.current) / 1000) : 0;

  let label: React.ReactNode = null;
  let button: React.ReactNode = null;
  if (!running) {
    if (ended) {
      label = <span className="strong">Stream ended: {name} was shut down at {formatClock(ended)}.</span>;
      button = onStart ? <Button onClick={onStart}>{startLabel ?? "Start viewing"}</Button> : null;
    } else {
      label = <span>{name} is shut down.</span>;
      button = onStart ? <Button onClick={onStart}>{startLabel ?? `Start ${name}`}</Button> : null;
    }
  } else if (!wanted) {
    label = <span>Not viewing</span>;
    button = <Button onClick={retry}>Start viewing</Button>;
  } else if (hiddenLong || hidden) {
    label = <span>Paused while this tab is hidden</span>;
  } else if (state === "failed") {
    label = <span className="strong danger">Live view failed: {error || "the stream stopped"}.</span>;
    button = <Button onClick={retry}>Try again</Button>;
  } else if (state === "ended") {
    label = <span className="strong">Stream ended: {name} is not running.</span>;
    button = <Button onClick={retry}>Start viewing</Button>;
  } else if (state === "reconnecting") {
    label = <span>Reconnecting (attempt {attempt})</span>;
    button = <Button onClick={() => setWanted(false)}>Stop viewing</Button>;
  } else if (state === "connecting") {
    label = <span>{showConnecting ? "Connecting…" : " "}</span>;
    button = <Button onClick={() => setWanted(false)}>Stop viewing</Button>;
  } else if (stalledSeconds >= 5) {
    label = <span className="strong">No new frames for {stalledSeconds} s</span>;
    button = <Button onClick={() => setWanted(false)}>Stop viewing</Button>;
  } else {
    label = <span className="strong">Live</span>;
    button = <Button onClick={() => setWanted(false)}>Stop viewing</Button>;
  }

  const showImage = running && wanted && frameUrl && state !== "failed";

  return (
    <div className="live">
      <div className="live-stage" style={{ aspectRatio: aspect }}>
        {showImage ? <img src={frameUrl} alt={`Live view of ${name}`} className="live-img" /> : null}
      </div>
      <div className="live-status">
        {label}
        {lastFrame && running && wanted && state === "live" ? <span>Last frame {formatClock(lastFrame, true)}</span> : null}
        {viewers ? <span>{viewers === 1 ? "1 viewer" : `${viewers.toLocaleString("en-US")} viewers`}</span> : null}
        {button}
      </div>
      <p className="viewer-note">View only. Input is not forwarded to the device.</p>
    </div>
  );
}
