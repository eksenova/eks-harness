import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { studio, useStudioConnection, useStudioEvent, useStudioRequest, type CatalogEntry, type MediaItem, type StudioJob, type StudioProject, type StudioRender, type StudioTimeline } from "../api/studio";
import { useScores, type ScoreFile } from "../api/system";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Form";
import { DefList, MetaItem, Mono, PageHeader, Section, Tabs } from "../components/Misc";
import { EmptyState, ErrorState, Loading, Notice } from "../components/Notice";
import { Rack, RunBlock, Timecode, toneFor } from "../components/Workbench";
import { formatSeconds, formatSize, plural } from "../lib/format";
import { useKeydown, useTitle, shortcutAllowed } from "../lib/hooks";
import { hrefWith, useSearchParams, useSetParams } from "../lib/url";
import { Sheet, type SheetSelection, type SheetTrack } from "../sheet/Sheet";
import { useCurrentProject } from "../shell/project";

const TREE_KEY = "ehx.studio.tree";

function readTree(): string {
  try {
    return window.localStorage.getItem(TREE_KEY) ?? "";
  } catch {
    return "";
  }
}

function useProjectTree(project: string | null) {
  return useQuery({
    queryKey: ["project-tree", project ?? ""],
    queryFn: () => api.get<{ tree: string | null }>("/api/plugins", { query: { project: project ?? undefined } }),
    enabled: Boolean(project),
    staleTime: 60_000,
  });
}

function useStudioJobs(): Record<string, StudioJob> {
  const [jobs, setJobs] = useState<Record<string, StudioJob>>({});
  useStudioConnection(["jobs"]);
  useStudioEvent((type, payload) => {
    if (!type.startsWith("job.") || type === "job.event" || type === "job.preview_frame") return;
    const job = payload as StudioJob;
    if (job?.job_id) setJobs((current) => ({ ...current, [job.job_id]: job }));
  });
  return jobs;
}

function StudioBlocked({ status }: { status: string }) {
  if (status === "denied") return <Notice variant="attention" title="The studio needs an admin login">Studio projects are programs the hub runs, so only admins can open them.</Notice>;
  if (status === "closed") return <Notice variant="attention">The studio channel is reconnecting.</Notice>;
  return null;
}

