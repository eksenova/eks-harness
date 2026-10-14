---
name: studio
description: "Builds and renders videos with eks-harness: writes @eks.edit timelines (clips, effects, transitions, video overlays, beats from the song), gfx scenes and audio mixes, plans and renders them with eks-harness run, reviews renders with contact sheets and machine checks, and hands over the result with artifact and studio links. Plans scores and Blender or web scenes with the eks API and renders those through the earlier video engine until their executors land. Use for promo videos, demo reels, motion graphics and synchronized device demos."
---

You make videos with eks-harness. Load `eks-harness:eks-edit`, `eks-harness:eks-gfx-audio` and
`eks-harness:video-review` first; add `eks-harness:eks-scenes` and `eks-harness:eks-scores` when the video
needs Blender, web scenes or a score.

## Way of working

1. Start from the music and the story: put the song on the edit (`e.audio.track(song).beats("beat",
   "downbeat")`), pick the window, and lay clips and overlays on beats and bars (`t("bar:4")`,
   `t("cue:drop") - t("2f")`), not on seconds, so a change to the song or window moves everything together.
2. Capture device footage with an eks flow (`with s.record(...)` plus live overlays, skill
   `eks-harness:eks-flows`), never against production, and cut it in with `e.media("artifact:<id>")`.
3. Author in code: `.harness/edits/*.py`, `.harness/scenes/*.py`. Check cheaply before rendering:
   `eks-harness plan <file>` validates times, targets, fonts and the clock without rendering; render drafts
   with `e.render_settings(quality="draft")`.
4. Render with `eks-harness run <file>` (renders take the hub's render queue one at a time; `eks-harness
   queue` shows it). Add review expectations to the edit (`e.expect.derive()`, `e.expect.cut(...)`,
   `e.expect.text(...)`) so the render checks itself.
5. Look at the result: `eks-harness video sheet <artifact>` with the beats and cues as markers gives one
   image of the whole render with its audio strip; Read it. For anything not covered by the edit's
   expectations, `eks-harness video check <artifact> expectations.json` measures cuts, text, logos, black
   or frozen spans and loudness and reports the delta in frames. Do not open frames one by one.
6. Hand over the artifact's page and direct links (and `eks-harness share create <id>` when someone without a
   login needs it). Existing studio projects publish with `eks-harness studio publish <project>`.

## What runs where today

- `@eks.edit`, `@eks.scene(kind="gfx")` and the audio mix render with `eks-harness run`.
- `@eks.scene(kind="blender"|"web")` and `@eks.score` plan and validate; their renders (Blender scene
  graph, web scenes, pinned device takes, live preview) are not built yet. For those, use the earlier video
  engine (skills `eks-harness:video-editing`, `eks-harness:blender-scenes`, `eks-harness:web-scenes`,
  `eks-harness:scores`) and say so in your report.
- Blender renders spread over nodes when they are online (`eks-harness node list`, skill `eks-harness:nodes`).
