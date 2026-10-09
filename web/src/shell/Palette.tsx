import { useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { useProjects, useSharedSessions } from "../api/queries";
import { useNodes, usePluginUi } from "../api/system";
import { useFocusTrap } from "../components/Dialog";
import { setTheme } from "../lib/theme";
import { useCurrentProject } from "./project";

export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  mono?: boolean;
  run: () => void;
}

function fold(text: string): string {
  return text.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

function score(command: Command, query: string): number {
  if (!query) return 1;
  const label = fold(command.label);
  const hint = fold(command.hint ?? "");
  const q = fold(query);
  if (label.startsWith(q)) return 3;
  if (label.includes(q)) return 2;
  if (hint.includes(q)) return 1;
  const parts = q.split(/\s+/).filter(Boolean);
  return parts.every((part) => label.includes(part) || hint.includes(part)) ? 0.5 : 0;
}

export function CommandPalette({ onClose, extra }: { onClose: () => void; extra: Command[] }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const sessions = useSharedSessions();
  const projects = useProjects();
  const nodes = useNodes();
  const [project, setProject] = useCurrentProject();
  const ui = usePluginUi(project);
  useFocusTrap(ref, true);
  useEffect(() => input.current?.focus(), []);

  const go = (to: string) => () => {
    onClose();
    void navigate({ to });
  };

  const commands = useMemo<Command[]>(() => {
    const out: Command[] = [
      { id: "nav-now", group: "Go to", label: "Now", hint: "g n", run: go("/") },
      { id: "nav-lab", group: "Go to", label: "Lab", hint: "g l", run: go("/lab") },
      { id: "nav-evidence", group: "Go to", label: "Evidence", hint: "g e", run: go("/evidence") },
      { id: "nav-studio", group: "Go to", label: "Studio", hint: "g s", run: go("/studio") },
      { id: "nav-nodes", group: "Go to", label: "Nodes", hint: "g o", run: go("/nodes") },
      { id: "nav-plugins", group: "Go to", label: "Plugins", hint: "g p", run: go("/plugins") },
      { id: "nav-settings", group: "Go to", label: "Settings", hint: "g ,", run: go("/settings/server") },
      { id: "nav-devices", group: "Go to", label: "Devices", hint: "Lab", run: go("/devices") },
      { id: "nav-browsers", group: "Go to", label: "Browser profiles", hint: "Lab", run: go("/browsers") },
      { id: "nav-backends", group: "Go to", label: "Backends", hint: "Lab", run: go("/backends") },
      { id: "nav-search", group: "Go to", label: "Search artifacts", hint: "/", run: go("/search") },
      { id: "nav-cleanup", group: "Go to", label: "Clean up old data", hint: "Evidence", run: go("/cleanup") },
      { id: "theme-light", group: "Theme", label: "Theme: light", run: () => { setTheme("light"); onClose(); } },
      { id: "theme-dark", group: "Theme", label: "Theme: dark", run: () => { setTheme("dark"); onClose(); } },
      { id: "theme-system", group: "Theme", label: "Theme: system", run: () => { setTheme("system"); onClose(); } },
    ];
    for (const session of sessions.data?.items ?? []) {
      out.push({ id: `session-${session.slug}`, group: "Sessions", label: session.name, mono: true, hint: `${session.artifactCount} artifacts`, run: go(`/sessions/${encodeURIComponent(session.slug)}`) });
    }
    for (const item of projects.data?.items ?? []) {
      out.push({ id: `project-${item.id}`, group: "Projects", label: item.id, mono: true, hint: item.id === project ? "current" : "make current", run: () => { setProject(item.id); onClose(); void navigate({ to: `/p/${item.id}` }); } });
    }
    for (const node of nodes.data?.items ?? []) {
      out.push({ id: `node-${node.id}`, group: "Nodes", label: node.id, mono: true, hint: node.state, run: go(`/nodes/${encodeURIComponent(node.id)}`) });
    }
    for (const module of ui.data?.items ?? []) {
      if (module.slot === "command") out.push({ id: `plugin-${module.plugin}-${module.id}`, group: "Plugins", label: String(module.title ?? module.id), hint: module.pluginName, run: () => { onClose(); window.dispatchEvent(new CustomEvent("ehx:plugin-command", { detail: module })); } });
      if (module.slot === "route") out.push({ id: `plugin-route-${module.plugin}-${module.id}`, group: "Plugins", label: String(module.title ?? module.id), hint: module.pluginName, run: go(`/x/${encodeURIComponent(module.plugin)}/${encodeURIComponent(module.id)}`) });
    }
    return [...extra, ...out];
  }, [sessions.data, projects.data, nodes.data, ui.data, project, extra]);

  const matches = useMemo(() => {
    const scored = commands.map((command) => ({ command, value: score(command, query.trim()) })).filter((entry) => entry.value > 0);
    if (query.trim()) scored.sort((a, b) => b.value - a.value);
    return scored.slice(0, 60).map((entry) => entry.command);
  }, [commands, query]);

  useEffect(() => setActive(0), [query]);

  const groups: { name: string; items: { command: Command; index: number }[] }[] = [];
  matches.forEach((command, index) => {
    const group = groups.find((g) => g.name === command.group);
    if (group) group.items.push({ command, index });
    else groups.push({ name: command.group, items: [{ command, index }] });
  });

  return (
    <div className="palette-scrim" data-overlay-open="" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="palette" ref={ref} role="dialog" aria-modal="true" aria-label="Find or run">
        <input
          ref={input}
          className="palette-input"
          placeholder="Find a session, project, node or command"
          value={query}
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={matches[active] ? `palette-${matches[active].id}` : undefined}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              onClose();
            } else if (event.key === "ArrowDown") {
              event.preventDefault();
              setActive((value) => Math.min(matches.length - 1, value + 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setActive((value) => Math.max(0, value - 1));
            } else if (event.key === "Enter") {
              event.preventDefault();
              matches[active]?.run();
            }
          }}
        />
        <ul className="palette-list" id="palette-list" role="listbox" aria-label="Results">
          {groups.map((group) => (
            <li key={group.name} role="presentation">
              <div className="palette-group">{group.name}</div>
              <ul className="palette-list" role="presentation">
                {group.items.map(({ command, index }) => (
                  <li
                    key={command.id}
                    id={`palette-${command.id}`}
                    role="option"
                    aria-selected={index === active}
                    className="palette-item"
                    onMouseEnter={() => setActive(index)}
                    onClick={() => command.run()}
                  >
                    <span className={command.mono ? "palette-item-mono ellipsis" : "ellipsis"}>{command.label}</span>
                    {command.hint ? <span className="palette-item-hint">{command.hint}</span> : null}
                  </li>
                ))}
              </ul>
            </li>
          ))}
          {!matches.length ? <li className="palette-empty">Nothing matches {query.trim()}.</li> : null}
        </ul>
      </div>
    </div>
  );
}
