import { describe, expect, it } from "vitest";
import { VirtualClock } from "../src/clock";

describe("VirtualClock", () => {
  it("runs timers in due order, including nested and intervals", () => {
    const clock = new VirtualClock();
    const log: string[] = [];
    clock.setTimeout(() => log.push(`a@${clock.time}`), 30);
    clock.setTimeout(() => {
      log.push(`b@${clock.time}`);
      clock.setTimeout(() => log.push(`c@${clock.time}`), 5);
    }, 10);
    const every = clock.setInterval(() => log.push(`i@${clock.time}`), 20);
    clock.advanceTo(40);
    clock.clear(every);
    clock.advanceTo(100);
    expect(log).toEqual(["b@10", "c@15", "i@20", "a@30", "i@40"]);
  });

  it("calls rAF once per advance with the frame time", () => {
    const clock = new VirtualClock();
    const seen: number[] = [];
    const loop = (t: number) => {
      seen.push(t);
      clock.requestAnimationFrame(loop);
    };
    clock.requestAnimationFrame(loop);
    clock.advanceTo(33.3);
    clock.advanceTo(66.6);
    expect(seen).toEqual([33.3, 66.6]);
    expect(() => clock.advanceTo(10)).toThrow(/cannot go back/);
  });

  it("installs Date, performance and timers on a target", () => {
    const clock = new VirtualClock(Date.UTC(2026, 0, 1));
    const target = {
      Date,
      performance: { now: () => 0 },
      setTimeout,
      clearTimeout,
      setInterval,
      clearInterval,
    } as unknown as Parameters<VirtualClock["install"]>[0];
    clock.install(target);
    clock.advanceTo(1500);
    expect(target.performance.now()).toBe(1500);
    expect(target.Date.now()).toBe(Date.UTC(2026, 0, 1) + 1500);
    expect(new target.Date().getTime()).toBe(Date.UTC(2026, 0, 1) + 1500);
    expect(new target.Date(0).getTime()).toBe(0);
  });
});
