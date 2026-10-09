import { keepPreviousData, useQuery, type QueryClient } from "@tanstack/react-query";
import { api, encodeSegment, projectPath, type Query } from "./client";
import type {
  AnnotationVersionsResponse,
  ApiKeyList,
  ArtifactList,
  ArtifactOut,
  BackendDefinitionList,
  BackendList,
  BackendOut,
  BrowserList,
  DeviceDetail,
  DeviceList,
  GrantList,
  LeaseList,
  LogResponse,
  MeResponse,
  ProfileDetail,
  ProfileList,
  ProjectAssetList,
  ProjectAssetOut,
  ProjectList,
  ProjectOut,
  SearchResponse,
  SessionList,
  SessionOut,
  SettingsResponse,
  ShareList,
  SharedArtifactOut,
  SidInfo,
  StatusResponse,
  TagCatalog,
  TagCount,
  TimelineResponse,
  UserList,
  VersionResponse,
} from "./types";

const SHARED = "*";

export function sessionPath(projectId: string | null, slug: string): string {
  return projectId ? `${projectPath(projectId)}/sessions/${encodeSegment(slug)}` : `/api/sessions/${encodeSegment(slug)}`;
}

export const keys = {
  me: ["me"] as const,
  status: ["status"] as const,
  version: ["version"] as const,
  projects: ["projects"] as const,
  project: (id: string) => ["project", id] as const,
  sessions: (projectId: string | null) => ["sessions", projectId ?? SHARED] as const,
  session: (projectId: string | null, slug: string) => ["session", projectId ?? SHARED, slug] as const,
  artifacts: (params: ArtifactParams) => ["artifacts", params] as const,
  artifactsAll: ["artifacts"] as const,
  artifact: (id: string) => ["artifact", id] as const,
  shares: (artifactId: string) => ["shares", artifactId] as const,
  timeline: (projectId: string | null, slug: string) => ["timeline", projectId ?? SHARED, slug] as const,
  tags: (projectId: string | null, slug: string | null) => ["tags", projectId ?? SHARED, slug] as const,
  tagCatalog: ["tags", "catalog"] as const,
  devices: ["devices"] as const,
  device: (kind: string, index: string) => ["device", kind, index] as const,
  browsers: ["browsers"] as const,
  profiles: ["profiles"] as const,
  profile: (id: string) => ["profile", id] as const,
  backends: ["backends"] as const,
  backend: (id: string) => ["backend", id] as const,
  backendDefinitions: ["backendDefinitions"] as const,
  settings: ["settings"] as const,
  keys: (user: string) => ["keys", user] as const,
  users: ["users"] as const,
  grants: (params: Query) => ["grants", params] as const,
  search: (params: Query) => ["search", params] as const,
  leases: ["leases"] as const,
  share: (token: string) => ["share", token] as const,
  sid: (sid: string) => ["sid", sid] as const,
  annotations: (id: string) => ["annotations", id] as const,
  assets: (projectId: string) => ["assets", projectId] as const,
};

export interface ArtifactParams {
  project?: string;
  session?: string;
  projectLevel?: boolean;
  kind?: string;
  tag?: string;
  q?: string;
  unseen?: boolean;
  sort?: string;
  dir?: string;
  cursor?: string;
  limit?: number;
  facets?: boolean;
}

export const PROJECT_LEVEL_SESSION = "_project";

export function artifactQuery(params: ArtifactParams): Query {
  return {
    project: params.project,
    session: params.projectLevel ? PROJECT_LEVEL_SESSION : params.session,
    kind: params.kind,
    tag: params.tag,
    q: params.q,
    unseen: params.unseen ? "1" : undefined,
    sort: params.sort,
    dir: params.dir,
    cursor: params.cursor,
    limit: params.limit ?? 100,
    facets: params.facets ? "true" : undefined,
  };
}

