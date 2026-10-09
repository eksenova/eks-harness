import { useCallback, useMemo } from "react";
import { errorText } from "../api/client";
import { ErrorState, Loading, Notice } from "../components/Notice";
import { JsonTree } from "./JsonTree";
import { useRawText } from "./TextViewer";
import { TextContent } from "./TextContent";
import type { ViewerProps } from "./types";

export function JsonViewer({ rawUrl, params, setParams, artifact }: ViewerProps) {
  const query = useRawText(rawUrl);
  const onFind = useCallback((value: string) => setParams({ find: value || null }, { replace: true }), [setParams]);
  const parsed = useMemo(() => {
    if (!query.data) return null;
    if (query.data.truncated) return { ok: false as const, reason: "The file is too large to show as a tree." };
    try {
      return { ok: true as const, value: JSON.parse(query.data.text) as unknown };
    } catch (err) {
      return { ok: false as const, reason: `The file is not valid JSON (${err instanceof Error ? err.message : "parse error"}).` };
    }
  }, [query.data]);
  if (query.isPending) return <Loading what="file" />;
  if (query.isError) return <ErrorState message={errorText(query.error, "load", "the file")} onRetry={() => void query.refetch()} />;
  if (!parsed || !parsed.ok) {
    return (
      <div className="viewer">
        <Notice variant="info">{parsed?.reason ?? ""} Showing it as text.</Notice>
        <TextContent
          text={query.data.text}
          find={params.find ?? ""}
          onFind={onFind}
          wrap={params.wrap === "1"}
          onWrap={(value) => setParams({ wrap: value ? "1" : null }, { replace: true })}
          label={`Contents of ${artifact.filename}`}
        />
      </div>
    );
  }
  return (
    <div className="viewer">
      <JsonTree data={parsed.value} find={params.find ?? ""} onFind={onFind} />
    </div>
  );
}
