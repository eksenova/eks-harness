import { useMemo, useRef, useState } from "react";
import { Button } from "../components/Button";
import { Select } from "../components/Form";
import { useDelayed } from "../lib/hooks";
import { localPath } from "../lib/url";
import { flatMeta } from "../lib/format";
import type { ViewerProps } from "./types";

const WIDTHS = ["fill", "375", "768", "1280"];

export function siteEntry(artifact: ViewerProps["artifact"]): string {
  const meta = flatMeta(artifact.meta);
  const fromMeta = typeof meta.entry === "string" ? (meta.entry as string) : "";
  if (fromMeta) return fromMeta.replace(/^\/+/, "");
  if (artifact.siteUrl) {
    const path = localPath(artifact.siteUrl).split("?")[0];
    const match = path.match(/^\/(?:site|s)\/[^/]+\/(.*)$/);
    if (match && match[1]) return decodeURIComponent(match[1]);
  }
  if (artifact.kind === "dom") return artifact.filename;
  return "index.html";
}

export function siteHtmlFiles(artifact: ViewerProps["artifact"], entry: string): string[] {
  const raw = flatMeta(artifact.meta).files;
  const files: string[] = Array.isArray(raw)
    ? raw.map((item) => (typeof item === "string" ? item : typeof item === "object" && item && typeof (item as { path?: unknown }).path === "string" ? (item as { path: string }).path : "")).filter(Boolean)
    : [];
  const html = files.filter((file) => /\.html?$/i.test(file));
  if (!html.includes(entry)) html.unshift(entry);
  return html;
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function FrameViewer({ artifact, siteBase, params, setParams }: ViewerProps) {
  const entry = siteEntry(artifact);
  const files = useMemo(() => siteHtmlFiles(artifact, entry), [artifact, entry]);
  const path = params.path && files.includes(params.path) ? params.path : entry;
  const vw = WIDTHS.includes(params.vw ?? "") ? (params.vw as string) : "fill";
  const base = siteBase ?? `/site/${encodeURIComponent(artifact.id)}/`;
  const src = base + encodePath(path);
  const [nonce, setNonce] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const showLoading = useDelayed(loading);
  const frameRef = useRef<HTMLIFrameElement>(null);

  const note =
    artifact.kind === "mhtml"
      ? "Preview of the unpacked snapshot with scripts off. Download the .mhtml file for the original."
      : artifact.kind === "dom"
        ? "The page's DOM as captured, with scripts off, so it shows the page as it was."
        : "Sandboxed: scripts run, but the page cannot reach this app or your session.";

  return (
    <div className="viewer">
      <div className="viewer-toolbar">
        {files.length > 1 ? (
          <Select value={path} onChange={(event) => setParams({ path: event.target.value === entry ? null : event.target.value }, { replace: true })} aria-label="Page in the site" className="mono">
            {files.map((file) => (
              <option key={file} value={file}>
                {file}
              </option>
            ))}
          </Select>
        ) : (
          <span className="mono frame-path">{path}</span>
        )}
        <Select value={vw} onChange={(event) => setParams({ vw: event.target.value === "fill" ? null : event.target.value }, { replace: true })} aria-label="Viewport width">
          <option value="fill">Fill</option>
          <option value="375">375 px</option>
          <option value="768">768 px</option>
          <option value="1280">1280 px</option>
        </Select>
        <Button
          onClick={() => {
            setLoading(true);
            setFailed(false);
            setNonce((n) => n + 1);
          }}
        >
          Reload
        </Button>
        <a className="link" href={src} target="_blank" rel="noopener noreferrer">
          Open in new tab
        </a>
      </div>
      <p className="viewer-note">{note}</p>
      <div className={`frame-stage ${vw === "fill" ? "" : "is-fixed"}`}>
        {showLoading && !failed ? <p className="stage-message muted">Loading page{"…"}</p> : null}
        {failed ? (
          <div className="notice notice--error" role="alert">
            <div>
              <span className="notice-lead">The page could not be loaded in the preview.</span> Open it in a new tab instead.{" "}
              <a className="link" href={src} target="_blank" rel="noopener noreferrer">
                Open in new tab
              </a>
            </div>
          </div>
        ) : null}
        <iframe
          key={`${src}#${nonce}`}
          ref={frameRef}
          title={`Preview of ${artifact.filename}`}
          src={src}
          className="frame-iframe"
          style={vw === "fill" ? undefined : { width: `${vw}px` }}
          sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"
          referrerPolicy="no-referrer"
          onLoad={() => setLoading(false)}
          onError={() => {
            setLoading(false);
            setFailed(true);
          }}
        />
      </div>
    </div>
  );
}
