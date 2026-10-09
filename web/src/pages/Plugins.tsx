import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, encodeSegment } from "../api/client";
import { systemKeys, usePlugin, usePlugins, type PluginOut } from "../api/system";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Form";
import { DefList, MetaItem, Mono, PageHeader, queryState, Section } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { Rack, StateLine, type Tone } from "../components/Workbench";
import { useAuth } from "../lib/auth";
import { plural } from "../lib/format";
import { useTitle } from "../lib/hooks";
import { hrefWith, useSearchParams } from "../lib/url";
import { useCurrentProject } from "../shell/project";
import { PluginSlot } from "../shell/plugins";

const STATE_WORDS: Record<PluginOut["state"], [string, Tone]> = {
  active: ["active", "ok"],
  pending: ["waiting for approval", "wait"],
  changed: ["changed since approved", "fail"],
  disabled: ["disabled", "idle"],
  error: ["error", "fail"],
  shadowed: ["overridden", "idle"],
};

const KIND_WORDS: Record<PluginOut["kind"], string> = { builtin: "built in", package: "package", path: "folder", repo: "repository", git: "git" };

function contributes(plugin: PluginOut): string {
  return Object.entries(plugin.contributes ?? {})
    .map(([type, count]) => (count > 1 ? `${count} ${type.replace(/_/g, " ")}` : type.replace(/_/g, " ")))
    .join(", ");
}

function pluginHref(plugin: PluginOut, tree: string | null): string {
  return hrefWith(`/plugins/${encodeURIComponent(plugin.id)}`, tree ? { tree } : {});
}

export function PluginsPage() {
  useTitle("Plugins");
  const params = useSearchParams();
  const [project] = useCurrentProject();
  const tree = params.tree ?? null;
  const plugins = usePlugins(tree);
  const items = (plugins.data?.items ?? []).filter((p) => p.state !== "shadowed" || params.all);
  const waiting = items.filter((p) => p.state === "pending" || p.state === "changed");
  const order: PluginOut["state"][] = ["pending", "changed", "error", "active", "disabled", "shadowed"];
  const sorted = [...items].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state) || a.id.localeCompare(b.id));
  return (
    <div className="page">
      <PageHeader
        title="Plugins"
        meta={
          plugins.data ? (
            <>
              <MetaItem>{plural(items.length, "plugin")}</MetaItem>
              <MetaItem>{items.filter((p) => p.state === "active").length} active</MetaItem>
              {waiting.length ? <MetaItem><span className="danger strong">{waiting.length} waiting for approval</span></MetaItem> : null}
              {plugins.data.tree ? <MetaItem label="Tree"><Mono>{plugins.data.tree}</Mono></MetaItem> : <MetaItem>{project ? `hub plugins; repo plugins appear for a work tree` : "hub plugins"}</MetaItem>}
            </>
          ) : null
        }
      />
      {waiting.length ? (
        <Notice variant="attention" title="Approval needed">
          Folder and git plugins run code in the hub. Review {waiting.map((p) => p.id).join(", ")} and approve them, or run <span className="mono">eks-harness plugin trust &lt;id&gt;</span>.
        </Notice>
      ) : null}
      <Section title="Installed" count={plugins.data ? items.length : null}>
        {queryState(plugins, "plugins") ?? (
          <Rack
            rows={sorted}
            rowKey={(p) => `${p.id}-${p.origin}`}
            rowTo={(p) => pluginHref(p, tree)}
            label="Plugins"
            columns={[
              { key: "id", label: "Plugin", width: "minmax(160px, 0.9fr)", render: (p) => <Mono>{p.id}</Mono> },
              { key: "state", label: "State", width: "minmax(150px, 0.8fr)", render: (p) => <StateLine state={STATE_WORDS[p.state][0]} tone={STATE_WORDS[p.state][1]} /> },
              { key: "source", label: "Source", width: "96px", render: (p) => KIND_WORDS[p.kind] },
              { key: "version", label: "Version", width: "72px", render: (p) => <span className="num">{p.version ?? ""}</span> },
              { key: "what", label: "Contributes", width: "minmax(0, 2fr)", render: (p) => <span className="ink-2">{p.error ?? contributes(p)}</span> },
            ]}
            empty={<EmptyState>No plugins found.</EmptyState>}
          />
        )}
      </Section>
      <PluginSlot slot="settings.page" />
    </div>
  );
}

