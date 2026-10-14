import { afterEach, describe, expect, it } from "vitest";
import { clockCommand, installClock } from "../src/clock";

const RealDate = Date;

afterEach(() => {
  (globalThis as any).Date = RealDate;
});

describe("clock", () => {
  it("starts Date at the planned instant and keeps time moving", async () => {
    const result = installClock("2026-01-02T03:04:05.000Z");
    expect(Math.abs(Date.now() - RealDate.parse("2026-01-02T03:04:05.000Z"))).toBeLessThan(1000);
    expect(Math.abs(new Date().getTime() - Date.now())).toBeLessThan(50);
    expect(result.offset).toBeLessThan(0);
    const before = Date.now();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(Date.now() - before).toBeGreaterThanOrEqual(25);
  });

  it("leaves explicit dates, parsing and instanceof alone", () => {
    installClock("2030-06-01T00:00:00Z");
    expect(new Date(0).toISOString()).toBe("1970-01-01T00:00:00.000Z");
    expect(new Date(2020, 0, 1).getFullYear()).toBe(2020);
    expect(Date.UTC(2020, 0, 1)).toBe(RealDate.UTC(2020, 0, 1));
    expect(Date.parse("2020-01-01T00:00:00Z")).toBe(1577836800000);
    expect(new Date()).toBeInstanceOf(RealDate);
    expect(typeof Date()).toBe("string");
    expect(Date()).toContain("2030");
  });

  it("can be installed again with a new instant", () => {
    installClock("2030-06-01T00:00:00Z");
    installClock("2001-01-01T00:00:00Z");
    expect(new Date().getUTCFullYear()).toBe(2001);
  });

  it("rejects a missing or bad date", () => {
    expect(() => clockCommand({})).toThrow(/clock needs/);
    expect(() => clockCommand({ now: "soon" })).toThrow(/not an ISO date/);
  });
});