export function StudioPage() {
  useTitle("Studio");
  const [project] = useCurrentProject();
  const projectTree = useProjectTree(project);
  const [treeText, setTreeText] = useState(readTree);
  const tree = treeText.trim() || projectTree.data?.tree || "";
  const scores = useScores(tree || null);
  const status = useStudioConnection(["jobs", "system"]);
  const projects = useStudioRequest<{ projects: StudioProject[] }>("projects.list", { query: "" });
  const jobs = useStudioJobs();
  const [system, setSystem] = useState<{ cpu_pct: number; mem_pct: number; gpu_pct?: number | null; render_pct?: number | null } | null>(null);
  useStudioEvent((type, payload) => {
    if (type === "system.snapshot") setSystem(payload as typeof system);
  });
  const hello = studio.hello;
  const active = Object.values(jobs).sort((a, b) => b.started_at - a.started_at);
  return (
    <div className="page">
      <PageHeader
        title="Studio"
        meta={
          <>
            <MetaItem>{projects.data ? plural(projects.data.projects.length, "video project") : "video projects"}</MetaItem>
            <MetaItem>{scores.data ? plural(scores.data.items.length, "score") : "scores"}</MetaItem>
            {system ? (
              <MetaItem>
                CPU <span className="num">{Math.round(system.cpu_pct)}%</span>, memory <span className="num">{Math.round(system.mem_pct)}%</span>
                {system.gpu_pct !== null && system.gpu_pct !== undefined ? <>, GPU <span className="num">{Math.round(system.gpu_pct)}%</span></> : null}
              </MetaItem>
            ) : null}
          </>
        }
      />
      <StudioBlocked status={status} />
      <div className="desk">
        <div className="desk-main">
          <Section title="Scores" count={scores.data ? scores.data.items.length : null}>
            <form className="toolbar studio-tree" onSubmit={(event) => event.preventDefault()}>
              <Field label="Work tree" htmlFor="studio-tree" helper={projectTree.data?.tree && !treeText ? `From the current project ${project}` : undefined}>
                <TextInput
                  id="studio-tree"
                  mono
                  value={treeText || projectTree.data?.tree || ""}
                  placeholder="/path/to/repo with .harness/scores"
                  onChange={(event) => {
                    setTreeText(event.target.value);
                    try {
                      window.localStorage.setItem(TREE_KEY, event.target.value);
                    } catch {
                      return;
                    }
                  }}
                />
              </Field>
            </form>
            {!tree ? (
              <EmptyState>Pick a work tree, or choose a current project whose repo the hub knows.</EmptyState>
            ) : scores.isError ? (
              <ErrorState message={scores.error instanceof Error ? scores.error.message : "Could not list scores."} onRetry={() => void scores.refetch()} />
            ) : scores.data ? (
              <Rack<ScoreFile>
                rows={scores.data.items}
                rowKey={(s) => s.path}
                rowTo={(s) => hrefWith("/studio/score", { path: s.path })}
                label="Scores"
                columns={[
                  { key: "name", label: "Score", width: "minmax(0, 0.8fr)", render: (s) => <span className="strong">{s.name}</span> },
                  { key: "path", label: "File", width: "minmax(0, 1.6fr)", render: (s) => <Mono>{s.relative}</Mono> },
                  { key: "when", label: "Changed", width: "120px", align: "end", render: (s) => <span className="num ink-2">{new Date(s.modified * 1000).toLocaleDateString("en-GB")}</span> },
                ]}
                empty={<span>No scores. Create one with <span className="mono">eks-harness score new .harness/scores/launch/score.py</span>.</span>}
              />
            ) : (
              <Loading what="scores" />
            )}
          </Section>
          <Section title="Video projects" count={projects.data ? projects.data.projects.length : null}>
            {projects.error ? (
              <ErrorState message={projects.error} onRetry={projects.reload} />
            ) : projects.data ? (
              <Rack<StudioProject>
                rows={projects.data.projects}
                rowKey={(p) => p.id}
                rowTo={(p) => hrefWith("/studio/edit", { project: p.id })}
                label="Video projects"
                columns={[
                  { key: "name", label: "Project", width: "minmax(0, 0.8fr)", render: (p) => <span className="strong">{p.name}</span> },
                  { key: "path", label: "Folder", width: "minmax(0, 1.6fr)", render: (p) => <Mono>{p.path}</Mono> },
                ]}
                empty={<span>No video projects in {hello?.project_workspace ? <Mono>{hello.project_workspace}</Mono> : "the workspace"}.</span>}
              />
            ) : (
              <Loading what="video projects" />
            )}
          </Section>
        </div>
        <aside className="desk-side">
          <Section title="Renders" count={active.length}>
            {active.length ? (
              <div className="run-list">
                {active.map((job) => (
                  <StudioJobBlock key={job.job_id} job={job} />
                ))}
              </div>
            ) : (
              <p className="ink-2 empty-line">No renders since this page opened.</p>
            )}
          </Section>
          <Section title="Workspace">
            <DefList
              compact
              items={[
                { label: "Projects", value: hello?.project_workspace ? <Mono>{hello.project_workspace}</Mono> : "unknown" },
                { label: "Media library", value: hello?.media_library_root ? <Mono>{hello.media_library_root}</Mono> : "not set" },
                { label: "Engine", value: hello?.version ? <Mono>{hello.version}</Mono> : "" },
              ]}
            />
          </Section>
        </aside>
      </div>
    </div>
  );
}

function StudioJobBlock({ job }: { job: StudioJob }) {
  const done = job.status === "succeeded";
  return (
    <RunBlock
      title={`${job.mode} ${job.project_id}`}
      state={done ? "done" : job.status}
      tone={done ? "ok" : toneFor(job.status)}
      progress={job.status === "running" ? job.progress : null}
      facts={[
        job.frame_total ? <span key="f" className="num">frame {job.frame_index} of {job.frame_total}</span> : null,
        job.current_step ? <span key="s">{job.current_step}</span> : null,
        job.eta_s ? <span key="e" className="num">{formatSeconds(job.eta_s)} left</span> : null,
      ].filter(Boolean) as React.ReactNode[]}
    >
      {job.error ? <pre className="danger">{job.error}</pre> : job.message ? <p>{job.message}</p> : null}
    </RunBlock>
  );
}

