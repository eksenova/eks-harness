import { createRootRoute, createRoute, createRouter, Navigate, Outlet } from "@tanstack/react-router";
import { onUnauthorized } from "./api/client";
import { ArtifactPage } from "./pages/Artifact";
import { BackendDetailPage, BackendsPage } from "./pages/Backends";
import { LoginPage } from "./pages/Login";
import { NotFoundPage, SidRedirectPage } from "./pages/Misc";
import { BrowsersPage, DeviceDetailPage, DevicesPage, ProfileDetailPage } from "./pages/Pools";
import { ProjectPage, type ProjectTab } from "./pages/Project";
import { ProjectsPage } from "./pages/Projects";
import { NowPage } from "./pages/Now";
import { MachinesPage } from "./pages/Machines";
import { NodePage, NodesPage } from "./pages/Nodes";
import { PluginPage, PluginRoutePage, PluginsPage } from "./pages/Plugins";
import { ScorePage } from "./pages/Score";
import { StudioEditPage, StudioPage } from "./pages/Studio";
import { SearchPage } from "./pages/Search";
import { CleanupPage } from "./pages/Cleanup";
import { SessionPage } from "./pages/Session";
import { SessionsPage } from "./pages/Sessions";
import { SettingsPage } from "./pages/Settings";
import { SharePage } from "./pages/Share";
import { AppShell } from "./shell/Shell";
import { parseSearch, stringifySearch } from "./lib/url";

const rootRoute = createRootRoute({ component: () => <Outlet />, notFoundComponent: NotFoundPage });

const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "login", component: LoginPage });

const shareRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "s/$token",
  component: function ShareRoute() {
    const { token } = shareRoute.useParams();
    return <SharePage token={token} />;
  },
});

const appRoute = createRoute({ getParentRoute: () => rootRoute, id: "app", component: AppShell, notFoundComponent: NotFoundPage });

const nowRoute = createRoute({ getParentRoute: () => appRoute, path: "/", component: NowPage });
const projectsRoute = createRoute({ getParentRoute: () => appRoute, path: "projects", component: ProjectsPage });
const machinesRoute = createRoute({ getParentRoute: () => appRoute, path: "machines", component: () => <Navigate to="/lab" replace /> });
const labRoute = createRoute({ getParentRoute: () => appRoute, path: "lab", component: MachinesPage });
const evidenceRoute = createRoute({ getParentRoute: () => appRoute, path: "evidence", component: ProjectsPage });
const studioRoute = createRoute({ getParentRoute: () => appRoute, path: "studio", component: StudioPage });
const studioEditRoute = createRoute({ getParentRoute: () => appRoute, path: "studio/edit", component: StudioEditPage });
const scoreRoute = createRoute({ getParentRoute: () => appRoute, path: "studio/score", component: ScorePage });
const nodesRoute = createRoute({ getParentRoute: () => appRoute, path: "nodes", component: NodesPage });
const nodeRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "nodes/$id",
  component: function NodeRoute() {
    const { id } = nodeRoute.useParams();
    return <NodePage key={id} id={id} />;
  },
});
const pluginsRoute = createRoute({ getParentRoute: () => appRoute, path: "plugins", component: PluginsPage });
const pluginRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "plugins/$id",
  component: function PluginRoute() {
    const { id } = pluginRoute.useParams();
    return <PluginPage key={id} id={id} />;
  },
});
const pluginPageRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "x/$plugin/$id",
  component: function PluginPageRoute() {
    const { plugin, id } = pluginPageRoute.useParams();
    return <PluginRoutePage plugin={plugin} id={id} />;
  },
});

function projectRoute(path: string, tab: ProjectTab) {
  const route = createRoute({
    getParentRoute: () => appRoute,
    path,
    component: function ProjectRoute() {
      const { owner, name } = route.useParams() as { owner: string; name: string };
      return <ProjectPage key={`${owner}/${name}`} owner={owner} name={name} tab={tab} />;
    },
  });
  return route;
}

const projectSessionsRoute = projectRoute("p/$owner/$name", "sessions");
const projectFilesRoute = projectRoute("p/$owner/$name/files", "files");
const projectSettingsRoute = projectRoute("p/$owner/$name/settings", "settings");
const projectAccessRoute = projectRoute("p/$owner/$name/access", "access");

const projectArtifactRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "p/$owner/$name/a/$id",
  component: function ProjectArtifactRoute() {
    const { owner, name, id } = projectArtifactRoute.useParams();
    return <ArtifactPage key={id} owner={owner} name={name} slug={null} id={id} />;
  },
});

const sessionRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "p/$owner/$name/s/$slug",
  component: function SessionRoute() {
    const { owner, name, slug } = sessionRoute.useParams();
    return <SessionPage key={`${owner}/${name}/${slug}`} projectId={`${owner}/${name}`} slug={slug} tab="artifacts" />;
  },
});

const timelineRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "p/$owner/$name/s/$slug/timeline",
  component: function TimelineRoute() {
    const { owner, name, slug } = timelineRoute.useParams();
    return <SessionPage key={`${owner}/${name}/${slug}`} projectId={`${owner}/${name}`} slug={slug} tab="timeline" />;
  },
});

const sessionsRoute = createRoute({ getParentRoute: () => appRoute, path: "sessions", component: SessionsPage });

const sharedSessionRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "sessions/$slug",
  component: function SharedSessionRoute() {
    const { slug } = sharedSessionRoute.useParams();
    return <SessionPage key={`*/${slug}`} projectId={null} slug={slug} tab="artifacts" />;
  },
});

const sharedTimelineRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "sessions/$slug/timeline",
  component: function SharedTimelineRoute() {
    const { slug } = sharedTimelineRoute.useParams();
    return <SessionPage key={`*/${slug}`} projectId={null} slug={slug} tab="timeline" />;
  },
});

const sharedArtifactRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "sessions/$slug/a/$id",
  component: function SharedArtifactRoute() {
    const { slug, id } = sharedArtifactRoute.useParams();
    return <ArtifactPage key={id} slug={slug} id={id} shared />;
  },
});

const artifactRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "p/$owner/$name/s/$slug/a/$id",
  component: function ArtifactRoute() {
    const { owner, name, slug, id } = artifactRoute.useParams();
    return <ArtifactPage key={id} owner={owner} name={name} slug={slug} id={id} />;
  },
});

const sidRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "sid/$sid",
  component: function SidRoute() {
    const { sid } = sidRoute.useParams();
    return <SidRedirectPage sid={sid} />;
  },
});

const searchRoute = createRoute({ getParentRoute: () => appRoute, path: "search", component: SearchPage });
const cleanupRoute = createRoute({ getParentRoute: () => appRoute, path: "cleanup", component: CleanupPage });
const devicesRoute = createRoute({ getParentRoute: () => appRoute, path: "devices", component: DevicesPage });
const deviceRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "devices/$kind/$n",
  component: function DeviceRoute() {
    const { kind, n } = deviceRoute.useParams();
    return <DeviceDetailPage key={`${kind}:${n}`} kind={kind} index={n} />;
  },
});
const browsersRoute = createRoute({ getParentRoute: () => appRoute, path: "browsers", component: () => <BrowsersPage tab="profiles" /> });
const processesRoute = createRoute({ getParentRoute: () => appRoute, path: "browsers/processes", component: () => <BrowsersPage tab="processes" /> });
const profileRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "browsers/profiles/$id",
  component: function ProfileRoute() {
    const { id } = profileRoute.useParams();
    return <ProfileDetailPage key={id} id={id} />;
  },
});
const backendsRoute = createRoute({ getParentRoute: () => appRoute, path: "backends", component: () => <BackendsPage tab="instances" /> });
const definitionsRoute = createRoute({ getParentRoute: () => appRoute, path: "backends/definitions", component: () => <BackendsPage tab="definitions" /> });
const backendRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "backends/$",
  component: function BackendRoute() {
    const { _splat } = backendRoute.useParams();
    if (!_splat) return <BackendsPage tab="instances" />;
    return <BackendDetailPage key={_splat} splat={_splat} />;
  },
});
const settingsIndexRoute = createRoute({ getParentRoute: () => appRoute, path: "settings", component: () => <Navigate to="/settings/$group" params={{ group: "server" }} replace /> });
const settingsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "settings/$group",
  component: function SettingsRoute() {
    const { group } = settingsRoute.useParams();
    return <SettingsPage group={group} />;
  },
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  shareRoute,
  appRoute.addChildren([
    nowRoute,
    projectsRoute,
    machinesRoute,
    labRoute,
    evidenceRoute,
    studioRoute,
    studioEditRoute,
    scoreRoute,
    nodesRoute,
    nodeRoute,
    pluginsRoute,
    pluginRoute,
    pluginPageRoute,
    projectSessionsRoute,
    projectFilesRoute,
    projectSettingsRoute,
    projectAccessRoute,
    projectArtifactRoute,
    sessionRoute,
    timelineRoute,
    sessionsRoute,
    sharedSessionRoute,
    sharedTimelineRoute,
    sharedArtifactRoute,
    artifactRoute,
    sidRoute,
    searchRoute,
    cleanupRoute,
    devicesRoute,
    deviceRoute,
    browsersRoute,
    processesRoute,
    profileRoute,
    backendsRoute,
    definitionsRoute,
    backendRoute,
    settingsIndexRoute,
    settingsRoute,
  ]),
]);

export const router = createRouter({
  routeTree,
  parseSearch,
  stringifySearch,
  defaultPreload: false,
  scrollRestoration: true,
});

onUnauthorized((path) => {
  if (window.location.pathname.startsWith("/login") || window.location.pathname.startsWith("/s/")) return;
  void router.navigate({ to: "/login", search: { next: path, notice: "expired" } as never, replace: true });
});
