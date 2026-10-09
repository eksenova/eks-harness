---
name: scores
description: Author, plan, render and play scores: one timeline that synchronizes video edits, Blender scenes, HTML/JS scenes, device takes and audio to beats and cues, with events flowing between tracks in both directions (a beat triggers a device action, a device event flips a web scene, a Blender impact drives the edit). Use for any synchronized video or live demo.
---

# Scores

```python
from eks_harness.score import Score, beats, cue

score = Score(fps=30, song="music/summer.ogg", window=(147.89, 175.55), size=(1080, 1920))
app = score.device("ios", flow="flows/scan.py")
cards = score.web("scenes/cards/index.html")
phone = score.blender("scenes/phone.py", textures={"screen": app, "card": cards})

score.on(beats("downbeat")[3], app.press("#start-scan"))
score.on(app.event("result-shown"), cards.emit("flip"))
score.on(cards.event("flipped") + 0.1, phone.emit("impact"))
score.on(beats()[0:32:2], app.patch("#badge", style={"opacity": 1}))

score.edit.layer(phone).layer(cards)
```

```bash
eks-harness score new scenes/score.py
eks-harness score plan score.py
eks-harness score render score.py --out renders/cut.mp4
eks-harness score export score.py
```

Time references: `12.5s`, `300ms`, `f:120`, `beat:8`, `downbeat:2`, `bar:4`, `cue:drop`, `end`, with
offsets `+0.25s`, `-2f`, `+1b`. Rules fire at times or on events (`track.event("name").first() + 0.5`).

Device tracks: the flow file's `setup(app)` (or `flow(app)`) runs before the take; then the recording
starts and the score's actions fire on the wall clock.

Render mode: device takes run first against the real target (actions fire on the wall clock, the take is
time-warped so each lands on its frame, app events become score events); web and Blender scenes render
frame-exact and are re-rendered until their emitted events settle; then the edit composites with audio.

Live mode (UI, Studio, score page): the hub plays the clock in real time, fires device actions and RN
patches immediately, web scenes run live in the browser, events flow in every direction. MCP:
`score_validate`, `score_plan`, `score_export`.
