import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { ApiError } from "../api/client";
import type { ArtifactOut } from "../api/types";
import { fetchers, keys } from "../api/queries";
import { ButtonLink } from "../components/Button";
import { Loading } from "../components/Notice";
import { dimensions, formatDuration, formatFull, formatFullMinutes, formatSize, kindLabel } from "../lib/format";
import { localPath, useSearchParams, useSetParams } from "../lib/url";
import { Viewer } from "../viewers/Viewer";
import { viewerKind } from "../viewers/types";

function linkStateMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.error === "share_revoked") return "This link was revoked.";
    if (err.error === "share_expired") return "This link has expired. Ask the person who shared it for a new one.";
    if (err.isNetwork) return "Could not reach the server. Try again later.";
  }
  return "This link does not exist or the file was deleted.";
}

export function SharePage({ token }: { token: string }) {
  const params = useSearchParams();
  const setParams = useSetParams();
  const share = useQuery({ queryKey: keys.share(token), queryFn: () => fetchers.share(token), retry: false });
  const artifact = share.data?.artifact;

  useEffect(() => {
    document.title = artifact ? artifact.filename : "Shared file";
  }, [artifact]);

  if (share.isPending) {
    return (
      <main className="share" id="main">
        <Loading what="file" />
      </main>
    );
  }
  if (share.isError || !artifact) {
    return (
      <main className="share" id="main">
        <p className="share-state">{linkStateMessage(share.error)}</p>
      </main>
    );
  }

  const base = `/s/${encodeURIComponent(token)}`;
  const rawUrl = `${base}/raw`;
  const downloadUrl = `${rawUrl}?download=1`;
  const expires = share.data?.expiresAt;
  const shareArtifact: ArtifactOut = {
    ...artifact,
    id: token,
    sessionUrl: artifact.url,
    sharedSessionUrl: null,
    projectId: "",
    sessionId: null,
    sessionSlug: null,
    sessionName: null,
    leaseSid: null,
    sha256: "",
    pinned: false,
    source: "ui",
    createdBy: null,
    tags: [],
    retentionDays: null,
    effectiveRetentionDays: null,
    retentionSource: null,
    expiresAt: null,
    seen: false,
    thumbnailUrl: null,
    shareCount: 0,
    previousId: null,
    nextId: null,
  };

  return (
    <main className="share" id="main">
      <header className="share-head">
        <div>
          <h1 className="share-title" tabIndex={-1} data-page-title="">
            {artifact.filename}
          </h1>
          <p className="share-meta">
            <span aria-label={`Kind: ${kindLabel(artifact.kind)}`}>{kindLabel(artifact.kind)}</span>
            <span aria-label={`Size: ${formatSize(artifact.size)}`}>{formatSize(artifact.size)}</span>
            {artifact.width && artifact.height ? <span>{dimensions(artifact.width, artifact.height)}</span> : null}
            {artifact.durationMs ? <span>{formatDuration(artifact.durationMs)}</span> : null}
            <span title={formatFull(artifact.createdAt)}>Captured {formatFullMinutes(artifact.createdAt)}</span>
          </p>
        </div>
        <div className="share-actions">
          <ButtonLink to={localPath(artifact.directUrl)} newTab>
            Open file
          </ButtonLink>
          <ButtonLink to={downloadUrl} download icon="download">
            Download
          </ButtonLink>
        </div>
      </header>
      <div className="share-viewer" data-stage={viewerKind(artifact)}>
        <Viewer artifact={shareArtifact} rawUrl={rawUrl} downloadUrl={downloadUrl} siteBase={`${base}/`} params={params} setParams={(patch, options) => setParams(patch, options)} shareMode keyboard />
      </div>
      {artifact.caption ? <p className="share-caption prose">{artifact.caption}</p> : null}
      <p className="share-foot">
        Shared with eks-harness.{expires ? ` This link expires on ${formatFullMinutes(expires)}.` : ""}
      </p>
    </main>
  );
}
