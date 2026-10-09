import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useBlocker, useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, errorText, setCsrfToken } from "../api/client";
import { fetchers, keys, useGrants, useKeys, useProjects, useSettings, useStatus, useUsers, useVersion } from "../api/queries";
import type { ApiKeyCreated, ApiKeyOut, GrantOut, SettingOut, SettingsPatchResponse, SettingsResponse, UserOut } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Checkbox, Field, NumberInput, PasswordInput, Radio, Select, Textarea, TextInput } from "../components/Form";
import { OverflowMenu, separator, type MenuItem } from "../components/Menu";
import { AbsTime, CopyField, DefList, EXACT_ACTIVE, Mono, NO_ACTIVE_PROPS, RelTime, queryState } from "../components/Misc";
import { EmptyState, Notice, ResultText } from "../components/Notice";
import { DataTable, type Column } from "../components/Table";
import { AnnotationAssets } from "../features/annotations";
import { useAuth } from "../lib/auth";
import { markRestarting, useConnection } from "../lib/events";
import { formatClock, formatFull, formatRelative } from "../lib/format";
import { nowTime, useIsDesktop, useTitle } from "../lib/hooks";
import { useSearchParams, useSetParams } from "../lib/url";
import { GrantForm, RemoveGrantConfirm } from "./Project";

export const SETTING_GROUPS: { id: string; label: string; prefixes: string[] }[] = [
  { id: "server", label: "Server", prefixes: ["server."] },
  { id: "auth", label: "Authentication", prefixes: ["auth."] },
  { id: "browser", label: "Browser", prefixes: ["browser."] },
  { id: "devices", label: "Devices", prefixes: ["devices.", "apps.", "live."] },
  { id: "capture", label: "Capture", prefixes: ["capture."] },
  { id: "leases", label: "Leases", prefixes: ["lease."] },
  { id: "backends", label: "Backends", prefixes: ["backend."] },
  { id: "storage", label: "Storage", prefixes: ["storage.", "retention.", "events."] },
];

const OTHER_PAGES: { id: string; label: string; admin: boolean }[] = [
  { id: "keys", label: "API keys", admin: false },
  { id: "users", label: "Users", admin: true },
  { id: "grants", label: "Grants", admin: true },
  { id: "daemon", label: "Daemon", admin: true },
];

const LABELS: Record<string, string> = {
  "server.host": "Listen address",
  "server.port": "Port",
  "server.publicUrl": "Public URL",
  "server.trustedProxies": "Trusted proxies",
  "server.cloudflare": "Trust Cloudflare headers (CF-Connecting-IP, X-Forwarded-Proto)",
  "auth.enabled": "Require sign-in",
  "auth.sessionHours": "Web session lifetime",
  "auth.cookieSecure": "Send the session cookie over HTTPS only",
  "browser.command": "Browser",
  "browser.instances": "Browser processes",
  "browser.profilesPerInstance": "Profiles per process",
  "browser.idleSeconds": "Quit an unused browser after",
  "browser.extraArgs": "Extra browser arguments",
  "devices.ios": "iOS simulators",
  "devices.android": "Android emulators",
  "devices.maxRunning": "Devices running at once",
  "devices.idleSeconds": "Shut down a free device after",
  "devices.iosDeviceType": "Simulator device type",
  "devices.androidBaseAvd": "Base AVD",
  "devices.androidPortBase": "First emulator console port",
  "apps.iosBundleId": "iOS app bundle id",
  "apps.androidPackage": "Android app package",
  "live.idleStopSeconds": "Stop a live stream after the last viewer leaves",
  "live.maxFps": "Live stream frame rate",
  "live.jpegQuality": "Live stream JPEG quality",
  "capture.pace.moveMs": "Pointer move time",
  "capture.pace.dwellMs": "Dwell before each action",
  "capture.pace.typeMsPerChar": "Typing speed",
  "capture.pace.holdMs": "Hold after each interaction",
  "capture.pace.screenHoldMs": "Hold on new screens",
  "capture.pace.mobilePressMs": "Mobile press marker time",
  "capture.trim.keepBeforeSec": "Keep before each interaction",
  "capture.trim.keepAfterSec": "Keep after each interaction",
  "capture.trim.maxSpeedup": "Fastest idle speedup",
  "capture.trim.minScreenSec": "Shortest screen time",
  "capture.hideSelectors": "Hidden selectors during capture",
  "capture.annotate.matchPx": "Annotation re-measure tolerance",
  "capture.annotate.minContrast": "Annotation minimum contrast",
  "capture.annotate.minContrastLarge": "Annotation large-text contrast",
  "capture.annotate.minTextPx": "Annotation minimum text size",
  "capture.pointer": "Video pointer",
  "lease.idleSeconds": "Release a lease without heartbeat after",
  "lease.agentStopGraceSeconds": "Idle limit after an agent's turn ends",
  "lease.queueTimeoutSeconds": "Drop a queued request without a poll after",
  "backend.idleGraceSeconds": "Stop an unbound backend after",
  "backend.portRangeStart": "First backend port",
  "backend.portRangeEnd": "Last backend port",
  "backend.definitionDirs": "Backend definition folders",
  "daemon.idleExitSeconds": "Exit when idle after",
  "daemon.tickSeconds": "Housekeeping interval",
  "daemon.sweepSeconds": "Sweep interval",
  "storage.maxUploadMb": "Largest upload",
  "storage.quotaGb": "Storage quota",
  "retention.defaultDays": "Default retention",
  "retention.checkMinutes": "Retention check interval",
  "events.retentionDays": "Keep activity events for",
};

