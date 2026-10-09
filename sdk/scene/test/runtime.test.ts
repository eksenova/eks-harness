// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";
import { SceneRuntime } from "../src/runtime";

function makeRuntime(config: ConstructorParameters<typeof SceneRuntime>[0]) {
  return new SceneRuntime({ mode: "render", fps: 30, ...config }, window as Window & typeof globalThis);
}

describe("SceneRuntime in render mode", () => {
  it("delivers score inputs at their exact times and records emits", async () => {
    const runtime = makeRuntime({
      inputs: [
        { time: 0.5, verb: "emit", name: "flip", data: { side: "back" } },
        { time: 0.2, verb: "set", prop: "pulse", value: 1 },
      ],
      beats: [0, 0.5, 1, 1.5],
    });
    const heard: Array<[string, number, unknown]> = [];
    runtime.on("flip", (data, info) => {
      heard.push(["flip", info.time, data]);
      runtime.emit("flipped", { at: info.frame });
    });
    runtime.on("prop:pulse", (value, info) => heard.push(["pulse", info.time, value]));
    let frames = 0;
    runtime.onFrame(() => frames++);
    for (let f = 0; f <= 20; f++) await runtime.renderFrame(f);
    expect(heard).toEqual([
      ["pulse", 0.2, 1],
      ["flip", 0.5, { side: "back" }],
    ]);
    expect(runtime.props.pulse).toBe(1);
    expect(runtime.allEmitted()).toEqual([{ time: 0.5, frame: 15, name: "flipped", data: { at: 15 } }]);
    expect(frames).toBe(21);
    expect(runtime.beatAt(0.7)).toBe(1);
    expect(runtime.beatPhase(0.75)).toBeCloseTo(0.5);
    await expect(runtime.renderFrame(3)).rejects.toThrow(/already rendered/);
  });

  it("drives page timers and textures from the virtual clock", async () => {
    const runtime = makeRuntime({ textures: { screen: { url: "/t/screen/{frame}.png", fps: 30 } } });
    const ticks: number[] = [];
    window.setTimeout(() => ticks.push(performance.now()), 100);
    const image = runtime.texture("screen");
    await runtime.renderFrame(0);
    expect(image.getAttribute("src")).toBe("/t/screen/000000.png");
    await runtime.renderFrame(4);
    expect(ticks).toEqual([100]);
    expect(image.getAttribute("src")).toBe("/t/screen/000004.png");
  });
});
