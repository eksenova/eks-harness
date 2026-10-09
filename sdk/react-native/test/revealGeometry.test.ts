import { revealOffset } from "../src/geometry";

const screen = { start: 0, size: 844 };
const viewport = { start: 100, size: 600 };

describe("revealOffset", () => {
  it("leaves a fully visible field where it is", () => {
    expect(revealOffset({ start: 300, size: 44 }, viewport, screen, 200, 2000)).toBeNull();
  });

  it("centres a field that sits below the scroll view's visible frame", () => {
    const contentStart = 1200;
    const current = 400;
    const item = { start: viewport.start + contentStart - current, size: 44 };
    expect(revealOffset(item, viewport, screen, contentStart, 3000)).toBe(contentStart - (100 + (600 - 44) / 2 - 100));
  });

  it("centres a field that was scrolled above the visible frame", () => {
    expect(revealOffset({ start: 40, size: 44 }, viewport, screen, 340, 3000)).toBe(340 - 278);
  });

  it("reveals a field cut off only partially at the bottom edge", () => {
    expect(revealOffset({ start: 680, size: 44 }, viewport, screen, 980, 3000)).toBe(980 - 278);
  });

  it("clips the frame to the screen when the scroll view is taller than the window", () => {
    const tall = { start: 0, size: 1400 };
    expect(revealOffset({ start: 900, size: 40 }, tall, screen, 900, 4000)).toBe(900 - 402);
  });

  it("never scrolls past the end of the content", () => {
    expect(revealOffset({ start: 760, size: 44 }, viewport, screen, 1360, 1500)).toBe(900);
  });

  it("never scrolls before the top of the content", () => {
    expect(revealOffset({ start: 60, size: 44 }, viewport, screen, 20, 3000)).toBe(0);
  });

  it("aligns a field taller than the frame to the frame's top", () => {
    expect(revealOffset({ start: 900, size: 800 }, viewport, screen, 1500, 5000)).toBe(1500);
  });

  it("does nothing when the scroll view is entirely off screen", () => {
    expect(revealOffset({ start: 1000, size: 44 }, { start: 900, size: 400 }, screen, 100, 3000)).toBeNull();
  });
});
