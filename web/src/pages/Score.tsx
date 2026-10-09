import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { useStatus } from "../api/queries";
import { useDriverWorkers, useScorePlan, type LiveSnapshot, type PlanAction, type PlanEvent, type ScoreIR, type ScorePlan, type ScoreTrack } from "../api/system";
import { Button } from "../components/Button";
import { Select } from "../components/Form";
import { DefList, MetaItem, Mono, PageHeader, Section, Tabs } from "../components/Misc";
import { EmptyState, ErrorState, Loading, Notice } from "../components/Notice";
import { Rack, RunBlock, StateLine, Timecode } from "../components/Workbench";
import { plural } from "../lib/format";
import { useEventListener } from "../lib/events";
import { shortcutAllowed, useKeydown, useTitle } from "../lib/hooks";
import { useSearchParams } from "../lib/url";
import { kindCode, Sheet, type SheetLink, type SheetSelection, type SheetTrack, type TrackKind } from "../sheet/Sheet";
import { PluginSlot } from "../shell/plugins";

const KINDS: TrackKind[] = ["edit", "blender", "web", "device", "audio"];

function trackKind(track: ScoreTrack): TrackKind {
  return (KINDS as string[]).includes(track.kind) ? (track.kind as TrackKind) : "plugin";
}

function trackSource(track: ScoreTrack): string {
  if (track.kind === "web") return String(track.entry ?? "");
  if (track.kind === "blender") return String(track.script ?? track.blend ?? "");
  if (track.kind === "device") return `${track.platform}${track.flow ? `, ${track.flow}` : ""}`;
  if (track.kind === "audio") return String(track.path ?? "");
  if (track.kind === "edit") return `${(track.layers ?? []).length} layers`;
  return track.kind;
}

function describeAction(action: { verb: string; args?: Record<string, unknown> }): string {
  const args = action.args ?? {};
  if (action.verb === "emit") return `emit ${String(args.name ?? "")}`;
  if (action.verb === "set" || action.verb === "key") return `${action.verb} ${String(args.prop ?? "")} = ${JSON.stringify(args.value)}`;
  const main = args.target ?? args.route ?? args.url ?? args.fake ?? args.name;
  return main ? `${action.verb} ${String(main)}` : action.verb;
}

export function buildSheet(score: ScoreIR, plan: ScorePlan): { tracks: SheetTrack[]; links: SheetLink[] } {
  const counters: Record<string, number> = {};
  const ordered = [...score.tracks].sort((a, b) => KINDS.indexOf(trackKind(a)) - KINDS.indexOf(trackKind(b)));
  const tracks: SheetTrack[] = ordered.map((track) => {
    const kind = trackKind(track);
    counters[kind] = (counters[kind] ?? 0) + 1;
    const span = plan.tracks[track.id] ?? { start: 0, end: plan.duration ?? 0 };
    const inputs = plan.actions
      .filter((a) => a.target === track.id)
      .map((a, i) => ({ id: `in-${track.id}-${i}-${a.frame}`, time: a.time, name: describeAction(a), dir: "in" as const, detail: `rule ${a.rule}${a.cause ? `, after ${a.cause.source} ${a.cause.name}` : ""}` }));
    const outputs = plan.events
      .filter((e) => e.source === track.id)
      .map((e, i) => ({ id: `out-${track.id}-${i}-${e.time}`, time: e.time, name: e.name, dir: "out" as const }));
    return {
      id: track.id,
      code: `${kindCode(kind)}${counters[kind]}`,
      kind,
      name: track.id,
      meta: trackSource(track),
      clips: [{ id: track.id, start: span.start, end: Math.max(span.end, span.start + 0.04), label: track.label ?? track.id, detail: trackSource(track) }],
      events: [...inputs, ...outputs],
    };
  });
  const links: SheetLink[] = plan.actions
    .filter((a) => a.cause && a.cause.source !== "score")
    .map((a, i) => ({ id: `l${i}`, from: { track: a.cause!.source, time: a.cause!.time }, to: { track: a.target, time: a.time } }));
  return { tracks, links };
}

