export type IsoDate = string;

export type LeaseKind = "browser" | "ios" | "android";
export type DeviceKind = "ios" | "android";
export type LeaseState = "queued" | "active" | "idle" | "released" | "broken";
export type LeasePhase = "preparing" | "ready" | "failed";
export type OwnerKind = "agent" | "manual";
export type Role = "admin" | "member";
export type GrantLevel = "viewer" | "editor";
export type AccessLevel = "viewer" | "editor" | "admin";
export type ArtifactSource = "agent" | "cli" | "ui" | "mcp";
export type PoolAction = "start" | "shutdown" | "reset" | "delete";
export type AuthVia = "local" | "key" | "cookie";
export type SettingSource = "default" | "file" | "env";
export type TimelineEntryType = "event" | "note" | "artifact";

export const ARTIFACT_KINDS = ["screenshot", "video", "dom", "mhtml", "a11y", "har", "console", "log", "site", "file"] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export interface ErrorResponse {
  error: string;
  message: string;
  [extra: string]: unknown;
}

export interface ValidationProblem {
  field: string;
  message: string;
  type: string;
}

export interface OkResponse {
  ok: boolean;
}

export interface HealthResponse {
  status: "ok";
  version: string;
}

export interface VersionResponse {
  version: string;
  sourceHash: string | null;
  builtAt: string | null;
  editable: boolean;
  python: string;
  platform: string;
}

export interface StorageUsage {
  usedBytes: number;
  artifactBytes: number;
  artifactCount: number;
  quotaBytes: number | null;
  overQuota: boolean;
  freeDiskBytes: number | null;
}

export interface DaemonActionResponse {
  ok: boolean;
  action: "restart" | "stop";
  message: string;
}

export interface EventOut {
  id: number;
  ts: IsoDate;
  type: string;
  resource: string | null;
  leaseSid: string | null;
  sessionId: number | null;
  projectId: string | null;
  actor: string | null;
  detail: Record<string, unknown>;
}

export interface EventList {
  items: EventOut[];
}

export interface SettingOut {
  key: string;
  value: unknown;
  default: unknown;
  description: string;
  type: string;
  restartRequired: boolean;
  group: string;
  groupTitle: string;
  nullable: boolean;
  minimum: number | null;
  maximum: number | null;
  choices: string[] | null;
  source: SettingSource;
  envName: string;
  pendingRestart: boolean;
}

export interface SettingsResponse {
  settings: SettingOut[];
  configFile: string;
  restartPending: boolean;
  restartPendingKeys: string[];
  errors: Record<string, string>;
}

export interface SettingsPatch {
  values: Record<string, unknown>;
  unset: string[];
}

export interface SettingChange {
  key: string;
  old: unknown;
  new: unknown;
  restartRequired: boolean;
}

export interface SettingsPatchResponse {
  changed: SettingChange[];
  restartRequired: boolean;
  settings: SettingsResponse | null;
}

export interface UserOut {
  id: number;
  username: string;
  role: Role;
  disabled: boolean;
  builtin: boolean;
  hasPassword: boolean;
  createdAt: IsoDate;
  updatedAt: IsoDate | null;
}

export interface UserList {
  items: UserOut[];
}

export interface UserCreate {
  username: string;
  password?: string | null;
  role: Role;
}

export interface UserUpdate {
  username?: string | null;
  password?: string | null;
  role?: Role | null;
  disabled?: boolean | null;
}

export interface GrantOut {
  id: number;
  userId: number;
  username: string | null;
  projectId: string;
  sessionId: number | null;
  sessionSlug: string | null;
  sessionName: string | null;
  level: GrantLevel;
  createdAt: IsoDate;
}

export interface GrantList {
  items: GrantOut[];
}

export interface GrantCreate {
  userId?: number | null;
  username?: string | null;
  project: string;
  session?: string | null;
  level: GrantLevel;
}

export interface LoginRequest {
  username?: string | null;
  password?: string | null;
  apiKey?: string | null;
}

export interface LoginResponse {
  user: UserOut;
  csrfToken: string;
  expiresAt: IsoDate;
}

export interface MeResponse {
  user: UserOut;
  via: AuthVia;
  authEnabled: boolean;
  csrfToken: string | null;
  keyPrefix: string | null;
  grants: GrantOut[];
}

export interface ApiKeyOut {
  id: number;
  userId: number;
  username: string | null;
  name: string;
  prefix: string;
  createdAt: IsoDate;
  lastUsedAt: IsoDate | null;
  revokedAt: IsoDate | null;
  active: boolean;
}

