import type { QueryClient } from "@tanstack/react-query";
import { api, ApiError, authHeaders, buildQuery, encodeSegment, type Query } from "../api/client";
import { fetchers, invalidateArtifactViews, keys, sessionPath, type ArtifactParams } from "../api/queries";
import type { ArtifactList, ArtifactOut, DeleteSummary, ErrorResponse, SeenResponse, TagInfo } from "../api/types";
import { saveBlob } from "../lib/download";

const CHUNK = 5000;

export async function setSeen(client: QueryClient, ids: string[], seen: boolean, projectId?: string | null): Promise<number> {
  if (!ids.length) return 0;
  let updated = 0;
  if (ids.length === 1) {
    const path = `/api/artifacts/${encodeSegment(ids[0])}/seen`;
    if (seen) await api.put(path);
    else await api.delete(path);
    updated = 1;
  } else {
    for (let i = 0; i < ids.length; i += CHUNK) {
      const chunk = ids.slice(i, i + CHUNK);
      const response = await api.post<SeenResponse>("/api/artifacts/seen", { ids: chunk, seen });
      updated += response?.updated ?? chunk.length;
    }
  }
  for (const id of ids) {
    client.setQueryData<ArtifactOut>(keys.artifact(id), (old) => (old ? { ...old, seen } : old));
  }
  invalidateArtifactViews(client, projectId);
  return updated;
}

export async function setSessionSeen(client: QueryClient, projectId: string | null, slug: string, seen: boolean): Promise<number> {
  const response = await api.post<SeenResponse>(`${sessionPath(projectId, slug)}/seen`, undefined, { query: { seen: seen ? "true" : "false" } });
  void client.invalidateQueries({ queryKey: ["artifact"] });
  invalidateArtifactViews(client, projectId);
  return response?.updated ?? 0;
}

export async function patchArtifact(client: QueryClient, id: string, patch: Record<string, unknown>): Promise<ArtifactOut> {
  const updated = await api.patch<ArtifactOut>(`/api/artifacts/${encodeSegment(id)}`, patch);
  if (updated && typeof updated === "object") client.setQueryData(keys.artifact(id), updated);
  return updated;
}

export async function forEachLimited<T>(items: T[], limit: number, fn: (item: T) => Promise<unknown>): Promise<{ failed: T[]; error: unknown }> {
  const failed: T[] = [];
  let error: unknown = null;
  let index = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (index < items.length) {
      const item = items[index++];
      try {
        await fn(item);
      } catch (err) {
        failed.push(item);
        error = err;
      }
    }
  });
  await Promise.all(workers);
  return { failed, error };
}

export async function bulkPatch(client: QueryClient, ids: string[], patch: Record<string, unknown>, projectId?: string | null): Promise<void> {
  const { failed, error } = await forEachLimited(ids, 6, (id) => patchArtifact(client, id, patch));
  invalidateArtifactViews(client, projectId);
  if (failed.length) throw error;
}

export async function bulkTag(client: QueryClient, ids: string[], add: string[], remove: string[], projectId?: string | null): Promise<void> {
  for (let i = 0; i < ids.length; i += CHUNK) {
    const result = await api.post<ArtifactList>("/api/artifacts/tags", { ids: ids.slice(i, i + CHUNK), add, remove });
    for (const item of result?.items ?? []) client.setQueryData(keys.artifact(item.id), item);
  }
  invalidateArtifactViews(client, projectId);
}

export async function bulkRetention(client: QueryClient, ids: string[], retentionDays: number | null, projectId?: string | null): Promise<void> {
  for (let i = 0; i < ids.length; i += CHUNK) {
    await api.post<ArtifactList>("/api/artifacts/retention", { ids: ids.slice(i, i + CHUNK), retentionDays });
  }
  for (const id of ids) void client.invalidateQueries({ queryKey: keys.artifact(id) });
  invalidateArtifactViews(client, projectId);
}