function whenText(rule: ScoreIR["rules"][number]): string {
  const when = rule.when;
  if (when.at) return `at ${when.at}${when.offset ? ` + ${when.offset} s` : ""}`;
  const where = when.where && Object.keys(when.where).length ? ` where ${JSON.stringify(when.where)}` : "";
  return `on ${when.source ? `${when.source} ` : ""}${when.event}${where}${when.offset ? ` + ${when.offset} s` : ""}${when.once ? ", once" : ""}`;
}

export function ScorePage() {
  const params = useSearchParams();
  const path = params.path ?? "";
  const tab = params.panel ?? "rules";
  const [analyze, setAnalyze] = useState(true);
  const query = useScorePlan(path, analyze);
  const name = path.split("/").slice(-2, -1)[0] ?? "score";
  useTitle(`${query.data?.score.name ?? name} - Studio`);
  const [position, setPosition] = useState(0);
  const [selection, setSelection] = useState<SheetSelection | null>(null);
  const [render, setRender] = useState<{ state: string; detail: string; output?: string } | null>(null);
  const [live, setLive] = useState<LiveSnapshot | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [binding, setBinding] = useState(false);
  const resource = query.data ? `score:${query.data.score.name}` : null;

  useEventListener((event) => {
    if (!resource || event.resource !== resource) return;
    if (event.type === "score.render.progress") setRender({ state: "running", detail: `${String(event.detail.stage ?? "")} ${String(event.detail.track ?? "")}`.trim() });
    if (event.type === "score.render.done") setRender({ state: "done", detail: `${String(event.detail.iterations ?? "")} passes`, output: String(event.detail.output ?? "") });
    if (event.type === "score.render.failed") setRender({ state: "failed", detail: String(event.detail.error ?? "") });
  });

  const sheet = useMemo(() => (query.data ? buildSheet(query.data.score, query.data.plan) : null), [query.data]);

  if (!path) return <div className="page"><EmptyState action={<Link to="/studio" className="link">Studio</Link>}>Pick a score in the studio.</EmptyState></div>;
  if (query.isPending) return <div className="page"><Loading what="the score and its beat grid" /></div>;
  if (query.isError) {
    const message = query.error instanceof ApiError ? query.error.message : String(query.error);
    return (
      <div className="page">
        <PageHeader crumbs={[{ label: "Studio", to: "/studio" }]} title={name} />
        <ErrorState message={message} onRetry={() => void query.refetch()} />
        {analyze ? <Button onClick={() => setAnalyze(false)}>Open without beat analysis</Button> : null}
      </div>
    );
  }
  const { score, plan } = query.data;
  const fps = plan.fps;
  const duration = plan.duration ?? Math.max(1, ...Object.values(plan.tracks).map((t) => t.end));
  const selectedTrack = selection ? score.tracks.find((t) => t.id === selection.track) : null;
  const selectedEvent = selection?.event ? sheet?.tracks.find((t) => t.id === selection.track)?.events?.find((e) => e.id === selection.event) : null;

  const startRender = async () => {
    setRender({ state: "queued", detail: "" });
    try {
      await api.post("/api/scores/render", { path });
    } catch (error) {
      setRender({ state: "failed", detail: error instanceof ApiError ? error.message : String(error) });
    }
  };

  return (
    <div className="page page--studio">
      <PageHeader
        crumbs={[{ label: "Studio", to: "/studio" }]}
        title={score.name}
        actions={
          <div className="action-row">
            <Button onClick={() => void startRender()}>Render</Button>
            <Button
              variant="primary"
              disabled={Boolean(live)}
              onClick={() => (score.tracks.some((t) => t.kind === "device") ? setBinding(true) : void openLive(path, analyze, {}, setLive, setLiveError))}
            >
              {live ? "Live" : "Play live"}
            </Button>
          </div>
        }
        meta={
          <>
            <MetaItem>{fps} fps</MetaItem>
            <MetaItem>
              <Timecode seconds={duration} fps={fps} />
            </MetaItem>
            <MetaItem>{plural(score.tracks.length, "track")}</MetaItem>
            <MetaItem>{plural(score.rules.length, "rule")}</MetaItem>
            <MetaItem>{plan.beats.length ? `${plural(plan.beats.length, "beat")}, ${plural(plan.downbeats.length, "bar")}` : "no beat grid"}</MetaItem>
            <MetaItem><Mono title={path}>{path.split("/").slice(-3).join("/")}</Mono></MetaItem>
          </>
        }
      />
      {plan.warnings.length ? (
        <Notice variant="attention" title={plural(plan.warnings.length, "warning")}>
          <ul className="plain-list">
            {plan.warnings.slice(0, 6).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {render ? (
        <div className="run-list">
          <RunBlock title={`render ${score.name}`} state={render.state} facts={[render.detail ? <span key="d">{render.detail}</span> : null, render.output ? <Mono key="o">{render.output}</Mono> : null].filter(Boolean) as React.ReactNode[]} progress={render.state === "running" ? 0.3 : null} />
        </div>
      ) : null}
      {liveError ? <Notice variant="error">{liveError}</Notice> : null}
      {binding && !live && query.data ? (
        <BindPanel
          tracks={query.data.score.tracks.filter((t) => t.kind === "device")}
          onCancel={() => setBinding(false)}
          onStart={(devices) => {
            setBinding(false);
            void openLive(path, analyze, devices, setLive, setLiveError);
          }}
        />
      ) : null}
      {live ? <LivePanel live={live} score={score} onClose={() => setLive(null)} onPosition={setPosition} /> : null}
      {sheet ? (
        <Sheet
          label={`Score ${score.name}`}
          duration={duration}
          fps={fps}
          tracks={sheet.tracks}
          links={sheet.links}
          beats={plan.beats}
          downbeats={plan.downbeats}
          markers={Object.entries(plan.markers).map(([markerName, time]) => ({ name: markerName, time }))}
          position={position}
          onSeek={setPosition}
          selection={selection}
          onSelect={setSelection}
          playing={live?.state === "playing"}
        />
      ) : null}
      <div className="desk score-desk">
        <div className="desk-main">
          <Tabs
            label="Score panels"
            sub
            items={[
              { label: "Rules", to: "/studio/score", search: { path }, active: tab === "rules", count: score.rules.length },
              { label: "Schedule", to: "/studio/score", search: { path, panel: "schedule" }, active: tab === "schedule", count: plan.actions.length },
              { label: "Tracks", to: "/studio/score", search: { path, panel: "tracks" }, active: tab === "tracks", count: score.tracks.length },
            ]}
          />
          {tab === "rules" ? (
            <Rack
              rows={score.rules}
              rowKey={(r) => r.id ?? JSON.stringify(r.when)}
              label="Rules"
              columns={[
                { key: "id", label: "Rule", width: "minmax(0, 0.8fr)", render: (r) => <Mono>{r.id ?? "unnamed"}</Mono> },
                { key: "when", label: "When", width: "minmax(0, 1.2fr)", render: (r) => <span className="mono-ish">{whenText(r)}</span> },
                { key: "do", label: "Then", width: "minmax(0, 1.4fr)", render: (r) => r.do.map((a) => `${a.target}: ${describeAction(a)}`).join("; ") },
                { key: "fires", label: "Fires", width: "56px", align: "end", render: (r) => <span className="num">{plan.actions.filter((a) => a.rule === r.id || a.rule.startsWith(`${r.id}-`)).length}</span> },
              ]}
              empty={<span>No rules. Add them with score.on(...) in the score file.</span>}
            />
          ) : null}
          {tab === "schedule" ? (
            <Rack<PlanAction>
              rows={plan.actions}
              rowKey={(a) => `${a.rule}-${a.frame}-${a.target}-${a.verb}`}
              label="Scheduled actions"
              onRow={(a) => setPosition(a.time)}
              columns={[
                { key: "frame", label: "Frame", width: "64px", align: "end", render: (a) => <span className="num">{a.frame}</span> },
                { key: "time", label: "Time", width: "110px", render: (a) => <Timecode seconds={a.time} fps={fps} /> },
                { key: "target", label: "Track", width: "minmax(0, 0.6fr)", render: (a) => <Mono>{a.target}</Mono> },
                { key: "verb", label: "Action", width: "minmax(0, 1.2fr)", render: (a) => describeAction(a) },
                { key: "rule", label: "Rule", width: "minmax(0, 0.8fr)", render: (a) => <span className="ink-2">{a.rule}{a.cause ? `, after ${a.cause.source} ${a.cause.name}` : ""}</span> },
              ]}
              empty={<span>Nothing is scheduled yet.</span>}
            />
          ) : null}
          {tab === "tracks" ? (
            <Rack<ScoreTrack>
              rows={score.tracks}
              rowKey={(t) => t.id}
              label="Tracks"
              onRow={(t) => setSelection({ track: t.id })}
              columns={[
                { key: "id", label: "Track", width: "minmax(0, 0.7fr)", render: (t) => <Mono>{t.id}</Mono> },
                { key: "kind", label: "Kind", width: "80px", render: (t) => t.kind },
                { key: "source", label: "Source", width: "minmax(0, 1.4fr)", render: (t) => <Mono>{trackSource(t)}</Mono> },
                { key: "span", label: "Span", width: "minmax(0, 0.9fr)", render: (t) => (plan.tracks[t.id] ? `${plan.tracks[t.id].start.toFixed(2)} to ${plan.tracks[t.id].end.toFixed(2)} s` : "") },
              ]}
            />
          ) : null}
        </div>
        <aside className="desk-side">
          <Section title="Inspector">
            {selectedEvent ? (
              <DefList compact items={[{ label: selectedEvent.dir === "in" ? "Receives" : "Emits", value: selectedEvent.name }, { label: "At", value: <Timecode seconds={selectedEvent.time} fps={fps} /> }, { label: "Frame", value: <span className="num">{Math.round(selectedEvent.time * fps)}</span> }, selectedEvent.detail ? { label: "From", value: selectedEvent.detail } : null]} />
            ) : selectedTrack ? (
              <DefList
                compact
                items={[
                  { label: "Track", value: <Mono>{selectedTrack.id}</Mono> },
                  { label: "Kind", value: selectedTrack.kind },
                  { label: "Source", value: <Mono>{trackSource(selectedTrack)}</Mono> },
                  plan.tracks[selectedTrack.id] ? { label: "Span", value: `${plan.tracks[selectedTrack.id].start.toFixed(3)} to ${plan.tracks[selectedTrack.id].end.toFixed(3)} s` } : null,
                  selectedTrack.textures && Object.keys(selectedTrack.textures).length ? { label: "Textures", value: Object.entries(selectedTrack.textures).map(([k, v]) => `${k} from ${v}`).join(", ") } : null,
                  { label: "Receives", value: plural(plan.actions.filter((a) => a.target === selectedTrack.id).length, "action") },
                  { label: "Emits", value: plural(plan.events.filter((e) => e.source === selectedTrack.id).length, "event") },
                ]}
              />
            ) : (
              <>
                <p className="sheet-legend">
                  <span><i className="legend-flag legend-flag--in" /> input</span>
                  <span><i className="legend-flag legend-flag--out" /> emitted</span>
                  <span><i className="legend-cause" /> cause</span>
                </p>
                <p className="ink-2">Select a track, clip or event.</p>
              </>
            )}
            <PluginSlot slot="score.inspector" props={{ score: score as unknown as Record<string, unknown>, track: (selectedTrack ?? undefined) as unknown as Record<string, unknown> }} />
          </Section>
        </aside>
      </div>
    </div>
  );
}

async function openLive(path: string, analyze: boolean, devices: Record<string, string>, set: (live: LiveSnapshot | null) => void, fail: (message: string | null) => void) {
  fail(null);
  try {
    const snapshot = await api.post<LiveSnapshot>("/api/scores/live", { path, analyze, devices }, { timeoutMs: 120_000 });
    set(snapshot);
  } catch (error) {
    fail(error instanceof ApiError ? error.message : String(error));
  }
}

const LEASE_KINDS: Record<string, string> = { web: "browser", ios: "ios", android: "android" };

function BindPanel({ tracks, onStart, onCancel }: { tracks: ScoreTrack[]; onStart: (devices: Record<string, string>) => void; onCancel: () => void }) {
  const status = useStatus();
  const workers = useDriverWorkers();
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const withWorker = new Set((workers.data?.items ?? []).map((w) => w.sid));
  return (
    <section className="bind-panel" aria-label="Bind device tracks">
      <h2 className="section-title">Bind device tracks</h2>
      <Rack<ScoreTrack>
        rows={tracks}
        rowKey={(t) => t.id}
        label="Device tracks"
        columns={[
          { key: "id", label: "Track", width: "minmax(0, 0.6fr)", render: (t) => <Mono>{t.id}</Mono> },
          { key: "platform", label: "Platform", width: "80px", render: (t) => String(t.platform ?? "") },
          {
            key: "lease",
            label: "Lease",
            width: "minmax(0, 1.6fr)",
            render: (t) => {
              const leases = (status.data?.leases ?? []).filter((l) => l.kind === LEASE_KINDS[String(t.platform)]);
              return (
                <Select aria-label={`Lease for ${t.id}`} value={chosen[t.id] ?? ""} onChange={(event) => setChosen({ ...chosen, [t.id]: event.target.value })}>
                  <option value="">Not bound (actions are only listed)</option>
                  {leases.map((l) => (
                    <option key={l.sid} value={l.sid}>
                      {l.sid} {l.sessionName ?? ""} {withWorker.has(l.sid) ? "" : "(no driver worker)"}
                    </option>
                  ))}
                </Select>
              );
            },
          },
          { key: "state", label: "State", width: "minmax(0, 0.7fr)", render: (t) => (chosen[t.id] ? <StateLine state={withWorker.has(chosen[t.id]) ? "bound" : "no worker"} tone={withWorker.has(chosen[t.id]) ? "ok" : "wait"} /> : <StateLine state="unbound" tone="idle" />) },
        ]}
      />
      <div className="button-row">
        <Button variant="primary" onClick={() => onStart(Object.fromEntries(Object.entries(chosen).filter(([, sid]) => sid && withWorker.has(sid))))}>
          Start live mode
        </Button>
        <Button variant="quiet" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </section>
  );
}

function LivePanel({ live, score, onClose, onPosition }: { live: LiveSnapshot; score: ScoreIR; onClose: () => void; onPosition: (t: number) => void }) {
  const [state, setState] = useState(live);
  const [previews, setPreviews] = useState(live.previews ?? {});
  const [feed, setFeed] = useState<{ key: string; time: number; text: string; kind: string }[]>([]);
  const socket = useRef<WebSocket | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const videos = useRef(new Map<string, HTMLVideoElement>());
  const offset = live.audio?.offset ?? 0;
  const devices = score.tracks.filter((t) => t.kind === "device");
  const webs = score.tracks.filter((t) => t.kind === "web");
  const blenders = score.tracks.filter((t) => t.kind === "blender");
  const sync = (position: number, playing: boolean, force: boolean) => {
    const element = audio.current;
    if (element) {
      const target = offset + position;
      if (force || Math.abs(element.currentTime - target) > 0.08) element.currentTime = target;
      if (playing && element.paused) void element.play().catch(() => undefined);
      if (!playing && !element.paused) element.pause();
    }
    for (const video of videos.current.values()) {
      if (force || Math.abs(video.currentTime - position) > 0.08) video.currentTime = Math.min(position, video.duration || position);
      if (playing && video.paused) void video.play().catch(() => undefined);
      if (!playing && !video.paused) video.pause();
    }
  };
  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${window.location.host}/api/scores/live/${live.id}/ws?role=ui`);
    socket.current = ws;
    let playing = live.state === "playing";
    ws.addEventListener("message", (message) => {
      const data = JSON.parse(String(message.data));
      if (data.type === "tick") {
        onPosition(data.position);
        sync(data.position, playing, false);
      }
      if (data.type === "seek") {
        onPosition(data.position);
        sync(data.position, playing, true);
      }
      if (data.type === "state") {
        playing = data.state === "playing";
        setState((current) => ({ ...current, state: data.state, position: data.position }));
        sync(data.position ?? 0, playing, true);
      }
      if (data.type === "preview") setPreviews((current) => ({ ...current, [data.track]: { state: data.state, url: data.url, error: data.error, withoutTextures: data.withoutTextures } }));
      if (data.type === "event" || data.type === "fire" || data.type === "input") {
        const text = data.type === "event" ? `${data.source} emits ${data.name}` : `${data.target}: ${describeAction({ verb: data.verb, args: data.args ?? data })}${data.ok === false ? `, failed: ${data.error}` : ""}`;
        setFeed((items) => [{ key: `${Date.now()}-${Math.random()}`, time: data.time ?? 0, text, kind: data.type }, ...items].slice(0, 80));
      }
    });
    return () => ws.close();
  }, [live.id]);
  const send = (message: Record<string, unknown>) => socket.current?.readyState === 1 && socket.current.send(JSON.stringify(message));
  useKeydown((event) => {
    if (!shortcutAllowed(event) || event.key !== " ") return;
    event.preventDefault();
    send({ type: state.state === "playing" ? "pause" : "play" });
  });
  const close = async () => {
    audio.current?.pause();
    await api.delete(`/api/scores/live/${live.id}`).catch(() => undefined);
    onClose();
  };
  return (
    <section className="live-panel" aria-label="Live mode">
      <div className="live-bar">
        <StateLine state={state.state === "playing" ? "playing live" : state.state} />
        <Button size="sm" variant="primary" onClick={() => send({ type: state.state === "playing" ? "pause" : "play" })}>
          {state.state === "playing" ? "Pause" : "Play"}
        </Button>
        <Button size="sm" onClick={() => send({ type: "seek", position: 0 })}>To start</Button>
        <Button size="sm" onClick={() => send({ type: "stop" })}>Stop</Button>
        <label className="checkbox-inline">
          <input type="checkbox" defaultChecked={live.loop} onChange={(event) => send({ type: "loop", value: event.target.checked })} /> Loop
        </label>
        {live.audio ? <span className="ink-3">Audio <Mono>{live.audio.track}</Mono>{offset ? ` from ${offset.toFixed(2)} s` : ""}</span> : <span className="ink-3">No audio track</span>}
        <span className="sheet-fill" />
        <span className="ink-3">Session <Mono>{live.id}</Mono></span>
        <Button size="sm" variant="quiet" onClick={() => void close()}>Close live mode</Button>
      </div>
      {live.audio ? <audio ref={audio} src={live.audio.url} preload="auto" /> : null}
      <div className="live-grid">
        <div className="live-scenes scroll-x">
          {webs.map((track) => {
            const [w, h] = (track.size as [number, number] | undefined) ?? [1080, 1920];
            return (
              <figure key={track.id} className="live-scene">
                <ScaledBox width={w} height={h}>
                  {(scale) => (
                    <iframe
                      title={`Web scene ${track.id}`}
                      src={`/api/scores/live/${live.id}/scene/${encodeURIComponent(track.id)}/${String(track.entry ?? "").split("/").pop()}`}
                      sandbox="allow-scripts allow-same-origin"
                      style={{ width: w, height: h, transform: `scale(${scale})`, transformOrigin: "top left" }}
                    />
                  )}
                </ScaledBox>
                <figcaption><Mono>{track.id}</Mono> web scene, {w}x{h}</figcaption>
              </figure>
            );
          })}
          {blenders.map((track) => {
            const preview = previews[track.id];
            const [w, h] = (track.size as [number, number] | undefined) ?? [1080, 1920];
            const without = preview?.withoutTextures ?? [];
            return (
              <figure key={track.id} className="live-scene">
                <ScaledBox width={w} height={h}>{() => (preview?.state === "ready" && preview.url ? (
                  <video
                    ref={(element) => {
                      if (element) videos.current.set(track.id, element);
                      else videos.current.delete(track.id);
                    }}
                    src={preview.url}
                    muted
                    playsInline
                    preload="auto"
                    className="live-fill"
                  />
                ) : (
                  <div className="live-scene-wait live-fill">
                    <StateLine state={preview?.state ?? "waiting for preview"} detail={preview?.error ?? null} />
                  </div>
                ))}</ScaledBox>
                <figcaption>
                  <Mono>{track.id}</Mono> Blender preview{without.length ? <span className="ink-3">, without {without.join(", ")}</span> : null}
                </figcaption>
              </figure>
            );
          })}
          {devices.map((track) => (
            <div key={track.id} className="live-device">
              <span className="strong mono">{track.id}</span>
              <span className="ink-2">{track.platform}</span>
              {live.devices[track.id] ? (
                <StateLine state={`bound to ${live.devices[track.id]}`} tone="ok" />
              ) : (
                <StateLine state="unbound, actions only listed" tone="idle" />
              )}
            </div>
          ))}
          {!webs.length && !devices.length && !blenders.length ? <EmptyState>No web, Blender or device tracks to show live.</EmptyState> : null}
        </div>
        <ol className="live-feed" aria-label="Live events">
          {feed.map((item) => (
            <li key={item.key} data-kind={item.kind}>
              <Timecode seconds={item.time} fps={live.fps} /> <span>{item.text}</span>
            </li>
          ))}
          {!feed.length ? <li className="ink-3">Events and fired actions appear here while the score plays.</li> : null}
        </ol>
      </div>
      <div className="live-emit">
        <EmitForm tracks={score.tracks} onEmit={(track, name) => send({ type: "emit", track, name })} />
      </div>
    </section>
  );
}

const TILE_WIDTH = 180;
const TILE_WIDTH_PHONE = 132;

function ScaledBox({ width, height, children }: { width: number; height: number; children: (scale: number) => React.ReactNode }) {
  const tile = window.matchMedia("(max-width: 699px)").matches ? TILE_WIDTH_PHONE : TILE_WIDTH;
  const scale = tile / width;
  return (
    <div className="live-box" style={{ width: tile, height: Math.round(height * scale) }}>
      {children(scale)}
    </div>
  );
}

function EmitForm({ tracks, onEmit }: { tracks: ScoreTrack[]; onEmit: (track: string, name: string) => void }) {
  const [track, setTrack] = useState((tracks.find((t) => t.kind === "device" || t.kind === "web") ?? tracks[0])?.id ?? "ui");
  const [name, setName] = useState("");
  return (
    <form
      className="toolbar"
      onSubmit={(event) => {
        event.preventDefault();
        if (name.trim()) onEmit(track, name.trim());
      }}
    >
      <span className="ink-2">Send an event as</span>
      <Select aria-label="Source track" value={track} onChange={(event) => setTrack(event.target.value)}>
        {tracks.map((t) => (
          <option key={t.id} value={t.id}>
            {t.id}
          </option>
        ))}
      </Select>
      <input className="input mono" aria-label="Event name" placeholder="order-placed" value={name} onChange={(event) => setName(event.target.value)} />
      <Button size="sm" type="submit" disabled={!name.trim()}>
        Send
      </Button>
    </form>
  );
}

export type { PlanEvent };
