import { VirtualClock, type ClockTarget } from "./clock";

export type SceneMode = "render" | "live";

export interface SceneInput {
  time: number;
  name?: string;
  verb?: "emit" | "set" | "key" | string;
  data?: unknown;
  prop?: string;
  value?: unknown;
  ease?: number;
  source?: string;
}

export interface TextureSpec {
  url: string;
  frames?: number;
  fps?: number;
  offset?: number;
}

export interface SceneConfig {
  mode: SceneMode;
  fps: number;
  track?: string;
  start?: number;
  duration?: number;
  props?: Record<string, unknown>;
  inputs?: SceneInput[];
  textures?: Record<string, TextureSpec>;
  beats?: number[];
  downbeats?: number[];
  socket?: string;
}

export interface Emitted {
  time: number;
  frame: number;
  name: string;
  data?: unknown;
}

export interface FrameInfo {
  time: number;
  frame: number;
  fps: number;
}

type Listener = (data: unknown, info: FrameInfo & { name: string }) => void;
type FrameListener = (info: FrameInfo) => void;

interface TrackedAnimation {
  start: number;
}

const DEFAULT_CONFIG: SceneConfig = { mode: "live", fps: 30 };

export class SceneRuntime {
  readonly clock = new VirtualClock();
  readonly config: SceneConfig;
  readonly props: Record<string, unknown>;
  private listeners = new Map<string, Set<Listener>>();
  private frameListeners = new Set<FrameListener>();
  private emitted: Emitted[] = [];
  private delivered = 0;
  private holds = new Set<Promise<unknown>>();
  private animations = new WeakMap<Animation, TrackedAnimation>();
  private textures = new Map<string, Set<HTMLImageElement>>();
  private socket: WebSocket | null = null;
  private liveStart = 0;
  private lastFrame = -1;

  constructor(config: Partial<SceneConfig>, private readonly scope: Window & typeof globalThis = window) {
    this.config = { ...DEFAULT_CONFIG, ...config };
    this.props = { ...(this.config.props ?? {}) };
    this.config.inputs = [...(this.config.inputs ?? [])].sort((a, b) => a.time - b.time);
    if (this.config.mode === "render") {
      this.clock.install(scope as unknown as ClockTarget);
    }
  }

  get fps(): number {
    return this.config.fps;
  }

  get time(): number {
    if (this.config.mode === "render") return this.clock.time / 1000;
    return (this.scope.performance.now() - this.liveStart) / 1000;
  }

  get frame(): number {
    return Math.round(this.time * this.fps);
  }

  info(): FrameInfo {
    return { time: this.time, frame: this.frame, fps: this.fps };
  }

  on(name: string, listener: Listener): () => void {
    let set = this.listeners.get(name);
    if (!set) {
      set = new Set();
      this.listeners.set(name, set);
    }
    set.add(listener);
    return () => set?.delete(listener);
  }

  onFrame(listener: FrameListener): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  emit(name: string, data?: unknown): void {
    const entry: Emitted = { time: this.time, frame: this.frame, name, data };
    this.emitted.push(entry);
    if (this.socket?.readyState === 1) {
      this.socket.send(JSON.stringify({ type: "emit", track: this.config.track, ...entry }));
    }
  }

  hold<T>(work: Promise<T>): Promise<T> {
    this.holds.add(work);
    const done = () => this.holds.delete(work);
    work.then(done, done);
    return work;
  }

  texture(name: string, element?: HTMLImageElement): HTMLImageElement {
    const spec = this.config.textures?.[name];
    const image = element ?? this.scope.document.createElement("img");
    image.dataset.ehxTexture = name;
    let set = this.textures.get(name);
    if (!set) {
      set = new Set();
      this.textures.set(name, set);
    }
    set.add(image);
    if (spec) image.src = this.textureUrl(spec, this.frame);
    return image;
  }

  beat(index: number): number | undefined {
    return this.config.beats?.[index];
  }