function unitFor(key: string): string {
  const last = key.split(".").pop() ?? "";
  if (/Ms$/.test(last)) return "ms";
  if (/Seconds$/.test(last)) return "seconds";
  if (/Minutes$/.test(last)) return "minutes";
  if (/Hours$/.test(last)) return "hours";
  if (/Days$/.test(last)) return "days";
  if (/Mb$/.test(last)) return "MB";
  if (/Gb$/.test(last)) return "GB";
  if (/Fps$/.test(last)) return "fps";
  return "";
}

function isList(setting: SettingOut): boolean {
  return setting.type.startsWith("list");
}

function toDraft(setting: SettingOut, value: unknown): string | boolean {
  if (setting.type === "bool") return Boolean(value);
  if (isList(setting)) return Array.isArray(value) ? value.join("\n") : "";
  if (value === null || value === undefined) return "";
  return String(value);
}

function fromDraft(setting: SettingOut, draft: string | boolean): { value: unknown; error?: string } {
  if (setting.type === "bool") return { value: Boolean(draft) };
  const text = String(draft);
  if (isList(setting)) return { value: text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean) };
  if (setting.type === "int" || setting.type === "float") {
    if (!text.trim()) return setting.nullable ? { value: null } : { value: null, error: "Enter a number." };
    const number = Number(text);
    if (!Number.isFinite(number) || (setting.type === "int" && !Number.isInteger(number))) return { value: null, error: "Enter a whole number." };
    if (setting.minimum !== null && number < setting.minimum) return { value: null, error: `The smallest value is ${setting.minimum}.` };
    if (setting.maximum !== null && number > setting.maximum) return { value: null, error: `The largest value is ${setting.maximum}.` };
    return { value: number };
  }
  return { value: text };
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function RestartControl({ settings }: { settings: SettingsResponse | undefined }) {
  const connection = useConnection();
  const [confirm, setConfirm] = useState(false);
  const pending = settings?.restartPendingKeys ?? [];
  if (connection.state === "restarting") return <Notice variant="attention" title={"Restarting…"} />;
  if (connection.restartedAt && !pending.length) return <ResultText>Restarted at {formatClock(new Date(connection.restartedAt))}.</ResultText>;
  if (!pending.length && !settings?.restartPending) return null;
  const count = pending.length || 1;
  return (
    <>
      <Notice variant="attention" title={`${count === 1 ? "1 change waits" : `${count} changes wait`} for a restart.`} action={<Button variant="primary" onClick={() => setConfirm(true)}>Restart daemon</Button>} />
      {confirm ? <RestartConfirm onClose={() => setConfirm(false)} /> : null}
    </>
  );
}

function RestartConfirm({ onClose }: { onClose: () => void }) {
  return (
    <ConfirmDialog
      title="Restart the daemon?"
      body="Leases, browsers, devices and backends are kept and re-adopted. This page reconnects in a few seconds."
      confirmLabel="Restart daemon"
      busyLabel={"Restarting…"}
      destructive={false}
      onClose={onClose}
      errorFor={(err) => errorText(err, "restart", "the daemon")}
      onConfirm={async () => {
        await api.post("/api/daemon/restart", {});
        markRestarting();
      }}
    />
  );
}

