import type { ArtifactOut } from "../api/types";
import type { ParamPatch, SearchParams } from "../lib/url";

export interface ViewerProps {
  artifact: ArtifactOut;
  rawUrl: string;
  downloadUrl?: string;
  siteBase: string | null;
  params: SearchParams;
  setParams: (patch: ParamPatch, options?: { replace?: boolean }) => void;
  shareMode?: boolean;
  onEnded?: () => void;
  keyboard?: boolean;
}

export type ViewerKind = "image" | "video" | "frame" | "har" | "text" | "json" | "other";

const TEXT_KINDS = new Set(["a11y", "console", "log"]);
const FRAME_KINDS = new Set(["site", "dom", "mhtml"]);

export function viewerKind(artifact: Pick<ArtifactOut, "kind" | "mime" | "filename">): ViewerKind {
  const mime = (artifact.mime || "").toLowerCase();
  const name = (artifact.filename || "").toLowerCase();
  if (FRAME_KINDS.has(artifact.kind)) return "frame";
  if (artifact.kind === "screenshot" || mime.startsWith("image/")) return "image";
  if (artifact.kind === "video" || mime.startsWith("video/")) return "video";
  if (artifact.kind === "har" || name.endsWith(".har")) return "har";
  if (mime.includes("html") || /\.(html?|mhtml|svg)$/.test(name)) return "other";
  if (mime.includes("json") || name.endsWith(".json")) return TEXT_KINDS.has(artifact.kind) ? "text" : "json";
  if (TEXT_KINDS.has(artifact.kind) || mime.startsWith("text/") || /\.(txt|log|md|csv|xml|yaml|yml|ini|conf)$/.test(name)) return "text";
  return "other";
}