  beatAt(time = this.time): number {
    const beats = this.config.beats ?? [];
    let lo = 0;
    let hi = beats.length - 1;
    if (!beats.length || time < beats[0]) return -1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (beats[mid] <= time) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  beatPhase(time = this.time): number {
    const beats = this.config.beats ?? [];
    const i = this.beatAt(time);
    if (i < 0 || i + 1 >= beats.length) return 0;
    return (time - beats[i]) / (beats[i + 1] - beats[i]);
  }

  async renderFrame(frame: number): Promise<{ frame: number; time: number; emitted: Emitted[] }> {
    if (this.config.mode !== "render") throw new Error("renderFrame is only available in render mode");
    if (frame <= this.lastFrame) throw new Error(`frame ${frame} was already rendered`);
    const before = this.emitted.length;
    const targetMs = (frame / this.fps) * 1000;
    const inputs = this.config.inputs ?? [];
    while (this.delivered < inputs.length && inputs[this.delivered].time * 1000 <= targetMs + 1e-6) {
      const input = inputs[this.delivered++];
      this.clock.advanceTo(Math.max(this.clock.time, input.time * 1000));
      this.dispatch(input);
    }
    this.clock.advanceTo(targetMs);
    this.lastFrame = frame;
    this.syncAnimations();
    const info = this.info();
    for (const listener of [...this.frameListeners]) listener(info);
    await Promise.all([this.syncMedia(), this.syncTextures(), ...this.holds]);
    await this.fontsReady();
    return { frame, time: info.time, emitted: this.emitted.slice(before) };
  }

  allEmitted(): Emitted[] {
    return [...this.emitted];
  }

  startLive(): void {
    if (this.config.mode !== "live") return;
    this.liveStart = this.scope.performance.now();
    const tick = () => {
      const info = this.info();
      const inputs = this.config.inputs ?? [];
      while (this.delivered < inputs.length && inputs[this.delivered].time <= info.time) {
        this.dispatch(inputs[this.delivered++]);
      }
      for (const listener of [...this.frameListeners]) listener(info);
      void this.syncTextures();
      this.scope.requestAnimationFrame(tick);
    };
    this.scope.requestAnimationFrame(tick);
    if (this.config.socket) this.connect(this.config.socket);
  }

  deliver(input: SceneInput): void {
    this.dispatch(input);
  }

  private connect(url: string): void {
    const socket = new this.scope.WebSocket(url);
    socket.addEventListener("message", (message) => {
      try {
        const payload = JSON.parse(String(message.data));
        if (payload.type === "input") this.dispatch(payload as SceneInput);
        const position = typeof payload.time === "number" ? payload.time : payload.position;
        if ((payload.type === "seek" || payload.type === "state") && typeof position === "number") {
          this.liveStart = this.scope.performance.now() - position * 1000;
          this.delivered = (this.config.inputs ?? []).findIndex((input) => input.time >= position);
          if (this.delivered < 0) this.delivered = (this.config.inputs ?? []).length;
        }
      } catch {
        return;
      }
    });
    socket.addEventListener("close", () => {
      this.socket = null;
      this.scope.setTimeout(() => this.connect(url), 1000);
    });
    this.socket = socket;
  }

  private dispatch(input: SceneInput): void {
    const verb = input.verb ?? "emit";
    if (verb === "set" || verb === "key") {
      if (input.prop) {
        this.props[input.prop] = input.value;
        this.fire(`prop:${input.prop}`, input.value);
        this.fire("prop", { prop: input.prop, value: input.value, ease: input.ease });
      }
      return;
    }
    const data = (input.data ?? {}) as unknown;
    if (input.name) this.fire(input.name, data);
    this.fire("*", { name: input.name, data, source: input.source });
  }

  private fire(name: string, data: unknown): void {
    const set = this.listeners.get(name);
    if (!set) return;
    const info = { ...this.info(), name };
    for (const listener of [...set]) listener(data, info);
  }

  private syncAnimations(): void {
    const doc = this.scope.document as Document & { getAnimations?: () => Animation[] };
    if (typeof doc.getAnimations !== "function") return;
    const now = this.clock.time;
    for (const animation of doc.getAnimations()) {
      let tracked = this.animations.get(animation);
      if (!tracked) {
        tracked = { start: now };
        this.animations.set(animation, tracked);
        animation.pause();
      }
      animation.currentTime = now - tracked.start;
    }
  }

  private async syncMedia(): Promise<void> {
    const time = this.time;
    const media = Array.from(this.scope.document.querySelectorAll<HTMLMediaElement>("video, audio"));
    await Promise.all(
      media.map((element) => {
        const offset = Number(element.dataset.ehxStart ?? "0");
        const target = Math.max(0, time - offset);
        if (!element.paused) element.pause();
        if (Math.abs(element.currentTime - target) < 1e-4) return Promise.resolve();
        return new Promise<void>((resolve) => {
          const done = () => {
            element.removeEventListener("seeked", done);
            resolve();
          };
          element.addEventListener("seeked", done);
          element.currentTime = target;
          this.scope.setTimeout(done, 0);
        });
      }),
    );
  }

  private async syncTextures(): Promise<void> {
    const loads: Promise<unknown>[] = [];
    for (const [name, images] of this.textures) {
      const spec = this.config.textures?.[name];
      if (!spec) continue;
      const url = this.textureUrl(spec, this.frame);
      for (const image of images) {
        if (image.getAttribute("src") === url) continue;
        image.src = url;
        if (this.config.mode === "render" && typeof image.decode === "function") {
          loads.push(image.decode().catch(() => undefined));
        }
      }
    }
    await Promise.all(loads);
  }

  private textureUrl(spec: TextureSpec, frame: number): string {
    const fps = spec.fps ?? this.fps;
    let index = Math.max(0, Math.round(((frame / this.fps) - (spec.offset ?? 0)) * fps));
    if (spec.frames) index = Math.min(index, spec.frames - 1);
    return spec.url.replace("{frame}", String(index).padStart(6, "0"));
  }

  private async fontsReady(): Promise<void> {
    const fonts = (this.scope.document as Document & { fonts?: FontFaceSet }).fonts;
    if (fonts?.ready) await fonts.ready;
  }
}

declare global {
  interface Window {
    __EHX_SCENE_CONFIG__?: Partial<SceneConfig>;
    __ehx?: SceneRuntime;
  }
}

export function installRuntime(scope: Window & typeof globalThis = window): SceneRuntime {
  if (scope.__ehx) return scope.__ehx;
  const runtime = new SceneRuntime(scope.__EHX_SCENE_CONFIG__ ?? {}, scope);
  scope.__ehx = runtime;
  if (runtime.config.mode === "live") {
    if (scope.document.readyState === "loading") {
      scope.document.addEventListener("DOMContentLoaded", () => runtime.startLive());
    } else {
      runtime.startLive();
    }
  }
  return runtime;
}

if (typeof window !== "undefined" && (window as Window).__EHX_SCENE_CONFIG__ !== undefined) {
  installRuntime(window as Window & typeof globalThis);
}