export interface ApiKeyList {
  items: ApiKeyOut[];
}

export interface ApiKeyCreate {
  name: string;
  userId?: number | null;
  username?: string | null;
}

export interface ApiKeyCreated extends ApiKeyOut {
  key: string;
}

export interface ProjectBrief {
  id: string;
  owner: string;
  name: string;
  title: string;
  url: string;
}

export interface SessionBrief {
  id: number;
  projectId: string | null;
  name: string;
  slug: string;
  url: string;
  sharedUrl: string;
}

export interface LeaseBrief {
  id: string;
  sid: string;
  kind: LeaseKind;
  resource: string | null;
  state: LeaseState;
  phase: LeasePhase | null;
  ownerInstance: string;
  ownerKind: OwnerKind;
  ownerUser: number | null;
  ownerUsername: string | null;
  acquiredAt: IsoDate | null;
  heartbeatAt: IsoDate | null;
  idleSeconds: number | null;
}

export interface ProjectOut {
  id: string;
  owner: string;
  name: string;
  title: string;
  description: string;
  implicit: boolean;
  retentionDays: number | null;
  defaultRetentionDays: number | null;
  effectiveRetentionDays: number | null;
  retentionSource: ProjectRetentionSource;
  createdAt: IsoDate;
  updatedAt: IsoDate;
  sessionCount: number;
  artifactCount: number;
  unseenCount: number;
  sizeBytes: number;
  lastActivityAt: IsoDate | null;
  projectFileCount: number;
  activeLeases: number;
  access: AccessLevel | null;
  url: string;
}

export type ProjectRetentionSource = "project" | "forever" | "global";

export type ArtifactRetentionSource = "pinned" | "artifact" | "project" | "global";

export interface ProjectList {
  items: ProjectOut[];
}

export interface ProjectCreate {
  id?: string | null;
  owner?: string | null;
  name?: string | null;
  title: string;
  description: string;
  retentionDays?: number | null;
}

export interface ProjectUpdate {
  title?: string | null;
  description?: string | null;
  retentionDays?: number | null;
}

export interface DeleteSummary {
  deleted: boolean;
  artifacts: number;
  sessions: number;
  notes: number;
  shares: number;
  bytes: number;
}

export interface SessionProjectOut {
  project: ProjectBrief;
  artifactCount: number;
  unseenCount: number;
  sizeBytes: number;
  activeLeases: number;
  lastActiveAt: IsoDate | null;
  url: string;
}

export interface SessionOut {
  id: number;
  projectId: string | null;
  projectIds: string[];
  projects: SessionProjectOut[];
  name: string;
  slug: string;
  createdAt: IsoDate;
  lastActiveAt: IsoDate;
  artifactCount: number;
  unseenCount: number;
  sizeBytes: number;
  noteCount: number;
  activeLeases: LeaseBrief[];
  access: AccessLevel | null;
  url: string;
  sharedUrl: string;
}

export interface SessionList {
  items: SessionOut[];
}

export interface SessionUpdate {
  name?: string | null;
  slug?: string | null;
}

export interface NoteCreate {
  body: string;
  sid?: string | null;
}

export interface NoteOut {
  id: number;
  sessionId: number;
  leaseSid: string | null;
  author: string;
  body: string;
  createdAt: IsoDate;
}

export interface ArtifactLinks {
  id: string;
  url: string;
  rawUrl: string;
  sessionUrl: string;
  sharedSessionUrl: string | null;
  downloadUrl: string;
  kind: string;
  size: number;
  caption: string;
}

export interface ArtifactOut extends ArtifactLinks {
  projectId: string;
  sessionId: number | null;
  sessionSlug: string | null;
  sessionName: string | null;
  leaseSid: string | null;
  mime: string;
  filename: string;
  sha256: string;
  width: number | null;
  height: number | null;
  durationMs: number | null;
  pinned: boolean;
  source: ArtifactSource;
  createdBy: string | null;
  createdAt: IsoDate;
  meta: Record<string, unknown>;
  tags: string[];
  retentionDays: number | null;
  effectiveRetentionDays: number | null;
  retentionSource: ArtifactRetentionSource | null;
  expiresAt: IsoDate | null;
  seen: boolean;
  thumbnailUrl: string | null;
  siteUrl: string | null;
  shareCount: number;
  previousId: string | null;
  nextId: string | null;
}

export interface ArtifactFacets {
  kind: Record<string, number>;
  tag: Record<string, number>;
}

