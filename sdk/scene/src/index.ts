import { installRuntime, type Emitted, type FrameInfo, type SceneRuntime } from "./runtime";

export type { Emitted, FrameInfo, SceneConfig, SceneInput, TextureSpec } from "./runtime";
export { SceneRuntime, installRuntime } from "./runtime";
export { VirtualClock } from "./clock";

function runtime(): SceneRuntime {
  return installRuntime(window as Window & typeof globalThis);
}

export const scene = {
  get time(): number {
    return runtime().time;
  },
  get frame(): number {
    return runtime().frame;
  },
  get fps(): number {
    return runtime().fps;
  },
  get mode(): string {
    return runtime().config.mode;
  },
  get props(): Record<string, unknown> {
    return runtime().props;
  },
  on(name: string, listener: (data: unknown, info: FrameInfo & { name: string }) => void): () => void {
    return runtime().on(name, listener);
  },
  onFrame(listener: (info: FrameInfo) => void): () => void {
    return runtime().onFrame(listener);
  },
  emit(name: string, data?: unknown): void {
    runtime().emit(name, data);
  },
  hold<T>(work: Promise<T>): Promise<T> {
    return runtime().hold(work);
  },
  texture(name: string, element?: HTMLImageElement): HTMLImageElement {
    return runtime().texture(name, element);
  },
  beat(index: number): number | undefined {
    return runtime().beat(index);
  },
  beatAt(time?: number): number {
    return runtime().beatAt(time);
  },
  beatPhase(time?: number): number {
    return runtime().beatPhase(time);
  },
  emitted(): Emitted[] {
    return runtime().allEmitted();
  },
};

export default scene;