export const fetchers = {
  me: () => api.get<MeResponse>("/api/auth/me", { handleUnauthorized: false }),
  status: () => api.get<StatusResponse>("/api/status"),
  version: () => api.get<VersionResponse>("/api/version"),
  projects: () => api.get<ProjectList>("/api/projects"),
  project: (id: string) => api.get<ProjectOut>(projectPath(id)),
  sessions: (projectId: string | null) => api.get<SessionList>(projectId ? `${projectPath(projectId)}/sessions` : "/api/sessions"),
  session: (projectId: string | null, slug: string) => api.get<SessionOut>(sessionPath(projectId, slug)),
  artifacts: (params: ArtifactParams) => api.get<ArtifactList>("/api/artifacts", { query: artifactQuery(params) }),
  artifact: (id: string) => api.get<ArtifactOut>(`/api/artifacts/${encodeSegment(id)}`),
  shares: (artifactId: string) => api.get<ShareList>(`/api/artifacts/${encodeSegment(artifactId)}/shares`),
  timeline: (projectId: string | null, slug: string) => api.get<TimelineResponse>(`${sessionPath(projectId, slug)}/timeline`),
  tags: (projectId: string | null, slug: string | null) =>
    api.get<TagCount[] | { items: TagCount[] }>("/api/tags", { query: { project: projectId ?? undefined, session: slug ?? undefined } }),
  tagCatalog: () => api.get<TagCatalog>("/api/tags/catalog"),
  devices: () => api.get<DeviceList>("/api/devices"),
  device: (kind: string, index: string) => api.get<DeviceDetail>(`/api/devices/${encodeSegment(kind)}/${encodeSegment(index)}`),
  browsers: () => api.get<BrowserList>("/api/browsers"),
  profiles: () => api.get<ProfileList>("/api/profiles"),
  profile: (id: string) => api.get<ProfileDetail>(`/api/profiles/${encodeSegment(id)}`),
  backends: () => api.get<BackendList>("/api/backends"),
  backend: (id: string) => api.get<BackendOut>(`/api/backends/${backendPath(id)}`),
  backendDefinitions: () => api.get<BackendDefinitionList>("/api/backends/definitions"),
  settings: () => api.get<SettingsResponse>("/api/settings"),
  keys: (user: string) => api.get<ApiKeyList>("/api/keys", { query: user === "*" ? { all: "true" } : { user: user || undefined } }),
  users: () => api.get<UserList>("/api/users"),
  grants: (params: Query) => api.get<GrantList>("/api/grants", { query: params }),
  search: (params: Query) => api.get<SearchResponse>("/api/search", { query: { q: params.q, project: params.project, limit: 500 } }),
  leases: () => api.get<LeaseList>("/api/leases"),
  share: (token: string) => api.get<SharedArtifactOut>(`/api/shared/${encodeSegment(token)}`, { handleUnauthorized: false }),
  sid: (sid: string) => api.get<SidInfo>(`/api/sid/${encodeSegment(sid)}`),
  logs: (query: Query) => api.get<LogResponse>("/api/logs", { query, timeoutMs: 30_000 }),
  annotations: (id: string) => api.get<AnnotationVersionsResponse>(`/api/artifacts/${encodeSegment(id)}/annotations`),
  assets: (projectId: string) => api.get<ProjectAssetList | ProjectAssetOut[]>(`${projectPath(projectId)}/assets`),
};

export function backendPath(id: string): string {
  return id.split("/").map(encodeSegment).join("/");
}

export function normalizeTags(value: TagCount[] | { items: TagCount[] } | undefined): TagCount[] {
  if (!value) return [];
  return Array.isArray(value) ? value : value.items ?? [];
}

export function normalizeAssets(value: ProjectAssetList | ProjectAssetOut[] | undefined): ProjectAssetOut[] {
  if (!value) return [];
  return Array.isArray(value) ? value : value.items ?? [];
}

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: fetchers.me, retry: false, staleTime: 60_000 });
}

export function useStatus() {
  return useQuery({ queryKey: keys.status, queryFn: fetchers.status, staleTime: 15_000, retry: 1 });
}

export function useVersion() {
  return useQuery({ queryKey: keys.version, queryFn: fetchers.version, staleTime: 300_000 });
}

export function useProjects() {
  return useQuery({ queryKey: keys.projects, queryFn: fetchers.projects });
}

export function useProject(id: string, enabled = true) {
  return useQuery({ queryKey: keys.project(id), queryFn: () => fetchers.project(id), enabled });
}

export function useSessions(projectId: string, enabled = true) {
  return useQuery({ queryKey: keys.sessions(projectId), queryFn: () => fetchers.sessions(projectId), enabled: enabled && Boolean(projectId) && projectId.includes("/") });
}

export function useSharedSessions(enabled = true) {
  return useQuery({ queryKey: keys.sessions(null), queryFn: () => fetchers.sessions(null), enabled });
}

export function useSession(projectId: string | null, slug: string, enabled = true) {
  return useQuery({ queryKey: keys.session(projectId, slug), queryFn: () => fetchers.session(projectId, slug), enabled });
}