export function SettingsPage({ group }: { group: string }) {
  const auth = useAuth();
  const navigate = useNavigate();
  const desktop = useIsDesktop();
  const settingGroup = SETTING_GROUPS.find((g) => g.id === group);
  const other = OTHER_PAGES.find((p) => p.id === group);
  const title = settingGroup?.label ?? other?.label ?? "Settings";
  useTitle(`${title} - Settings`);

  useEffect(() => {
    if (!settingGroup && !other) void navigate({ to: auth.isAdmin ? "/settings/server" : "/settings/keys", replace: true });
    else if (!auth.isAdmin && (settingGroup || other?.admin)) void navigate({ to: "/settings/keys", replace: true });
  }, [group, auth.isAdmin]);

  const pages = [
    ...(auth.isAdmin ? SETTING_GROUPS.map((g) => ({ id: g.id, label: g.label, sep: false })) : []),
    ...OTHER_PAGES.filter((p) => auth.isAdmin || !p.admin).map((p, i) => ({ id: p.id, label: p.label, sep: i === 0 && auth.isAdmin })),
  ];

  let content: ReactNode = null;
  if (settingGroup && auth.isAdmin) content = <SettingsGroupForm group={settingGroup} />;
  else if (group === "keys") content = <KeysPage />;
  else if (group === "users" && auth.isAdmin) content = <UsersPage />;
  else if (group === "grants" && auth.isAdmin) content = <GrantsPage />;
  else if (group === "daemon" && auth.isAdmin) content = <DaemonPage />;

  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title" tabIndex={-1} data-page-title="">
          Settings
        </h1>
      </div>
      <div className="settings-split">
        {desktop ? (
          <nav className="subnav" aria-label="Settings sections">
            <ul>
              {pages.map((page) => (
                <li key={page.id} className={page.sep ? "subnav-sep" : undefined}>
                  <Link to={`/settings/${page.id}`} className="nav-item" activeOptions={EXACT_ACTIVE} activeProps={NO_ACTIVE_PROPS} aria-current={page.id === group ? "page" : undefined}>
                    {page.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ) : (
          <Select value={group} onChange={(event) => void navigate({ to: `/settings/${event.target.value}` })} aria-label="Section">
            {pages.map((page) => (
              <option key={page.id} value={page.id}>
                Section: {page.label}
              </option>
            ))}
          </Select>
        )}
        <div className="settings-content">{content}</div>
      </div>
    </div>
  );
}

function SettingsGroupForm({ group }: { group: (typeof SETTING_GROUPS)[number] }) {
  const client = useQueryClient();
  const settings = useSettings();
  const users = useUsers();
  const [drafts, setDrafts] = useState<Record<string, string | boolean>>({});
  const [unset, setUnset] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState("");
  const [saved, setSaved] = useState("");
  const [savedRestart, setSavedRestart] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [confirmAuthOff, setConfirmAuthOff] = useState(false);

  const rows = useMemo(() => (settings.data?.settings ?? []).filter((s) => group.prefixes.some((p) => s.key.startsWith(p))), [settings.data, group]);
  useEffect(() => {
    setDrafts({});
    setUnset(new Set());
    setErrors({});
    setSaved("");
    setFormError("");
  }, [group.id]);

  const dirtyKeys = rows.filter((s) => {
    if (unset.has(s.key)) return s.source === "file";
    if (!(s.key in drafts)) return false;
    const parsed = fromDraft(s, drafts[s.key]);
    return parsed.error !== undefined || !same(parsed.value, s.value);
  });
  const dirty = dirtyKeys.length > 0;

  const blocker = useBlocker({ shouldBlockFn: () => dirty, enableBeforeUnload: () => dirty, withResolver: true });

  const state = queryState(settings, "settings");
  if (state) return <>{state}</>;

  const current = (s: SettingOut) => (unset.has(s.key) ? toDraft(s, s.default) : s.key in drafts ? drafts[s.key] : toDraft(s, s.value));

  const setDraft = (s: SettingOut, value: string | boolean) => {
    setSaved("");
    setUnset((prev) => {
      const next = new Set(prev);
      next.delete(s.key);
      return next;
    });
    if (s.key === "auth.enabled" && value === true) {
      const admins = (users.data?.items ?? []).filter((u) => u.role === "admin" && !u.builtin && !u.disabled);
      if (users.data && !admins.length) {
        setErrors((prev) => ({ ...prev, [s.key]: "Create an admin user with a password or an API key first." }));
      } else {
        setErrors((prev) => {
          const next = { ...prev };
          delete next[s.key];
          return next;
        });
      }
    }
    setDrafts((prev) => ({ ...prev, [s.key]: value }));
  };

  const save = async (confirmed = false) => {
    setFormError("");
    const values: Record<string, unknown> = {};
    const nextErrors: Record<string, string> = {};
    const unsetKeys: string[] = [];
    for (const s of dirtyKeys) {
      if (unset.has(s.key)) {
        unsetKeys.push(s.key);
        continue;
      }
      const parsed = fromDraft(s, drafts[s.key]);
      if (parsed.error) nextErrors[s.key] = parsed.error;
      else values[s.key] = parsed.value;
    }
    if (errors["auth.enabled"] && values["auth.enabled"] === true) nextErrors["auth.enabled"] = errors["auth.enabled"];
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) {
      setFormError("Fix the marked fields.");
      return;
    }
    if (values["auth.enabled"] === false && !confirmed) {
      setConfirmAuthOff(true);
      return;
    }
    setBusy(true);
    try {
      const response = await api.patch<SettingsPatchResponse>("/api/settings", { values, unset: unsetKeys });
      if (response?.settings) client.setQueryData(keys.settings, response.settings);
      else void client.invalidateQueries({ queryKey: keys.settings });
      void client.invalidateQueries({ queryKey: keys.status });
      void client.invalidateQueries({ queryKey: keys.me });
      const restartKeys = new Set((response?.changed ?? []).filter((c) => c.restartRequired).map((c) => c.key));
      setSavedRestart(restartKeys);
      setDrafts({});
      setUnset(new Set());
      setSaved(`Saved at ${nowTime()}`);
    } catch (err) {
      const fieldErrors: Record<string, string> = {};
      if (err instanceof ApiError) {
        const map = err.body?.errors;
        if (map && typeof map === "object") for (const [key, message] of Object.entries(map as Record<string, string>)) fieldErrors[key] = String(message);
        for (const problem of err.problems) {
          const key = rows.find((s) => problem.field.includes(s.key))?.key;
          if (key) fieldErrors[key] = problem.message;
        }
        const exposure = err.error === "exposure_refused" ? rows.find((s) => s.key === "server.host" || s.key === "auth.enabled")?.key : undefined;
        const key = typeof err.body?.key === "string" ? (err.body.key as string) : exposure ?? rows.find((s) => err.message.includes(s.key))?.key;
        if (key && !fieldErrors[key]) fieldErrors[key] = err.message;
      }
      setErrors(fieldErrors);
      setFormError(Object.keys(fieldErrors).length ? "Fix the marked fields." : errorText(err, "save", `${group.label.toLowerCase()} settings`));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
    <form
      className="form-grid"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
      noValidate
    >
      <h2 className="section-title">{group.label}</h2>
      <RestartControl settings={settings.data} />
      {rows.map((s) => (
        <SettingField key={s.key} setting={s} value={current(s)} error={errors[s.key]} savedRestart={savedRestart.has(s.key)} onChange={(value) => setDraft(s, value)} onDefault={() => {
          setSaved("");
          setDrafts((prev) => {
            const next = { ...prev };
            delete next[s.key];
            return next;
          });
          setUnset((prev) => new Set(prev).add(s.key));
        }} usingDefault={unset.has(s.key) || (!(s.key in drafts) && s.source === "default")} />
      ))}
      {!rows.length ? <EmptyState>No settings in this group.</EmptyState> : null}
      {formError ? <Notice variant="error">{formError}</Notice> : null}
      <div className="button-row">
        <Button type="submit" variant="primary" busy={busy} busyLabel={"Saving…"} disabled={!dirty}>
          Save {group.label.toLowerCase()} settings
        </Button>
        <Button
          disabled={!dirty || busy}
          onClick={() => {
            setDrafts({});
            setUnset(new Set());
            setErrors({});
            setFormError("");
          }}
        >
          Reset changes
        </Button>
        <ResultText>{saved}</ResultText>
      </div>
      {confirmAuthOff ? (
        <ConfirmDialog
          title="Turn off sign-in?"
          body={`Anyone who can reach ${window.location.host} gets full access.`}
          confirmLabel="Turn off sign-in"
          onClose={() => setConfirmAuthOff(false)}
          onConfirm={() => save(true)}
        />
      ) : null}
      {blocker.status === "blocked" ? (
        <ConfirmDialog
          title={`Leave without saving ${dirtyKeys.length === 1 ? "1 change" : `${dirtyKeys.length} changes`}?`}
          body="Your edits in this group are lost."
          confirmLabel="Leave"
          cancelLabel="Stay"
          onClose={() => blocker.reset?.()}
          onConfirm={() => blocker.proceed?.()}
        />
      ) : null}
    </form>
    {group.id === "capture" ? <AnnotationAssets /> : null}
    </>
  );
}

function SettingField({ setting, value, error, onChange, onDefault, usingDefault, savedRestart }: { setting: SettingOut; value: string | boolean; error?: string; onChange: (value: string | boolean) => void; onDefault: () => void; usingDefault: boolean; savedRestart: boolean }) {
  const id = useId();
  const errorId = useId();
  const label = LABELS[setting.key] ?? setting.key;
  const unit = unitFor(setting.key);
  const defaultText = setting.default === null || setting.default === undefined || setting.default === "" || (Array.isArray(setting.default) && !setting.default.length) ? "empty" : Array.isArray(setting.default) ? setting.default.join(", ") : String(setting.default);
  const differs = !usingDefault && !same(fromDraft(setting, value).value, setting.default);
  const fromEnv = setting.source === "env";
  const aside = savedRestart ? <span className="strong">Saved, applies after restart</span> : setting.restartRequired ? <span className="strong">Needs restart</span> : null;
  const description = setting.description ? setting.description[0].toUpperCase() + setting.description.slice(1) + (setting.description.endsWith(".") ? "" : ".") : "";

  if (setting.type === "bool") {
    return (
      <div className="setting-field">
        <div className="field-label-row">
          <Checkbox label={<span className="strong">{label}</span>} checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} aria-describedby={errorId} disabled={fromEnv} />
          {aside ? <span className="field-aside">{aside}</span> : null}
        </div>
        <p className="setting-key mono">{setting.key}</p>
        {error ? <p id={errorId} className="field-error">{error}</p> : description ? <p id={errorId} className="field-helper">{description}</p> : null}
        <DefaultLine text={defaultText} differs={differs} onDefault={onDefault} mono={false} fromEnv={fromEnv} envName={setting.envName} />
      </div>
    );
  }

  let input: ReactNode;
  if (setting.choices && setting.choices.length) {
    input = (
      <Select id={id} value={String(value)} onChange={(event) => onChange(event.target.value)} invalid={Boolean(error)} aria-describedby={errorId} disabled={fromEnv}>
        {setting.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    );
  } else if (isList(setting)) {
    input = <Textarea id={id} value={String(value)} onChange={(event) => onChange(event.target.value)} invalid={Boolean(error)} aria-describedby={errorId} mono disabled={fromEnv} />;
  } else if (setting.type === "int" || setting.type === "float") {
    input = <NumberInput id={id} value={String(value)} unit={unit} onChange={(event) => onChange(event.target.value.replace(/[^\d.-]/g, ""))} invalid={Boolean(error)} aria-describedby={errorId} disabled={fromEnv} placeholder={setting.nullable ? "Empty" : undefined} />;
  } else {
    input = <TextInput id={id} value={String(value)} onChange={(event) => onChange(event.target.value)} invalid={Boolean(error)} aria-describedby={errorId} disabled={fromEnv} />;
  }
  return (
    <Field
      className="setting-field"
      label={<span className="strong">{label}</span>}
      htmlFor={id}
      aside={aside}
      sub={<p className="setting-key mono">{setting.key}</p>}
      error={error}
      errorId={errorId}
      helper={isList(setting) ? `${description} One per line.` : description}
      footer={<DefaultLine text={defaultText} differs={differs} onDefault={onDefault} mono={setting.type === "str" || isList(setting)} fromEnv={fromEnv} envName={setting.envName} />}
    >
      {input}
    </Field>
  );
}

function DefaultLine({ text, differs, onDefault, mono, fromEnv, envName }: { text: string; differs: boolean; onDefault: () => void; mono: boolean; fromEnv: boolean; envName: string }) {
  return (
    <p className="field-helper">
      Default {mono && text !== "empty" ? <span className="mono">{text}</span> : text}
      {fromEnv ? (
        <>
          . Set by <span className="mono">{envName}</span>; change it there.
        </>
      ) : differs ? (
        <>
          {" "}
          <button type="button" className="link" onClick={onDefault}>
            Use default
          </button>
        </>
      ) : null}
    </p>
  );
}

function DaemonPage() {
  const version = useVersion();
  const status = useStatus();
  const settings = useSettings();
  const [restart, setRestart] = useState(false);
  const [stop, setStop] = useState(false);
  const [stopped, setStopped] = useState(false);
  const daemonGroup = { id: "daemon", label: "Daemon", prefixes: ["daemon."] };
  const s = status.data;
  const v = version.data;
  return (
    <div className="form-grid">
      <h2 className="section-title">Process</h2>
      <RestartControl settings={settings.data} />
      {stopped ? <Notice variant="attention" title="The daemon is stopping.">Start it again with eks-harness daemon start.</Notice> : null}
      {s || v ? (
        <DefList
          items={[
            { label: "Version", value: v?.version ?? s?.version ?? "" },
            v?.sourceHash || s?.sourceHash ? { label: "Source hash", value: (v?.sourceHash ?? s?.sourceHash ?? "").slice(0, 12), mono: true, copy: v?.sourceHash ?? s?.sourceHash ?? "" } : null,
            v?.builtAt ? { label: "Built", value: formatFull(v.builtAt) } : null,
            v?.editable ? { label: "Install", value: "Editable (runs from the source tree)" } : null,
            s ? { label: "Listening on", value: s.url.replace(/^https?:\/\//, "").replace(/\/$/, ""), mono: true } : null,
            s ? { label: "Public URL", value: s.publicUrl } : null,
            s ? { label: "Config file", value: s.configFile, mono: true, copy: s.configFile } : null,
            s ? { label: "Started", value: `${formatFull(s.startedAt)} (${formatRelative(s.startedAt)})` } : null,
            s ? { label: "Installed as", value: s.managed ? "Login service" : "Not installed as a service" } : null,
            s ? { label: "Process", value: String(s.pid), mono: true } : null,
            v ? { label: "Python", value: `${v.python} on ${v.platform}` } : null,
            s?.fakePools ? { label: "Pools", value: "Fake pools (test mode)" } : null,
          ]}
        />
      ) : (
        queryState(status, "daemon status")
      )}
      <div className="button-row">
        <Button onClick={() => setRestart(true)}>Restart daemon</Button>
        <Button onClick={() => setStop(true)}>
          {"Stop daemon…"}
        </Button>
      </div>
      <SettingsGroupForm group={daemonGroup} />
      {restart ? <RestartConfirm onClose={() => setRestart(false)} /> : null}
      {stop ? (
        <ConfirmDialog
          title="Stop the daemon?"
          body="Browsers, devices and backends keep running, but agents cannot acquire or capture until it starts again. This page stops working until you run eks-harness daemon start."
          confirmLabel="Stop daemon"
          busyLabel={"Stopping…"}
          onClose={() => setStop(false)}
          errorFor={(err) => errorText(err, "stop", "the daemon")}
          onConfirm={async () => {
            await api.post("/api/daemon/stop", {});
            setStopped(true);
          }}
        />
      ) : null}
    </div>
  );
}

function KeysPage() {
  const auth = useAuth();
  const client = useQueryClient();
  const navigate = useNavigate();
  const params = useSearchParams();
  const setParams = useSetParams();
  const users = useUsers(auth.isAdmin);
  const filterUser = auth.isAdmin ? params.user ?? "*" : "";
  const keyList = useKeys(filterUser === "*" ? "*" : filterUser);
  const [name, setName] = useState("");
  const owners = (users.data?.items ?? []).filter((u) => !u.disabled && !u.builtin);
  const [chosenOwner, setOwner] = useState<string | null>(null);
  const owner = auth.isAdmin ? chosenOwner ?? (owners.some((u) => u.username === auth.username) ? auth.username : owners[0]?.username ?? "") : auth.username;
  const noOwner = auth.isAdmin && users.isSuccess && owners.length === 0;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);
  const nameId = useId();
  const ownerId = useId();

  const create = async () => {
    setBusy(true);
    setError("");
    try {
      const body: Record<string, unknown> = { name: name.trim() };
      if (auth.isAdmin && owner) body.username = owner;
      const key = await api.post<ApiKeyCreated>("/api/keys", body);
      setCreated(key);
      setName("");
      void client.invalidateQueries({ queryKey: ["keys"] });
    } catch (err) {
      setError(errorText(err, "create", "the key"));
    } finally {
      setBusy(false);
    }
  };

  const items = [...(keyList.data?.items ?? [])].sort((a, b) => Number(b.active) - Number(a.active) || Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const columns: Column<ApiKeyOut>[] = [
    { key: "name", header: "Name", render: (k) => k.name || <span className="muted">Unnamed</span> },
    { key: "prefix", header: "Prefix", width: "160px", render: (k) => <Mono>{k.prefix.startsWith("ehk_") ? k.prefix : `ehk_${k.prefix}`}</Mono> },
    ...(auth.isAdmin ? [{ key: "user", header: "User", width: "120px", render: (k: ApiKeyOut) => k.username ?? String(k.userId) }] : []),
    { key: "created", header: "Created", width: "128px", render: (k) => <AbsTime value={k.createdAt} /> },
    { key: "used", header: "Last used", width: "112px", render: (k) => <RelTime value={k.lastUsedAt} empty="Never" /> },
    { key: "status", header: "Status", width: "176px", render: (k) => (k.active ? "Active" : <span className="muted">Revoked <AbsTime value={k.revokedAt} /></span>) },
    { key: "revoke", header: <span className="visually-hidden">Actions</span>, width: "80px", render: (k) => (k.active ? <button type="button" className="link" onClick={() => setRevoking(k)}>Revoke</button> : null) },
  ];

  return (
    <div className="stack">
      <div>
        <h2 className="section-title">API keys</h2>
        <p className="muted">Keys let the CLI, agents and MCP act as you.</p>
      </div>
      {noOwner ? (
        <Notice variant="info">
          {auth.authEnabled ? "There are no active users to hold a key." : "Authentication is off, so nothing needs a key yet, and the built-in local user cannot hold one."}{" "}
          <Link to="/settings/users" className="link">
            Add a user
          </Link>{" "}
          to create keys for it.
        </Notice>
      ) : (
      <form
        className="inline-form-row"
        onSubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <Field label="Name" htmlFor={nameId}>
          <TextInput id={nameId} value={name} onChange={(event) => setName(event.target.value)} placeholder="ci-bot laptop" maxLength={100} />
        </Field>
        {auth.isAdmin ? (
          <Field label="User" htmlFor={ownerId}>
            <Select id={ownerId} value={owner} onChange={(event) => setOwner(event.target.value)}>
              {owners.map((u) => (
                <option key={u.id} value={u.username}>
                  {u.username}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
        <div className="inline-form-buttons">
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Creating…"} disabled={auth.isAdmin && !owner}>
            Create key
          </Button>
        </div>
      </form>
      )}
      {error ? <Notice variant="error">{error}</Notice> : null}
      {created ? (
        <div className="notice notice-attention" role="status">
          <div className="notice-text stack-tight">
            <span className="notice-first">Copy this key now. It is shown only once.</span>
            <CopyField value={created.key} autoSelect label="New API key" />
            <span className="key-help">
              Use it with eks-harness login, or set EKS_HARNESS_API_KEY.{" "}
              <Button onClick={() => setCreated(null)}>Done</Button>
            </span>
          </div>
        </div>
      ) : null}
      {auth.isAdmin ? (
        <div className="toolbar page-tools">
          <Select value={filterUser} onChange={(event) => setParams({ user: event.target.value === "*" ? null : event.target.value })} aria-label="User">
            <option value="*">All users</option>
            {(users.data?.items ?? []).map((u) => (
              <option key={u.id} value={u.username}>
                {u.username}
              </option>
            ))}
          </Select>
        </div>
      ) : null}
      {queryState(keyList, "keys") ?? (
        <DataTable rows={items} columns={columns} getId={(k) => String(k.id)} label="API keys" empty={<EmptyState>You have no API keys. Create one to use the CLI, agents or MCP.</EmptyState>} />
      )}
      {revoking ? (
        <ConfirmDialog
          title={`Revoke the key ${revoking.name || "unnamed"} (${revoking.prefix.startsWith("ehk_") ? revoking.prefix : `ehk_${revoking.prefix}`})?`}
          body="Anything using it stops working immediately. This cannot be undone."
          confirmLabel="Revoke key"
          busyLabel={"Revoking…"}
          onClose={() => setRevoking(null)}
          errorFor={(err) => errorText(err, "revoke", "the key")}
          onConfirm={async () => {
            await api.delete(`/api/keys/${revoking.id}`);
            void client.invalidateQueries({ queryKey: ["keys"] });
            const mine = auth.me.keyPrefix && (revoking.prefix === auth.me.keyPrefix || `ehk_${revoking.prefix}` === auth.me.keyPrefix || revoking.prefix === `ehk_${auth.me.keyPrefix}`);
            if (mine && auth.me.via === "cookie") {
              await api.post("/api/auth/logout", {}, { handleUnauthorized: false }).catch(() => undefined);
              setCsrfToken(null);
              client.clear();
              void navigate({ to: "/login", search: { notice: "revoked" } as never });
            }
          }}
        />
      ) : null}
    </div>
  );
}

function UsersPage() {
  const auth = useAuth();
  const client = useQueryClient();
  const navigate = useNavigate();
  const users = useUsers();
  const allKeys = useQuery({ queryKey: keys.keys("*"), queryFn: () => fetchers.keys("*") });
  const allGrants = useGrants({});
  const [editing, setEditing] = useState<UserOut | "new" | null>(null);
  const [resetting, setResetting] = useState<UserOut | null>(null);
  const [confirm, setConfirm] = useState<{ mode: "disable" | "delete"; user: UserOut } | null>(null);
  const [error, setError] = useState("");
  const list = (users.data?.items ?? []).filter((u) => !u.builtin || !auth.authEnabled);
  const activeAdmins = list.filter((u) => u.role === "admin" && !u.disabled && !u.builtin);
  const keyCount = (u: UserOut) => (allKeys.data?.items ?? []).filter((k) => k.userId === u.id && k.active).length;
  const grantCount = (u: UserOut) => (allGrants.data?.items ?? []).filter((g) => g.userId === u.id).length;

  const setDisabled = async (user: UserOut, disabled: boolean) => {
    setError("");
    try {
      await api.patch(`/api/users/${user.id}`, { disabled });
      void client.invalidateQueries({ queryKey: keys.users });
    } catch (err) {
      setError(errorText(err, disabled ? "disable" : "enable", user.username));
      throw err;
    }
  };

  const menu = (user: UserOut): MenuItem[] => {
    const lastAdmin = user.role === "admin" && !user.disabled && activeAdmins.length <= 1;
    const items: MenuItem[] = [
      { label: "Edit…", onSelect: () => setEditing(user) },
      { label: "Reset password…", onSelect: () => setResetting(user) },
      { label: "Grants", onSelect: () => void navigate({ to: "/settings/grants", search: { user: user.username } as never }) },
      user.disabled
        ? { label: "Enable", onSelect: () => void setDisabled(user, false).catch(() => undefined) }
        : { label: "Disable…", disabled: lastAdmin, title: lastAdmin ? "At least one admin must remain." : undefined, onSelect: () => setConfirm({ mode: "disable", user }) },
      separator(),
      { label: "Delete…", danger: true, disabled: lastAdmin, title: lastAdmin ? "At least one admin must remain." : undefined, onSelect: () => setConfirm({ mode: "delete", user }) },
    ];
    return items;
  };

  const columns: Column<UserOut>[] = [
    { key: "username", header: "Username", render: (u) => u.username },
    { key: "role", header: "Role", width: "96px", render: (u) => (u.role === "admin" ? "Admin" : "Member") },
    { key: "status", header: "Status", width: "96px", render: (u) => (u.disabled ? <span className="strong">Disabled</span> : "Active") },
    { key: "created", header: "Created", width: "128px", render: (u) => <AbsTime value={u.createdAt} /> },
    { key: "keys", header: "Keys", align: "right", width: "64px", render: (u) => String(keyCount(u)) },
    { key: "grants", header: "Grants", align: "right", width: "72px", render: (u) => String(grantCount(u)) },
    { key: "more", header: <span className="visually-hidden">Actions</span>, width: "var(--h-control)", render: (u) => (u.builtin ? null : <OverflowMenu label={`More actions for ${u.username}`} items={menu(u)} />) },
  ];

  return (
    <div className="stack">
      <div className="section-head">
        <h2 className="section-title">Users</h2>
        <Button variant="primary" onClick={() => setEditing("new")}>
          Add user
        </Button>
      </div>
      {error ? <Notice variant="error">{error}</Notice> : null}
      {queryState(users, "users") ?? (
        <DataTable
          rows={list}
          columns={columns}
          getId={(u) => String(u.id)}
          label="Users"
          empty={<EmptyState action={<Button onClick={() => setEditing("new")}>Add user</Button>}>Only you. Add users to share this daemon.</EmptyState>}
        />
      )}
      {list.length === 1 && list[0].username === auth.username ? <p className="muted">Only you. Add users to share this daemon.</p> : null}
      {editing ? <UserDialog user={editing === "new" ? null : editing} onClose={() => setEditing(null)} /> : null}
      {resetting ? <UserDialog user={resetting} passwordOnly onClose={() => setResetting(null)} /> : null}
      {confirm?.mode === "disable" ? (
        <ConfirmDialog
          title={`Disable ${confirm.user.username}?`}
          body="Their keys and web sessions stop working until you enable them again."
          confirmLabel={`Disable ${confirm.user.username}`}
          busyLabel={"Disabling…"}
          onClose={() => setConfirm(null)}
          errorFor={(err) => errorText(err, "disable", confirm.user.username)}
          onConfirm={() => setDisabled(confirm.user, true)}
        />
      ) : null}
      {confirm?.mode === "delete" ? (
        <ConfirmDialog
          title={`Delete ${confirm.user.username}?`}
          body={`This deletes ${keyCount(confirm.user) === 1 ? "1 API key" : `${keyCount(confirm.user)} API keys`} and ${grantCount(confirm.user) === 1 ? "1 grant" : `${grantCount(confirm.user)} grants`}. Artifacts they created stay.`}
          confirmLabel={`Delete ${confirm.user.username}`}
          busyLabel={"Deleting…"}
          typeToConfirm={confirm.user.username}
          onClose={() => setConfirm(null)}
          errorFor={(err) => errorText(err, "delete", confirm.user.username)}
          onConfirm={async () => {
            await api.delete(`/api/users/${confirm.user.id}`);
            void client.invalidateQueries({ queryKey: keys.users });
            void client.invalidateQueries({ queryKey: ["keys"] });
            void client.invalidateQueries({ queryKey: ["grants"] });
          }}
        />
      ) : null}
    </div>
  );
}

function UserDialog({ user, onClose, passwordOnly }: { user: UserOut | null; onClose: () => void; passwordOnly?: boolean }) {
  const client = useQueryClient();
  const [username, setUsername] = useState(user?.username ?? "");
  const [role, setRole] = useState(user?.role ?? "member");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const userId = useId();
  const passId = useId();
  const confirmId = useId();
  const submit = async () => {
    const next: Record<string, string> = {};
    if (!user && !/^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$/.test(username)) next.username = "Use letters, digits, ., _, @ and -, starting with a letter or digit.";
    if (password && password.length < 8) next.password = "Use at least 8 characters.";
    if (passwordOnly && !password) next.password = "Enter a new password.";
    if (password !== confirm) next.confirm = "The passwords do not match.";
    setErrors(next);
    if (Object.keys(next).length) return;
    setBusy(true);
    setError("");
    try {
      if (user) {
        const body: Record<string, unknown> = {};
        if (!passwordOnly && role !== user.role) body.role = role;
        if (password) body.password = password;
        await api.patch(`/api/users/${user.id}`, body);
      } else {
        await api.post("/api/users", { username, role, password: password || null });
      }
      void client.invalidateQueries({ queryKey: keys.users });
      onClose();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 409) setErrors({ username: `A user ${username} already exists.` });
      else setError(errorText(err, user ? "save" : "add", user ? user.username : "the user"));
    }
  };
  return (
    <Dialog
      title={passwordOnly ? `Reset the password of ${user?.username}` : user ? `Edit ${user.username}` : "Add user"}
      onClose={onClose}
      wide
      busy={busy}
      onSubmit={() => void submit()}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Saving…"}>
            {passwordOnly ? "Reset password" : user ? "Save changes" : "Add user"}
          </Button>
        </>
      }
    >
      <div className="stack">
        {!passwordOnly ? (
          <>
            <Field label="Username" htmlFor={userId} error={errors.username}>
              {user ? <p id={userId}>{user.username}</p> : <TextInput id={userId} value={username} onChange={(e) => setUsername(e.target.value.trim())} invalid={Boolean(errors.username)} autoCapitalize="none" spellCheck={false} data-autofocus="" />}
            </Field>
            <fieldset className="fieldset">
              <legend className="field-label">Role</legend>
              <Radio name="role" label="Admin" checked={role === "admin"} onChange={() => setRole("admin")} helper="Admins can do everything, including settings and users." />
              <Radio name="role" label="Member" checked={role === "member"} onChange={() => setRole("member")} helper="Members see only projects and sessions they are granted." />
            </fieldset>
          </>
        ) : null}
        <Field label={passwordOnly ? "New password" : "Password (optional)"} htmlFor={passId} error={errors.password} helper={passwordOnly ? "At least 8 characters." : "Leave empty for key-only sign-in."}>
          <PasswordInput id={passId} value={password} onChange={(e) => setPassword(e.target.value)} invalid={Boolean(errors.password)} autoComplete="new-password" />
        </Field>
        <Field label="Confirm password" htmlFor={confirmId} error={errors.confirm}>
          <PasswordInput id={confirmId} value={confirm} onChange={(e) => setConfirm(e.target.value)} invalid={Boolean(errors.confirm)} autoComplete="new-password" />
        </Field>
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

function GrantsPage() {
  const params = useSearchParams();
  const setParams = useSetParams();
  const client = useQueryClient();
  const users = useUsers();
  const projects = useProjects();
  const grants = useGrants(params.user ? { user: params.user } : {});
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<GrantOut | null>(null);
  const rows = (grants.data?.items ?? []).filter((g) => !params.user || g.username === params.user);
  const columns: Column<GrantOut>[] = [
    { key: "user", header: "User", render: (g) => g.username ?? String(g.userId) },
    { key: "project", header: "Project", render: (g) => g.projectId },
    { key: "scope", header: "Scope", render: (g) => (g.sessionId ? g.sessionName ?? g.sessionSlug : "All sessions") },
    { key: "level", header: "Level", width: "96px", render: (g) => (g.level === "editor" ? "Editor" : "Viewer") },
    { key: "granted", header: "Granted", width: "128px", render: (g) => <AbsTime value={g.createdAt} /> },
    { key: "remove", header: <span className="visually-hidden">Actions</span>, width: "96px", render: (g) => <button type="button" className="link" onClick={() => setRemoving(g)}>Remove</button> },
  ];
  return (
    <div className="stack">
      <div className="section-head">
        <h2 className="section-title">Grants</h2>
        {!adding ? (
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add grant
          </Button>
        ) : null}
      </div>
      <div className="toolbar page-tools">
        <Select value={params.user ?? ""} onChange={(event) => setParams({ user: event.target.value || null })} aria-label="User">
          <option value="">All users</option>
          {(users.data?.items ?? []).filter((u) => u.role !== "admin").map((u) => (
            <option key={u.id} value={u.username}>
              {u.username}
            </option>
          ))}
        </Select>
      </div>
      {adding ? (
        <GrantForm
          users={(users.data?.items ?? []).filter((u) => u.role !== "admin" && !u.builtin).map((u) => ({ value: u.username, label: u.username }))}
          projects={(projects.data?.items ?? []).map((p) => ({ value: p.id, label: p.id }))}
          fixedUser={params.user || undefined}
          onDone={() => {
            setAdding(false);
            void client.invalidateQueries({ queryKey: ["grants"] });
          }}
          onCancel={() => setAdding(false)}
        />
      ) : null}
      {queryState(grants, "grants") ?? (
        <DataTable
          rows={rows}
          columns={columns}
          getId={(g) => String(g.id)}
          label="Grants"
          empty={<EmptyState action={!adding ? <Button onClick={() => setAdding(true)}>Add grant</Button> : undefined}>No grants. Members see nothing until you grant them a project or session.</EmptyState>}
        />
      )}
      {removing ? <RemoveGrantConfirm grant={removing} onClose={() => setRemoving(null)} /> : null}
    </div>
  );
}
