import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { ApiError, errorText } from "../api/client";
import { fetchers, keys } from "../api/queries";
import { Mono } from "../components/Misc";
import { EmptyState, ErrorState, Loading } from "../components/Notice";
import { useTitle } from "../lib/hooks";
import { hrefWith, localPath, sessionUrl, usePathname } from "../lib/url";

export function SidRedirectPage({ sid }: { sid: string }) {
  const navigate = useNavigate();
  useTitle(`Lease ${sid}`);
  const info = useQuery({ queryKey: keys.sid(sid), queryFn: () => fetchers.sid(sid), retry: false });
  useEffect(() => {
    const data = info.data;
    if (!data) return;
    const projectId = data.session?.projectId ?? data.lease.projectId;
    const slug = data.session?.slug ?? data.lease.sessionSlug;
    const target = projectId && slug ? sessionUrl(projectId, slug) : localPath(data.urls?.session);
    if (target) void navigate({ to: hrefWith(target, { lease: sid }), replace: true });
  }, [info.data, navigate, sid]);
  if (info.isError) {
    if (info.error instanceof ApiError && info.error.status === 404) {
      return (
        <div className="page">
          <EmptyState action={<Link to="/projects" className="link">Projects</Link>}>
            There is no lease <Mono>{sid}</Mono>.
          </EmptyState>
        </div>
      );
    }
    return (
      <div className="page">
        <ErrorState message={errorText(info.error, "look up", `the lease ${sid}`)} onRetry={() => void info.refetch()} />
      </div>
    );
  }
  if (info.data && !(info.data.session || info.data.lease.projectId)) {
    return (
      <div className="page">
        <EmptyState>
          The lease <Mono>{sid}</Mono> has no session.
        </EmptyState>
      </div>
    );
  }
  return (
    <div className="page">
      <Loading what="lease" />
    </div>
  );
}

export function NotFoundPage() {
  const pathname = usePathname();
  useTitle("Not found");
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title" tabIndex={-1} data-page-title="">
          Not found
        </h1>
      </div>
      <EmptyState action={<Link to="/projects" className="link">Projects</Link>}>There is no page at {pathname}. It may have moved.</EmptyState>
    </div>
  );
}
