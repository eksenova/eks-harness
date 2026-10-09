import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { errorText, fetchText } from "../api/client";
import { ButtonLink } from "../components/Button";
import { ErrorState, Loading, Notice } from "../components/Notice";
import { formatSize } from "../lib/format";
import { hrefWith, localPath, usePathname } from "../lib/url";
import { TextContent } from "./TextContent";
import type { ViewerProps } from "./types";

export const TEXT_LIMIT = 5 * 1000 * 1000;

export function useRawText(url: string) {
  return useQuery({
    queryKey: ["raw-text", url],
    queryFn: ({ signal }) => fetchText(url, TEXT_LIMIT, signal),
    staleTime: Infinity,
    retry: 1,
  });
}

export function TextViewer({ artifact, rawUrl, downloadUrl, params, setParams }: ViewerProps) {
  const query = useRawText(rawUrl);
  const pathname = usePathname();
  const defaultWrap = artifact.kind === "a11y";
  const wrap = params.wrap === "1" ? true : params.wrap === "0" ? false : defaultWrap;
  const onFind = useCallback((value: string) => setParams({ find: value || null }, { replace: true }), [setParams]);
  if (query.isPending) return <Loading what="log" />;
  if (query.isError) return <ErrorState message={errorText(query.error, "load", "the file")} onRetry={() => void query.refetch()} />;
  const { text, total, truncated } = query.data;
  const noun = artifact.kind === "log" || artifact.kind === "console" ? "log" : "file";
  return (
    <div className="viewer">
      {truncated ? (
        <Notice variant="info" title={`This ${noun} is ${formatSize(total)}.`} action={<ButtonLink to={downloadUrl ?? localPath(artifact.downloadUrl)} download>Download full file</ButtonLink>}>
          Showing the last {formatSize(TEXT_LIMIT)}.
        </Notice>
      ) : null}
      <TextContent
        text={text}
        find={params.find ?? ""}
        onFind={onFind}
        lineSpec={params.line}
        onLine={(spec) => setParams({ line: spec }, { replace: true })}
        lineHref={(spec) => hrefWith(pathname, { ...params, line: spec })}
        wrap={wrap}
        onWrap={(value) => setParams({ wrap: value === defaultWrap ? null : value ? "1" : "0" }, { replace: true })}
        label={`Contents of ${artifact.filename}`}
      />
    </div>
  );
}