export function PluginPage({ id }: { id: string }) {
  const params = useSearchParams();
  const tree = params.tree ?? null;
  const plugin = usePlugin(id, tree);
  const auth = useAuth();
  const client = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  useTitle(`${id} - Plugins`);
  const state = queryState(plugin, "plugin", { notFound: <EmptyState action={<Link to="/plugins" className="link">Plugins</Link>}>There is no plugin {id}.</EmptyState> });
  if (state) return <div className="page">{state}</div>;
  const p = plugin.data as PluginOut;
  const [word, tone] = STATE_WORDS[p.state];
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["plugins"] });
    void client.invalidateQueries({ queryKey: ["plugin"] });
    void client.invalidateQueries({ queryKey: systemKeys.pluginUi(null).slice(0, 1) });
  };
  const trust = async (revoke: boolean) => {
    try {
      if (revoke) await api.delete(`/api/plugins/${encodeSegment(p.id)}/trust`, { query: { tree: tree ?? undefined } });
      else await api.post(`/api/plugins/${encodeSegment(p.id)}/trust`, { tree });
      setMessage(revoke ? "Approval withdrawn." : "Approved; the plugin is active.");
      refresh();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : String(error));
    }
  };
  const save = async () => {
    try {
      const body: Record<string, unknown> = {};
      for (const spec of p.settings ?? []) {
        if (!(spec.key in values)) continue;
        const raw = values[spec.key];
        body[spec.key] = spec.type === "int" ? Number.parseInt(raw, 10) : spec.type === "float" ? Number.parseFloat(raw) : spec.type === "bool" ? raw === "true" : spec.type === "list[str]" ? raw.split(",").map((s) => s.trim()).filter(Boolean) : raw;
      }
      await api.put(`/api/plugins/${encodeSegment(p.id)}/settings`, { values: body });
      setMessage("Settings saved.");
      setValues({});
      refresh();
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : String(error));
    }
  };
  const canTrust = auth.isAdmin && (p.kind === "path" || p.kind === "repo" || p.kind === "git");
  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: "Plugins", to: "/plugins" }]}
        title={p.id}
        mono
        actions={
          canTrust ? (
            <div className="action-row">
              {p.state === "pending" || p.state === "changed" ? (
                <Button variant="primary" onClick={() => void trust(false)}>
                  Approve this version
                </Button>
              ) : p.state === "active" ? (
                <Button onClick={() => void trust(true)}>Withdraw approval</Button>
              ) : null}
            </div>
          ) : null
        }
        meta={
          <>
            <MetaItem>
              <StateLine state={word} tone={tone} detail={p.reason && p.reason !== word ? p.reason : null} />
            </MetaItem>
            {p.name && p.name !== p.id ? <MetaItem>{p.name}</MetaItem> : null}
            {p.version ? <MetaItem>version {p.version}</MetaItem> : null}
            <MetaItem>{KIND_WORDS[p.kind]}</MetaItem>
          </>
        }
      />
      {message ? <p className="result-text" role="status">{message}</p> : null}
      {p.description ? <p className="lede">{p.description}</p> : null}
      <div className="desk">
        <div className="desk-main">
          <Section title="Contributions" count={p.contributions?.length ?? 0}>
            <Rack
              rows={p.contributions ?? []}
              rowKey={(c) => `${c.type}-${c.id}`}
              label="Contributions"
              columns={[
                { key: "type", label: "Type", width: "minmax(0, 0.6fr)", render: (c) => c.type.replace(/_/g, " ") },
                { key: "id", label: "Id", width: "minmax(0, 0.8fr)", render: (c) => <Mono>{c.id}</Mono> },
                { key: "entry", label: "Entry, module or path", width: "minmax(0, 1.6fr)", render: (c) => <Mono>{String(c.entry ?? c.module ?? c.path ?? "")}</Mono> },
              ]}
              empty={<span>This plugin contributes nothing yet.</span>}
            />
          </Section>
          {p.settings?.length ? (
            <Section title="Settings" count={p.settings.length}>
              <form
                className="settings-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void save();
                }}
              >
                {p.settings.map((spec) => {
                  const current = values[spec.key] ?? (spec.type === "secret" ? "" : stringify(p.values?.[spec.key] ?? spec.default));
                  return (
                    <Field key={spec.key} label={spec.key} htmlFor={`setting-${spec.key}`} helper={spec.description || undefined} aside={<span className="code-chip">{spec.type}</span>}>
                      <TextInput
                        id={`setting-${spec.key}`}
                        type={spec.type === "secret" ? "password" : "text"}
                        mono={spec.type === "path" || spec.type === "list[str]"}
                        value={current}
                        placeholder={spec.type === "secret" ? "unchanged" : undefined}
                        disabled={!auth.isAdmin}
                        onChange={(event) => setValues({ ...values, [spec.key]: event.target.value })}
                      />
                    </Field>
                  );
                })}
                {auth.isAdmin ? (
                  <div className="button-row">
                    <Button variant="primary" type="submit" disabled={!Object.keys(values).length}>
                      Save settings
                    </Button>
                  </div>
                ) : null}
              </form>
            </Section>
          ) : null}
        </div>
        <aside className="desk-side">
          <Section title="Source">
            <DefList
              compact
              items={[
                { label: "Origin", value: <Mono>{p.origin}</Mono> },
                p.root ? { label: "Folder", value: <Mono>{p.root}</Mono>, copy: p.root } : null,
                p.hash ? { label: "Content hash", value: <Mono title={p.hash}>{p.hash.slice(0, 23)}</Mono>, copy: p.hash } : null,
                p.tree ? { label: "Work tree", value: <Mono>{p.tree}</Mono> } : null,
                { label: "API", value: p.api ?? "1" },
                { label: "Enabled by default", value: p.enabledByDefault === false ? "no, a project enables it" : "yes" },
              ]}
            />
          </Section>
        </aside>
      </div>
    </div>
  );
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

export function PluginRoutePage({ plugin, id }: { plugin: string; id: string }) {
  useTitle(id);
  return (
    <div className="page">
      <PluginSlot slot="route" />
      <p className="ink-3">
        Page <Mono>{id}</Mono> from <Mono>{plugin}</Mono>
      </p>
    </div>
  );
}