export function useArtifacts(params: ArtifactParams, enabled = true) {
  return useQuery({
    queryKey: keys.artifacts(params),
    queryFn: () => fetchers.artifacts(params),
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useArtifact(id: string) {
  return useQuery({ queryKey: keys.artifact(id), queryFn: () => fetchers.artifact(id) });
}

export function useShares(artifactId: string, enabled = true) {
  return useQuery({ queryKey: keys.shares(artifactId), queryFn: () => fetchers.shares(artifactId), enabled });
}

export function useAnnotationVersions(artifactId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.annotations(artifactId ?? ""),
    queryFn: () => fetchers.annotations(artifactId as string),
    enabled: enabled && Boolean(artifactId),
    retry: false,
  });
}

export function useProjectAssets(projectId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.assets(projectId ?? ""),
    queryFn: async () => normalizeAssets(await fetchers.assets(projectId as string)),
    enabled: enabled && Boolean(projectId),
    retry: false,
  });
}

export function useTimeline(projectId: string | null, slug: string) {
  return useQuery({ queryKey: keys.timeline(projectId, slug), queryFn: () => fetchers.timeline(projectId, slug) });
}

export function useTags(projectId: string | null, slug: string | null) {
  return useQuery({
    queryKey: keys.tags(projectId, slug),
    queryFn: async () => normalizeTags(await fetchers.tags(projectId, slug)),
    retry: false,
  });
}

export function useTagCatalog() {
  return useQuery({ queryKey: keys.tagCatalog, queryFn: fetchers.tagCatalog, staleTime: 60_000, retry: false });
}

export function useDevices() {
  return useQuery({ queryKey: keys.devices, queryFn: fetchers.devices });
}

export function useDevice(kind: string, index: string) {
  return useQuery({ queryKey: keys.device(kind, index), queryFn: () => fetchers.device(kind, index) });
}

export function useBrowsers() {
  return useQuery({ queryKey: keys.browsers, queryFn: fetchers.browsers });
}

export function useProfiles() {
  return useQuery({ queryKey: keys.profiles, queryFn: fetchers.profiles });
}

export function useProfile(id: string) {
  return useQuery({ queryKey: keys.profile(id), queryFn: () => fetchers.profile(id) });
}

export function useBackends() {
  return useQuery({ queryKey: keys.backends, queryFn: fetchers.backends });
}

export function useBackend(id: string) {
  return useQuery({ queryKey: keys.backend(id), queryFn: () => fetchers.backend(id) });
}

export function useBackendDefinitions() {
  return useQuery({ queryKey: keys.backendDefinitions, queryFn: fetchers.backendDefinitions });
}

export function useSettings(enabled = true) {
  return useQuery({ queryKey: keys.settings, queryFn: fetchers.settings, enabled });
}

export function useKeys(user: string) {
  return useQuery({ queryKey: keys.keys(user), queryFn: () => fetchers.keys(user) });
}

export function useUsers(enabled = true) {
  return useQuery({ queryKey: keys.users, queryFn: fetchers.users, enabled });
}

export function useGrants(params: Query, enabled = true) {
  return useQuery({ queryKey: keys.grants(params), queryFn: () => fetchers.grants(params), enabled });
}

export function useSearch(params: Query, enabled: boolean) {
  return useQuery({ queryKey: keys.search(params), queryFn: () => fetchers.search(params), enabled, placeholderData: keepPreviousData });
}

export function useLeases() {
  return useQuery({ queryKey: keys.leases, queryFn: fetchers.leases });
}

export function invalidateArtifactViews(client: QueryClient, projectId?: string | null): void {
  void client.invalidateQueries({ queryKey: keys.artifactsAll });
  void client.invalidateQueries({ queryKey: keys.projects });
  void client.invalidateQueries({ queryKey: ["search"] });
  void client.invalidateQueries({ queryKey: ["tags"] });
  void client.invalidateQueries({ queryKey: keys.sessions(null) });
  void client.invalidateQueries({ queryKey: ["session", SHARED] });
  void client.invalidateQueries({ queryKey: ["timeline", SHARED] });
  if (projectId) {
    void client.invalidateQueries({ queryKey: keys.project(projectId) });
    void client.invalidateQueries({ queryKey: keys.sessions(projectId) });
    void client.invalidateQueries({ queryKey: ["session", projectId] });
    void client.invalidateQueries({ queryKey: ["timeline", projectId] });
  }
}
