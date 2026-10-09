export type TimerCallback = (...args: unknown[]) => void;
export type FrameCallback = (time: number) => void;

interface Timer {
  id: number;
  due: number;
  every: number | null;
  callback: TimerCallback;
  args: unknown[];
  order: number;
}

export interface ClockTarget {
  Date: DateConstructor;
  performance: { now(): number };
  setTimeout: (cb: TimerCallback, ms?: number, ...args: unknown[]) => unknown;
  clearTimeout: (id: unknown) => void;
  setInterval: (cb: TimerCallback, ms?: number, ...args: unknown[]) => unknown;
  clearInterval: (id: unknown) => void;
  requestAnimationFrame?: (cb: FrameCallback) => number;
  cancelAnimationFrame?: (id: number) => void;
}

export class VirtualClock {
  private now = 0;
  private nextId = 1;
  private order = 0;
  private timers = new Map<number, Timer>();
  private frames = new Map<number, FrameCallback>();
  private installed: Partial<ClockTarget> | null = null;

  constructor(readonly epoch: number = Date.UTC(2026, 0, 1)) {}

  get time(): number {
    return this.now;
  }

  setTimeout(callback: TimerCallback, ms = 0, ...args: unknown[]): number {
    return this.add(callback, ms, null, args);
  }

  setInterval(callback: TimerCallback, ms = 0, ...args: unknown[]): number {
    return this.add(callback, ms, Math.max(1, ms), args);
  }

  clear(id: unknown): void {
    this.timers.delete(Number(id));
  }

  requestAnimationFrame(callback: FrameCallback): number {
    const id = this.nextId++;
    this.frames.set(id, callback);
    return id;
  }

  cancelAnimationFrame(id: number): void {
    this.frames.delete(id);
  }

  pending(): number {
    return this.timers.size + this.frames.size;
  }

  advanceTo(target: number, onStep?: (time: number) => void): void {
    if (target < this.now) {
      throw new Error(`virtual time cannot go back (${target} < ${this.now})`);
    }
    for (let guard = 0; guard < 100000; guard++) {
      const next = this.nextTimer(target);
      if (!next) break;
      this.now = Math.max(this.now, next.due);
      onStep?.(this.now);
      if (next.every === null) {
        this.timers.delete(next.id);
      } else {
        next.due += next.every;
        next.order = this.order++;
      }
      next.callback(...next.args);
    }
    this.now = target;
    onStep?.(this.now);
    const callbacks = [...this.frames.values()];
    this.frames.clear();
    for (const callback of callbacks) callback(this.now);
  }

  install(target: ClockTarget): void {
    if (this.installed) return;
    const clock = this;
    const RealDate = target.Date;
    this.installed = { ...target };
    const epoch = this.epoch;
    class VirtualDate extends RealDate {
      constructor(...args: unknown[]) {
        if (args.length === 0) {
          super(epoch + clock.now);
        } else {
          super(...(args as [number]));
        }
      }
      static now(): number {
        return epoch + clock.now;
      }
    }
    target.Date = VirtualDate as DateConstructor;
    target.performance.now = () => clock.now;
    target.setTimeout = (cb, ms, ...args) => clock.setTimeout(cb, ms, ...args);
    target.setInterval = (cb, ms, ...args) => clock.setInterval(cb, ms, ...args);
    target.clearTimeout = (id) => clock.clear(id);
    target.clearInterval = (id) => clock.clear(id);
    target.requestAnimationFrame = (cb) => clock.requestAnimationFrame(cb);
    target.cancelAnimationFrame = (id) => clock.cancelAnimationFrame(id);
  }

  private add(callback: TimerCallback, ms: number, every: number | null, args: unknown[]): number {
    const id = this.nextId++;
    const delay = Number.isFinite(ms) ? Math.max(0, ms) : 0;
    this.timers.set(id, { id, due: this.now + delay, every, callback, args, order: this.order++ });
    return id;
  }

  private nextTimer(limit: number): Timer | undefined {
    let best: Timer | undefined;
    for (const timer of this.timers.values()) {
      if (timer.due > limit) continue;
      if (!best || timer.due < best.due || (timer.due === best.due && timer.order < best.order)) best = timer;
    }
    return best;
  }
}