export interface ArtifactList {
  items: ArtifactOut[];
  nextCursor: string | null;
  prevCursor: string | null;
  total: number | null;
  facets: ArtifactFacets | null;
}

export interface ArtifactUpdate {
  caption?: string | null;
  pinned?: boolean | null;
  tags?: string[] | null;
  addTags?: string[] | null;
  removeTags?: string[] | null;
  meta?: Record<string, unknown> | null;
  retentionDays?: number | null;
}

export interface SeenBulkRequest {
  ids: string[];
  seen: boolean;
}

export interface SeenResponse {
  updated: number;
  seen: boolean;
}

export interface SearchHitOut {
  artifact: ArtifactOut;
  snippet: string;
  rank: number;
}

export interface SearchResponse {
  query: string;
  items: SearchHitOut[];
}

export interface TagCount {
  tag: string;
  count: number;
  color?: string | null;
  builtin?: boolean;
  label?: string | null;
}

export interface TagInfo {
  tag: string;
  label: string;
  color: string | null;
  defaultColor: string | null;
  builtin: boolean;
  description: string;
  count: number;
}

export interface TagCatalog {
  items: TagInfo[];
}

export interface TimelineEntry {
  ts: IsoDate;
  type: TimelineEntryType;
  event: EventOut | null;
  note: NoteOut | null;
  artifact: ArtifactOut | null;
}

export interface TimelineResponse {
  session: SessionOut;
  items: TimelineEntry[];
}

export interface ShareCreate {
  expires?: string | null;
  expiresAt?: IsoDate | null;
}

export interface ShareOut {
  token: string;
  artifactId: string;
  url: string;
  rawUrl: string;
  directUrl: string | null;
  createdBy: string | null;
  createdAt: IsoDate;
  expiresAt: IsoDate | null;
  revokedAt: IsoDate | null;
  views: number;
  lastViewedAt: IsoDate | null;
  active: boolean;
}

export interface ShareList {
  items: ShareOut[];
}

export interface SharedArtifact {
  kind: string;
  mime: string;
  filename: string;
  size: number;
  width: number | null;
  height: number | null;
  durationMs: number | null;
  caption: string;
  createdAt: IsoDate;
  url: string;
  rawUrl: string;
  directUrl: string;
  downloadUrl: string;
  siteUrl: string | null;
  meta: Record<string, unknown>;
}

export interface SharedArtifactOut {
  token: string;
  artifact: SharedArtifact;
  expiresAt: IsoDate | null;
}

export interface AnnotationRuleResult {
  rule: string;
  ok: boolean;
  message?: string | null;
  [extra: string]: unknown;
}

export interface AnnotationValidation {
  ok: boolean;
  rules: AnnotationRuleResult[];
  anchorFallback: boolean;
  [extra: string]: unknown;
}

export interface AnnotationCrop {
  itemId: string;
  url: string;
  rawUrl: string;
}

export interface AnnotateResponse {
  artifact: ArtifactOut;
  version: number;
  kind: string;
  validation: AnnotationValidation;
  crops: AnnotationCrop[];
}

export interface AnnotationVersionOut {
  version: number;
  kind: string;
  createdAt: IsoDate;
  createdBy: string | null;
  styleName: string | null;
  url: string;
}

export interface AnnotationVersionsResponse {
  versions: AnnotationVersionOut[];
}

export interface AnnotationMeta {
  cleanId?: string | null;
  spec?: unknown;
  normalizedSpec?: unknown;
  styleName?: string | null;
  styleSnapshot?: Record<string, unknown> | null;
  boxes?: unknown;
  anchorFallback?: boolean;
  report?: unknown;
  crops?: unknown;
  [extra: string]: unknown;
}

export interface ProjectAssetOut {
  name: string;
  filename: string;
  mime?: string | null;
  size?: number | null;
  url?: string | null;
  [extra: string]: unknown;
}

export interface ProjectAssetList {
  items: ProjectAssetOut[];
}

export interface LeaseUrls {
  session: string;
  ui: string;
  project: string | null;
}

export interface DeviceBrief {
  key: string;
  kind: DeviceKind;
  index: number;
  name: string;
  udid: string | null;
  serial: string | null;
  port: number | null;
  status: string | null;
}

