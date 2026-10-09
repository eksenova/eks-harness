---
name: web-scenes
description: Write HTML/CSS/JS scenes for videos and scores with @eks-harness/scene: the virtual clock (timers, rAF, CSS and Web Animations are frame-exact), score inputs, beat helpers, textures from other tracks, emitting events, live mode. Use when building motion graphics or UI mockups that must sync to beats or other tracks.
---

# Web scenes

A web scene is an HTML entry (plus any CSS, JS and assets beside it). In renders it runs on a virtual
clock: `Date`, `performance.now`, `setTimeout`, `setInterval`, `requestAnimationFrame`, CSS animations and
the Web Animations API all advance exactly one frame at a time, so output is deterministic.

```html
<script>
  ehx.on("flip", (data, at) => card.classList.add("flipped"));
  ehx.on("prop:accent", (value) => root.style.setProperty("--accent", value));
  ehx.onFrame(({ time, frame }) => { bar.style.width = `${ehx.beatPhase() * 100}%`; });
  ehx.texture("screen", document.querySelector("#screen"));
  card.addEventListener("transitionend", () => ehx.emit("flipped"));
</script>
```

Bundled code can `import { scene } from "@eks-harness/scene"` (same API). Use `ehx.hold(promise)` to make
the renderer wait (fonts, images, data). In a video project the media kind is
`WebScene(entry=..., events=..., marker_events={"beat": "beat"}, media={...})`; in a score it is
`score.web(...)`. Rendering needs Playwright Chromium (`eks-harness driver install web`).

Live mode serves the same scene into the UI with a real clock; inputs and emits flow over the score's
WebSocket.
