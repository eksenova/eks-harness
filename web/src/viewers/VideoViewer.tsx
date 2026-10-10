import { useRef, useState } from "react";
import { MediaChrome, useMedia, useMediaKeys } from "./media";
import type { ViewerProps } from "./types";

export function VideoViewer({ artifact, rawUrl, params, setParams, shareMode, onEnded, keyboard = true }: ViewerProps) {
  const ref = useRef<HTMLVideoElement>(null);
  const regionRef = useRef<HTMLDivElement>(null);
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

  const aspect = artifact.width && artifact.height ? `${artifact.width} / ${artifact.height}` : "16 / 9";

  return (
    <div className="viewer media-viewer" ref={regionRef} data-fullscreen={media.fullscreen ? "" : undefined}>
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
            onClick={media.toggle}
            onDoubleClick={media.toggleFullscreen}
            {...media.bind}
            onError={() => setFailed(true)}
          />
        )}
      </div>
      <MediaChrome media={media} label="video" />
      {!shareMode && !media.fullscreen ? <p className="viewer-note">{artifact.seen ? "Seen" : "Marked as seen when played to the end."}</p> : null}
    </div>
  );
}