export interface LeaseOut extends LeaseBrief {
  sessionId: number | null;
  projectId: string | null;
  sessionSlug: string | null;
  sessionName: string | null;
  backendId: string | null;
  reason: string | null;
  error: string | null;
  queuedAt: IsoDate | null;
  releasedAt: IsoDate | null;
  idleLimit: number | null;
  label: string | null;
  tree: string | null;
  stateDir: string | null;
  previousSid: string | null;
  stale: string | null;
  queuePosition: number | null;
  cdp: string | null;
  browser: number | null;
  profile: number | null;
  device: DeviceBrief | null;
  urls: LeaseUrls | null;
  meta: Record<string, unknown>;
}

export interface LeaseList {
  items: LeaseOut[];
  queue: LeaseOut[];
}

export interface SidInfo {
  sid: string;
  valid: boolean;
  lease: LeaseOut;
  project: ProjectBrief | null;
  session: SessionBrief | null;
  urls: LeaseUrls | null;
  reacquire: string | null;
}

export interface DeviceOut {
  key: string;
  kind: DeviceKind;
  index: number;
  name: string;
  status: string;
  statusText: string;
  statusSince: IsoDate | null;
  udid: string | null;
  serial: string | null;
  port: number | null;
  retired: boolean;
  lastUsedAt: IsoDate | null;
  idleSeconds: number | null;
  lease: LeaseBrief | null;
  session: SessionBrief | null;
  queueLength: number;
  liveUrl: string | null;
  screenWidth: number | null;
  screenHeight: number | null;
  liveViewers: number;
}

export interface DeviceList {
  items: DeviceOut[];
  maxRunning: number;
  running: number;
}

export interface DeviceDetail extends DeviceOut {
  queue: LeaseOut[];
  activity: EventOut[];
  history: LeaseOut[];
  logResource: string;
}

export interface ProfileOut {
  id: string;
  browser: number;
  profile: number;
  status: string;
  statusText: string;
  statusSince: IsoDate | null;
  pageUrl: string | null;
  lease: LeaseBrief | null;
  session: SessionBrief | null;
  queueLength: number;
  liveUrl: string | null;
  screenWidth: number | null;
  screenHeight: number | null;
  liveViewers: number;
}

export interface ProfileList {
  items: ProfileOut[];
}

export interface ProfileDetail extends ProfileOut {
  queue: LeaseOut[];
  activity: EventOut[];
  history: LeaseOut[];
  cdp: string | null;
}

export interface BrowserProcessOut {
  index: number;
  status: string;
  statusText: string;
  alive: boolean;
  pid: number | null;
  port: number | null;
  cdp: string | null;
  binary: string | null;
  launchedAt: IsoDate | null;
  lastUsedAt: IsoDate | null;
  profiles: ProfileOut[];
  logResource: string;
}

export interface BrowserList {
  items: BrowserProcessOut[];
  capacity: number;
  command: string;
}

export interface PoolActionRequest {
  confirm?: string | null;
  reason?: string | null;
  force?: boolean;
}

export interface PoolActionResponse {
  resource: string;
  action: PoolAction;
  ok: boolean;
  brokenLeases: string[];
  lease: LeaseOut | null;
  message: string;
}

export interface BackendOut {
  id: string;
  definition: string;
  instance: string;
  status: string;
  tree: string | null;
  description: string | null;
  ports: Record<string, number>;
  captured: Record<string, string>;
  exports: Record<string, string>;
  processes: string[];
  logs: Record<string, string>;
  alive: Record<string, boolean>;
  bindings: string[];
  error: string | null;
  fingerprint: string | null;
  idleGraceSeconds: number | null;
  startedAt: IsoDate | null;
  stoppedAt: IsoDate | null;
  logFile: string | null;
  [extra: string]: unknown;
}

export interface BackendList {
  items: BackendOut[];
}

export interface BackendDefinitionOut {
  name: string;
  path: string;
  source: "tree" | "config";
  tree: string | null;
}

export interface BackendDefinitionList {
  items: BackendDefinitionOut[];
}

export interface LogResponse {
  resource: string;
  path: string;
  offset: number;
  text: string;
}

export interface StatusResponse {
  pid: number;
  managed: boolean;
  fakePools: boolean;
  version: string;
  sourceHash: string | null;
  url: string;
  publicUrl: string;
  startedAt: IsoDate;
  uptimeSeconds: number;
  authEnabled: boolean;
  configFile: string;
  restartPending: boolean;
  leases: LeaseOut[];
  queue: LeaseOut[];
  devices: DeviceOut[];
  browsers: BrowserProcessOut[];
  backends: BackendOut[];
  storage: StorageUsage | null;
}
