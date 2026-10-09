import { markerFrame, relativeTo } from "../src/geometry";

const tapTarget = { x: 181.71, y: 158.1, width: 221.71, height: 36.19 };

describe("relativeTo", () => {
  it("moves an Android measureInWindow box into the edge-to-edge root that draws the marker", () => {
    const rootMeasuredBelowStatusBar = { x: 0, y: -52 };
    expect(relativeTo(tapTarget, rootMeasuredBelowStatusBar)).toEqual({
      x: 181.71,
      y: 210.1,
      width: 221.71,
      height: 36.19,
    });
  });

  it("leaves the box alone when the root sits at the window origin", () => {
    expect(relativeTo(tapTarget, { x: 0, y: 0 })).toEqual(tapTarget);
  });

  it("subtracts a root that is inset into the window", () => {
    expect(relativeTo({ x: 30, y: 120, width: 10, height: 20 }, { x: 10, y: 47 })).toEqual({
      x: 20,
      y: 73,
      width: 10,
      height: 20,
    });
  });

  it("falls back to the raw box when the root cannot be measured", () => {
    expect(relativeTo(tapTarget, null)).toBe(tapTarget);
  });
});

describe("markerFrame", () => {
  it("outlines the box with a four point inset on every side", () => {
    expect(markerFrame({ x: 8, y: 216, width: 165, height: 24 })).toEqual({
      left: 4,
      top: 212,
      width: 173,
      height: 32,
    });
  });
});