export async function setTagColor(client: QueryClient, tag: string, color: string | null): Promise<TagInfo> {
  const info = await api.put<TagInfo>(`/api/tags/${encodeSegment(tag)}/color`, { color });
  await client.invalidateQueries({ queryKey: keys.tagCatalog });
  void client.invalidateQueries({ queryKey: ["tags"] });
  return info;
}

export async function deleteSummary(ids: string[]): Promise<DeleteSummary> {
  const total: DeleteSummary = { deleted: false, artifacts: 0, sessions: 0, notes: 0, shares: 0, bytes: 0 };
  for (let i = 0; i < ids.length; i += CHUNK) {
    const part = await api.post<DeleteSummary>("/api/artifacts/delete", { ids: ids.slice(i, i + CHUNK) }, { query: { dryRun: "true" } });
    total.artifacts += part.artifacts;
    total.shares += part.shares;
    total.notes += part.notes;
    total.bytes += part.bytes;
  }
  return total;
}

export async function deleteArtifacts(client: QueryClient, ids: string[], projectId?: string | null): Promise<DeleteSummary> {
  const total: DeleteSummary = { deleted: true, artifacts: 0, sessions: 0, notes: 0, shares: 0, bytes: 0 };
  if (ids.length === 1) {
    const one = await api.delete<DeleteSummary>(`/api/artifacts/${encodeSegment(ids[0])}`);
    Object.assign(total, one ?? {});
  } else {
    for (let i = 0; i < ids.length; i += CHUNK) {
      const part = await api.post<DeleteSummary>("/api/artifacts/delete", { ids: ids.slice(i, i + CHUNK) });
      total.artifacts += part.artifacts;
      total.shares += part.shares;
      total.bytes += part.bytes;
    }
  }
  for (const id of ids) client.removeQueries({ queryKey: keys.artifact(id) });
  void client.invalidateQueries({ queryKey: ["artifact-nav"] });
  invalidateArtifactViews(client, projectId);
  return total;
}

export async function collectAll(params: ArtifactParams): Promise<ArtifactOut[]> {
  const out: ArtifactOut[] = [];
  let cursor: string | undefined;
  for (let guard = 0; guard < 1000; guard += 1) {
    const page = await fetchers.artifacts({ ...params, cursor, limit: 500 });
    out.push(...page.items);
    if (!page.nextCursor || !page.items.length) break;
    cursor = page.nextCursor;
  }
  return out;
}

export async function resolveArtifacts(ids: string[], known: Map<string, ArtifactOut>): Promise<ArtifactOut[]> {
  const out: ArtifactOut[] = [];
  const missing = ids.filter((id) => !known.has(id));
  const fetched = new Map<string, ArtifactOut>();
  await forEachLimited(missing, 6, async (id) => {
    fetched.set(id, await fetchers.artifact(id));
  });
  for (const id of ids) {
    const item = known.get(id) ?? fetched.get(id);
    if (item) out.push(item);
  }
  return out;
}

function filenameFrom(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback;
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) {
    try {
      return decodeURIComponent(star[1]);
    } catch {
      return fallback;
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

export async function downloadZip(target: { ids: string[] } | { project?: string; session?: string }, fallbackName: string): Promise<void> {
  let response: Response;
  if ("ids" in target && target.ids.length > 150) {
    response = await fetch("/api/artifacts/zip", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/zip", ...authHeaders("POST") },
      body: JSON.stringify({ ids: target.ids }),
    });
  } else {
    const query: Query = "ids" in target ? { ids: target.ids } : { project: target.project, session: target.session };
    response = await fetch(`/api/artifacts/zip${buildQuery(query)}`, { credentials: "same-origin", headers: { Accept: "application/zip" } });
  }
  if (!response.ok) {
    let body: ErrorResponse | null = null;
    try {
      body = (await response.json()) as ErrorResponse;
    } catch {
      body = null;
    }
    throw new ApiError(response.status, body?.error ?? `http_${response.status}`, body?.message ?? `HTTP ${response.status}`, body);
  }
  const blob = await response.blob();
  saveBlob(blob, filenameFrom(response.headers.get("content-disposition"), `${fallbackName}.zip`));
}
