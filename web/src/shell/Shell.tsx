import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, setCsrfToken } from "../api/client";
import { keys, useMe, useProjects, useStatus } from "../api/queries";
import { Button, IconButton } from "../components/Button";
import { useFocusTrap } from "../components/Dialog";
import { OverflowMenu, separator, type MenuItem } from "../components/Menu";
import { EXACT_ACTIVE, Meter, NO_ACTIVE_PROPS } from "../components/Misc";
import { AuthContext, type AuthInfo } from "../lib/auth";
import { retryNow, startEvents, useConnection } from "../lib/events";
import { formatSize } from "../lib/format";
import { isMac, isTypingTarget, overlayOpen, shortcutAllowed, useAnnouncer, useIsDesktop, useIsSmall, useKeydown, useSecondTicker } from "../lib/hooks";
import { setTheme, useTheme, type Theme } from "../lib/theme";
import { usePathname } from "../lib/url";
import { usePlugins, useNodes } from "../api/system";
import { CommandPalette } from "./Palette";
import { PluginSlot } from "./plugins";
import { ProjectSwitcher, StatusBar } from "./StatusBar";
import { ShortcutsDialog } from "./ShortcutsDialog";

export function AppShell() {
  const me = useMe();
  const navigate = useNavigate();
  const pathname = usePathname();
  const client = useQueryClient();
  const [retryIn, setRetryIn] = useState(5);

  useEffect(() => {
    if (me.data) setCsrfToken(me.data.csrfToken ?? null);
  }, [me.data]);

  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) {
      void navigate({ to: "/login", search: { next: pathname + window.location.search } as never, replace: true });
    }
  }, [me.error, navigate, pathname]);

  const unreachable = me.isError && me.error instanceof ApiError && me.error.isNetwork;
  useEffect(() => {
    if (!unreachable) return;
    setRetryIn(5);
    const timer = window.setInterval(() => {
      setRetryIn((value) => {
        if (value <= 1) {
          void me.refetch();
          return 5;
        }
        return value - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [unreachable, me]);

  useEffect(() => {
    if (!me.data) return;
    return startEvents(client);
  }, [me.data, client]);

  const auth = useMemo<AuthInfo | null>(() => {
    if (!me.data) return null;
    return { me: me.data, isAdmin: me.data.user.role === "admin", authEnabled: me.data.authEnabled, username: me.data.user.username };
  }, [me.data]);

  if (!auth) {
    if (unreachable) {
      return (
        <BareFrame>
          <div className="state" role="alert">
            <p className="state-sentence">Could not reach the daemon at {window.location.host}.</p>
            <p className="state-hint">
              Start it with <span className="mono">eks-harness daemon start</span>, then try again. Retrying in <span className="num">{retryIn}</span> s.
            </p>
            <div className="button-row">
              <Button onClick={() => void me.refetch()}>Try again</Button>
            </div>
          </div>
        </BareFrame>
      );
    }
    if (me.isError && !(me.error instanceof ApiError && me.error.status === 401)) {
      return (
        <BareFrame>
          <div className="state" role="alert">
            <p className="state-sentence state-error">Could not load your account: {me.error instanceof Error ? me.error.message : "unknown error"}.</p>
            <div className="button-row">
              <Button onClick={() => void me.refetch()}>Try again</Button>
            </div>
          </div>
        </BareFrame>
      );
    }
    return <BareFrame busy />;
  }

  return (
    <AuthContext.Provider value={auth}>
      <SignedInShell auth={auth} />
    </AuthContext.Provider>
  );
}

function BareFrame({ children, busy }: { children?: React.ReactNode; busy?: boolean }) {
  return (
    <div className="shell">
      <header className="bar">
        <span className="wordmark">eks-harness</span>
      </header>
      <main id="main" className="main" aria-busy={busy || undefined}>
        {children ? <div className="page">{children}</div> : null}
      </main>
    </div>
  );
}

interface Section {
  to: string;
  label: string;
  match: (path: string) => boolean;
  count?: number;
  countLabel?: string;
}

function useSections(auth: AuthInfo): Section[] {
  const projects = useProjects();
  const status = useStatus();
  const nodes = useNodes();
  const plugins = usePlugins(null);
  const unseen = projects.data?.items.reduce((sum, p) => sum + (p.unseenCount ?? 0), 0) ?? 0;
  const queued = status.data?.queue?.length ?? 0;
  const offline = nodes.data?.items.filter((n) => n.state === "offline").length ?? 0;
  const pending = plugins.data?.items.filter((p) => p.state === "pending" || p.state === "changed").length ?? 0;
  return [
    { to: "/", label: "Now", match: (p) => p === "/" },
    { to: "/lab", label: "Lab", match: (p) => /^\/(lab|machines|devices|browsers|backends)(\/|$)/.test(p), count: queued || undefined, countLabel: `${queued} queued` },
    { to: "/evidence", label: "Evidence", match: (p) => /^\/(evidence|sessions|projects|p|sid|search|cleanup)(\/|$)/.test(p), count: unseen || undefined, countLabel: `${unseen} not seen` },
    { to: "/studio", label: "Studio", match: (p) => p.startsWith("/studio") },
    { to: "/nodes", label: "Nodes", match: (p) => p.startsWith("/nodes"), count: offline || undefined, countLabel: `${offline} offline` },
    { to: "/plugins", label: "Plugins", match: (p) => p.startsWith("/plugins") || p.startsWith("/x/"), count: pending || undefined, countLabel: `${pending} waiting for approval` },
    auth.isAdmin ? { to: "/settings/server", label: "Settings", match: (p) => p.startsWith("/settings") } : { to: "/settings/keys", label: "API keys", match: (p) => p.startsWith("/settings") },
  ];
}

const CHORDS: Record<string, string> = { n: "/", l: "/lab", e: "/evidence", s: "/studio", o: "/nodes", p: "/plugins", d: "/devices", b: "/browsers", k: "/backends" };

function SignedInShell({ auth }: { auth: AuthInfo }) {
  const desktop = useIsDesktop();
  const small = useIsSmall();
  const [menuOpen, setMenuOpen] = useState(false);
  const [help, setHelp] = useState(false);
  const [palette, setPalette] = useState(false);
  const [chord, setChord] = useState(false);
  const chordTimer = useRef<number | null>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();
  const pathname = usePathname();
  const announcement = useAnnouncer();
  const locationKey = useRouterState({ select: (s) => s.location.pathname });
  const sections = useSections(auth);

  useEffect(() => {
    setMenuOpen(false);
    const timer = window.setTimeout(() => {
      if (document.activeElement && document.activeElement !== document.body && !document.activeElement.closest(".bar")) return;
      document.querySelector<HTMLElement>("[data-page-title]")?.focus({ preventScroll: true });
    }, 30);
    return () => window.clearTimeout(timer);
  }, [locationKey]);

  useKeydown((event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      setPalette((open) => !open);
      return;
    }
    if (event.key === "Escape" && !overlayOpen() && isTypingTarget(event.target)) {
      (event.target as HTMLElement).blur();
      return;
    }
    if (!shortcutAllowed(event)) return;
    if (chord) {
      setChord(false);
      if (chordTimer.current !== null) window.clearTimeout(chordTimer.current);
      const target = event.key === "," ? (auth.isAdmin ? "/settings/server" : "/settings/keys") : CHORDS[event.key];
      if (target) {
        event.preventDefault();
        void navigate({ to: target });
      }
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      void navigate({ to: "/search" });
    } else if (event.key === "?") {
      event.preventDefault();
      setHelp(true);
    } else if (event.key === "g") {
      event.preventDefault();
      setChord(true);
      chordTimer.current = window.setTimeout(() => setChord(false), 1000);
    }
  });

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="bar">
        <Link to="/" className="wordmark" aria-label="eks-harness, now">
          eks-harness
        </Link>
        {!small ? <SectionNav sections={sections} pathname={pathname} /> : null}
        {small ? <span className="bar-section">{sections.find((section) => section.match(pathname))?.label ?? ""}</span> : null}
        {!small ? <PluginSlot slot="nav.section" /> : null}
        <span className="bar-fill" />
        <BarStatus chord={chord} />
        {desktop ? <ProjectSwitcher /> : null}
        {desktop ? (
          <button type="button" className="bar-find" onClick={() => setPalette(true)} aria-keyshortcuts="Meta+K">
            <span>Find or run</span>
            <span className="kbd">{isMac() ? "\u2318K" : "Ctrl K"}</span>
          </button>
        ) : (
          <Button size="sm" onClick={() => setPalette(true)}>
            Find
          </Button>
        )}
        {small ? (
          <Button ref={menuButton} size="sm" onClick={() => setMenuOpen(true)} aria-haspopup="dialog" aria-expanded={menuOpen}>
            Menu
          </Button>
        ) : (
          <AccountMenu auth={auth} onHelp={() => setHelp(true)} />
        )}
      </header>
      {menuOpen ? (
        <PhoneMenu
          auth={auth}
          sections={sections}
          pathname={pathname}
          onClose={() => {
            setMenuOpen(false);
            menuButton.current?.focus();
          }}
        />
      ) : null}
      <main id="main" className="main" tabIndex={-1}>
        <Outlet />
      </main>
      <div className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </div>
      {help ? <ShortcutsDialog onClose={() => setHelp(false)} /> : null}
      {palette ? <CommandPalette onClose={() => setPalette(false)} extra={[{ id: "help", group: "Help", label: "Keyboard shortcuts", hint: "?", run: () => { setPalette(false); setHelp(true); } }]} /> : null}
      {desktop ? <StatusBar /> : null}
    </div>
  );
}

function SectionNav({ sections, pathname }: { sections: Section[]; pathname: string }) {
  return (
    <nav className="bar-nav" aria-label="Main">
      {sections
        .map((section) => (
          <Link key={section.label} to={section.to} className="bar-item" activeOptions={EXACT_ACTIVE} activeProps={NO_ACTIVE_PROPS} aria-current={section.match(pathname) ? "page" : undefined}>
            {section.label}
            {section.count ? (
              <span className="bar-count" aria-label={section.countLabel}>
                {section.count.toLocaleString("en-US")}
              </span>
            ) : null}
          </Link>
        ))}
    </nav>
  );
}

function BarStatus({ chord }: { chord: boolean }) {
  const connection = useConnection();
  const now = useSecondTicker(connection.state === "disconnected");
  let text: React.ReactNode = null;
  if (connection.state === "reconnecting") text = <span className="ink-2">Live updates paused, reconnecting{"…"}</span>;
  else if (connection.state === "restarting") text = <span className="ink-2">Daemon restarting{"…"}</span>;
  else if (connection.state === "disconnected") {
    const seconds = connection.retryAt ? Math.max(0, Math.ceil((connection.retryAt - now) / 1000)) : 5;
    text = (
      <>
        <span className="bar-alert">Lost connection to the daemon. Retrying in {seconds} s.</span>{" "}
        <button type="button" className="link" onClick={retryNow}>
          Retry now
        </button>
      </>
    );
  }
  if (!text && !chord) return null;
  return (
    <span className="bar-status" role="status">
      {chord ? <span className="kbd">g</span> : null}
      {text}
    </span>
  );
}

function useSignOut() {
  const navigate = useNavigate();
  const client = useQueryClient();
  return async () => {
    try {
      await api.post("/api/auth/logout", {}, { handleUnauthorized: false });
    } catch {
      setCsrfToken(null);
    }
    setCsrfToken(null);
    client.clear();
    void navigate({ to: "/login" });
  };
}

function StorageLine({ compact }: { compact?: boolean }) {
  const status = useStatus();
  const storage = status.data?.storage;
  if (!storage) return null;
  const quota = storage.quotaBytes ?? null;
  return (
    <div className="storage-line">
      <span className={storage.overQuota ? "bar-alert" : undefined}>
        {storage.overQuota && quota ? `Storage ${formatSize(storage.usedBytes)}, over the ${formatSize(quota)} quota` : `Storage ${formatSize(storage.usedBytes)}${quota ? ` of ${formatSize(quota)}` : ""}`}
      </span>
      {quota && !compact ? <Meter value={storage.usedBytes} max={quota} over={storage.overQuota} /> : null}
    </div>
  );
}

function AccountMenu({ auth, onHelp }: { auth: AuthInfo; onHelp: () => void }) {
  const navigate = useNavigate();
  const theme = useTheme();
  const status = useStatus();
  const signOut = useSignOut();
  const storage = status.data?.storage;
  const items: MenuItem[] = [
    { label: `Signed in as ${auth.username} (${auth.isAdmin ? "admin" : "member"})`, kind: "text" },
    storage ? { label: `Storage ${formatSize(storage.usedBytes)}${storage.quotaBytes ? ` of ${formatSize(storage.quotaBytes)}` : ""}`, kind: "text" } : null,
    status.data ? { label: `Version ${status.data.version}`, onSelect: () => void navigate({ to: auth.isAdmin ? "/settings/daemon" : "/settings/keys" }) } : null,
    separator(),
    ...(["system", "light", "dark"] as Theme[]).map<MenuItem>((value) => ({ label: `Theme: ${value[0].toUpperCase()}${value.slice(1)}`, kind: "radio", checked: theme === value, onSelect: () => setTheme(value) })),
    separator(),
    auth.isAdmin ? { label: "Settings", onSelect: () => void navigate({ to: "/settings/server" }) } : null,
    { label: "API keys", onSelect: () => void navigate({ to: "/settings/keys" }) },
    { label: "Keyboard shortcuts", onSelect: onHelp },
    auth.authEnabled ? { label: "Sign out", onSelect: () => void signOut() } : null,
  ].filter(Boolean) as MenuItem[];
  return (
    <OverflowMenu
      items={items}
      label="Account, theme and settings"
      trigger={({ ref, onClick, expanded }) => (
        <button ref={ref} type="button" className="bar-account" aria-haspopup="menu" aria-expanded={expanded} onClick={onClick}>
          <span className="ellipsis">{auth.authEnabled ? auth.username : "local"}</span>
        </button>
      )}
    />
  );
}

function PhoneMenu({ auth, sections, pathname, onClose }: { auth: AuthInfo; sections: Section[]; pathname: string; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useTheme();
  const status = useStatus();
  const signOut = useSignOut();
  useFocusTrap(ref, true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="phone-menu" data-overlay-open="" ref={ref} role="dialog" aria-modal="true" aria-label="Menu">
      <div className="bar">
        <span className="wordmark">eks-harness</span>
        <span className="bar-fill" />
        <IconButton icon="close" label="Close menu" shortcut="Escape" onClick={onClose} />
      </div>
      <nav className="phone-menu-nav" aria-label="Main">
        {sections.map((section) => (
          <Link key={section.label} to={section.to} className="phone-menu-item" aria-current={section.match(pathname) ? "page" : undefined} onClick={onClose}>
            <span>{section.label}</span>
            {section.count ? <span className="num ink-3">{section.count.toLocaleString("en-US")}</span> : null}
          </Link>
        ))}
      </nav>
      <div className="phone-menu-foot">
        <StorageLine />
        <p className="ink-3">
          {auth.authEnabled ? `Signed in as ${auth.username}` : "Local mode, sign-in is off"}
          {status.data ? <>, version {status.data.version}</> : null}
        </p>
        <fieldset className="phone-theme">
          <legend className="field-label">Theme</legend>
          <div className="seg">
            {(["system", "light", "dark"] as Theme[]).map((value) => (
              <button key={value} type="button" className="seg-item" aria-pressed={theme === value} onClick={() => setTheme(value)}>
                {value[0].toUpperCase() + value.slice(1)}
              </button>
            ))}
          </div>
        </fieldset>
        {auth.authEnabled ? (
          <Button onClick={() => void signOut()} block>
            Sign out
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export { StorageLine };

export function invalidateMe(client: ReturnType<typeof useQueryClient>): void {
  void client.invalidateQueries({ queryKey: keys.me });
}