function beatsFrom(timeline: StudioTimeline | null): { beats: number[]; downbeats: number[] } {
  if (!timeline) return { beats: [], downbeats: [] };
  for (const marker of Object.values(timeline.markers ?? {})) {
    if (marker.streams?.beat?.length) return { beats: marker.streams.beat, downbeats: marker.streams.downbeat ?? [] };
  }
  return { beats: [], downbeats: [] };
}

export function StudioEditPage() {
  const params = useSearchParams();
  const setParams = useSetParams();
  const projectId = params.project ?? "";
  useTitle(`${projectId} - Studio`);
  const status = useStudioConnection(["jobs", "catalog", "media"]);
  const timeline = useStudioRequest<StudioTimeline>("project.timeline", projectId ? { project_id: projectId } : null);
  const renders = useStudioRequest<{ artifacts: StudioRender[] }>("artifacts.list", projectId ? { project_id: projectId } : null);
  const effects = useStudioRequest<{ entries: CatalogEntry[] }>("catalog.request", { kind: "effect" });
  const media = useStudioRequest<{ entries: MediaItem[] }>("media.list", projectId ? { scope: "project", project_id: projectId } : null);
  const jobs = useStudioJobs();
  const [position, setPosition] = useState(0);
  const [selection, setSelection] = useState<SheetSelection | null>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const tab = params.panel ?? "inspector";

  const data = timeline.data;
  const fps = data?.fps ?? 30;
  const { beats, downbeats } = beatsFrom(data);
  const tracks: SheetTrack[] = useMemo(() => {
    if (!data) return [];
    const video = [...data.tracks].sort((a, b) => b.z - a.z).map((track, index) => ({
      id: `v-${track.name}`,
      code: `E${data.tracks.length - index}`,
      kind: "edit" as const,
      name: track.name,
      meta: `z ${track.z}, ${plural(track.segments.length, "clip")}`,
      clips: track.segments.filter((s) => s.start !== null && s.end !== null).map((s) => ({ id: s.id, start: s.start as number, end: s.end as number, label: s.id, detail: [s.media, ...s.effects.map((e) => String(e.name ?? e.kind ?? ""))].filter(Boolean).join(", ") })),
    }));
    const audio = data.audio_tracks.map((track, index) => ({
      id: `a-${track.name}`,
      code: `A${index + 1}`,
      kind: "audio" as const,
      name: track.name,
      meta: plural(track.segments.length, "clip"),
      clips: track.segments.filter((s) => s.start !== null && s.end !== null).map((s) => ({ id: s.id, start: s.start as number, end: s.end as number, label: s.media ?? s.id })),
    }));
    return [...video, ...audio];
  }, [data]);
  const latest = (renders.data?.artifacts ?? []).find((r) => r.status === "succeeded" && r.output_url);
  const selected = selection?.clip ? tracks.find((t) => t.id === selection.track)?.clips.find((c) => c.id === selection.clip) : null;
  const projectJobs = Object.values(jobs).filter((j) => j.project_id === projectId).sort((a, b) => b.started_at - a.started_at);

  useEffect(() => {
    if (Object.values(jobs).some((j) => j.project_id === projectId && j.status === "succeeded")) renders.reload();
  }, [jobs]);

  useEffect(() => {
    const element = video.current;
    if (!element || playing) return;
    if (Math.abs(element.currentTime - position) > 0.5 / fps) element.currentTime = position;
  }, [position, playing, fps]);

  const toggle = () => {
    const element = video.current;
    if (!element) {
      setPlaying(false);
      return;
    }
    if (element.paused) void element.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
    else {
      element.pause();
      setPlaying(false);
    }
  };

  useKeydown((event) => {
    if (!shortcutAllowed(event)) return;
    if (event.key === " ") {
      event.preventDefault();
      toggle();
    } else if (event.key === "k") {
      video.current?.pause();
      setPlaying(false);
    } else if (event.key === "l") toggle();
    else if (event.key === "j") setPosition((p) => Math.max(0, p - 1));
  });

  const startRender = async (mode: "preview" | "final") => {
    setError(null);
    try {
      await studio.send("render.start", { project_id: projectId, mode });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (!projectId) return <div className="page"><EmptyState action={<Link to="/studio" className="link">Studio</Link>}>Pick a video project.</EmptyState></div>;
  return (
    <div className="page page--studio">
      <PageHeader
        crumbs={[{ label: "Studio", to: "/studio" }]}
        title={projectId}
        mono
        actions={
          <div className="action-row">
            <Button onClick={() => void startRender("preview")}>Render preview</Button>
            <Button variant="primary" onClick={() => void startRender("final")}>
              Render final
            </Button>
          </div>
        }
        meta={
          data ? (
            <>
              <MetaItem>
                {data.resolution[0]}x{data.resolution[1]} at {data.fps} fps
              </MetaItem>
              <MetaItem>
                <Timecode seconds={data.duration} fps={data.fps} />
              </MetaItem>
              <MetaItem>{plural(data.tracks.reduce((sum, t) => sum + t.segments.length, 0), "clip")}</MetaItem>
              {beats.length ? <MetaItem>{plural(beats.length, "beat")}</MetaItem> : null}
            </>
          ) : null
        }
      />
      <StudioBlocked status={status} />
      {error ? <Notice variant="error">{error}</Notice> : null}
      {timeline.error ? <ErrorState message={timeline.error} onRetry={timeline.reload} /> : null}
      <div className="studio-grid">
        <div className="studio-stage">
          {latest?.output_url ? (
            <video ref={video} className="studio-video" src={latest.output_url} playsInline preload="metadata" onTimeUpdate={(event) => playing && setPosition(event.currentTarget.currentTime)} onEnded={() => setPlaying(false)} />
          ) : (
            <div className="studio-empty-stage">
              <p>No render yet.</p>
              <Button onClick={() => void startRender("preview")}>Render a preview</Button>
            </div>
          )}
          <div className="studio-transport">
            <button type="button" className="sheet-tool" onClick={() => setPosition(0)} aria-label="Go to start">
              <span className="mono">|&lt;</span>
            </button>
            <button type="button" className="sheet-tool" onClick={toggle} disabled={!latest} aria-label={playing ? "Pause" : "Play"}>
              {playing ? "Pause" : "Play"}
            </button>
            <span className="ink-3">{latest ? `${latest.mode} render ${latest.finished_at ? new Date(latest.finished_at * 1000).toLocaleTimeString("en-GB") : ""}` : "renders appear here"}</span>
          </div>
        </div>
        <aside className="studio-inspector">
          <Tabs
            label="Panels"
            sub
            items={[
              { label: "Inspector", to: "/studio/edit", search: { project: projectId }, active: tab === "inspector" },
              { label: "Renders", to: "/studio/edit", search: { project: projectId, panel: "renders" }, active: tab === "renders" },
              { label: "Effects", to: "/studio/edit", search: { project: projectId, panel: "effects" }, active: tab === "effects" },
              { label: "Media", to: "/studio/edit", search: { project: projectId, panel: "media" }, active: tab === "media" },
            ]}
          />
          {tab === "inspector" ? (
            selected ? (
              <DefList
                compact
                items={[
                  { label: "Clip", value: <Mono>{selected.id}</Mono> },
                  { label: "Track", value: selection?.track.replace(/^[va]-/, "") ?? "" },
                  { label: "In", value: <Timecode seconds={selected.start} fps={fps} /> },
                  { label: "Out", value: <Timecode seconds={selected.end} fps={fps} /> },
                  { label: "Length", value: <span className="num">{(selected.end - selected.start).toFixed(2)} s</span> },
                  selected.detail ? { label: "Media and effects", value: selected.detail } : null,
                ]}
              />
            ) : (
              <p className="ink-2">Select a clip on the timeline. Edits go through project.py or the MCP tools video_add_segment, video_add_effect and video_set_property.</p>
            )
          ) : null}
          {tab === "renders" ? (
            <div className="run-list">
              {projectJobs.map((job) => (
                <StudioJobBlock key={job.job_id} job={job} />
              ))}
              {(renders.data?.artifacts ?? []).map((render) => (
                <RunBlock
                  key={render.job_id}
                  title={`${render.mode} ${render.job_id.slice(0, 8)}`}
                  state={render.status === "succeeded" ? "done" : render.status}
                  facts={[render.duration_s ? <span key="d" className="num">{formatSeconds(render.duration_s)}</span> : null, render.total_frames ? <span key="f" className="num">{render.total_frames} frames</span> : null].filter(Boolean) as React.ReactNode[]}
                >
                  {render.output_url ? (
                    <a className="link" href={render.output_url}>
                      Open the render
                    </a>
                  ) : null}
                </RunBlock>
              ))}
              {!projectJobs.length && !(renders.data?.artifacts ?? []).length ? <p className="ink-2 empty-line">No renders yet.</p> : null}
            </div>
          ) : null}
          {tab === "effects" ? <EffectCatalog entries={effects.data?.entries ?? []} /> : null}
          {tab === "media" ? (
            <Rack<MediaItem>
              rows={media.data?.entries ?? []}
              rowKey={(m) => m.path}
              label="Media"
              columns={[
                { key: "name", label: "File", width: "minmax(0, 1.4fr)", render: (m) => <Mono>{m.name}</Mono> },
                { key: "kind", label: "Kind", width: "60px", render: (m) => m.kind },
                { key: "size", label: "Size", width: "72px", align: "end", render: (m) => <span className="num">{m.size_bytes ? formatSize(m.size_bytes) : ""}</span> },
              ]}
              empty={<span>No media in this project. Upload with <span className="mono">POST /api/studio/files/projects/{projectId}/upload</span>.</span>}
            />
          ) : null}
        </aside>
      </div>
      {data ? (
        <Sheet
          label={`Timeline of ${projectId}`}
          duration={data.duration}
          fps={fps}
          tracks={tracks}
          beats={beats}
          downbeats={downbeats}
          position={position}
          playing={playing}
          onSeek={(t) => {
            setPosition(t);
            if (video.current) video.current.currentTime = t;
          }}
          selection={selection}
          onSelect={(next) => {
            setSelection(next);
            if (next?.clip && tab !== "inspector") setParams({ panel: null });
          }}
        />
      ) : timeline.loading ? (
        <Loading what="the timeline" />
      ) : null}
    </div>
  );
}

function EffectCatalog({ entries }: { entries: CatalogEntry[] }) {
  const [preview, setPreview] = useState<{ name: string; url: string | null; pending: boolean } | null>(null);
  useStudioEvent((type, payload) => {
    const ready = payload as { name?: string; url?: string };
    if (type === "catalog.preview_ready" && preview && ready.name === preview.name) setPreview({ name: preview.name, url: ready.url ?? null, pending: false });
  });
  const request = async (name: string) => {
    setPreview({ name, url: null, pending: true });
    try {
      const result = await studio.send<{ url?: string; pending?: boolean }>("preview.request_effect", { name });
      if (result.url) setPreview({ name, url: result.url, pending: false });
    } catch {
      setPreview({ name, url: null, pending: false });
    }
  };
  return (
    <div className="catalog">
      {preview ? (
        <div className="catalog-preview">
          {preview.url ? <video src={preview.url} autoPlay loop muted playsInline /> : <p className="ink-3">{preview.pending ? `Rendering a preview of ${preview.name}...` : `No preview for ${preview.name}.`}</p>}
        </div>
      ) : null}
      <ul className="catalog-list">
        {entries.map((entry) => (
          <li key={entry.name}>
            <button type="button" className="catalog-item" onClick={() => void request(entry.name)} title={entry.previewable_reason ?? undefined}>
              <span className="mono">{entry.name}</span>
              {entry.description ? <span className="ink-3">{entry.description}</span> : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

