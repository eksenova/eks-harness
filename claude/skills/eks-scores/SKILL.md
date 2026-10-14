---
name: eks-scores
description: "Scores in eks: @eks.score with a song, window and fps as the one clock, beat, bar, downbeat and cue times, the score's edit and audio mix, embedded gfx and Blender scenes, and device takes declared with sc.take and take.flow. States plainly that pinned takes (at= on take steps) and live preview are not executable yet. Use when planning a synchronized ad or demo that ties a device recording, scenes, edit and music together."
---

# Scores

A score is the clock everything else hangs on: the song's beats and bars, the cut window, the edit, the
mix, embedded scenes and device takes.

```python
import eks
from eks import t


@eks.score(fps=30, song="music/song.ogg", window=t.span(t("0s"), t("15s")))
def ad(sc: eks.Score):
    take = sc.take(platform="ios", app="mobile-app", seed="standard", as_="harman-admin")
    with take.flow() as s:
        s.ui.tap("#new-invoice", at=t("bar:1"))
        s.ui.fill("#amount", "5", at=t("beat:6"))
        s.overlay.callout("#total", "Anlık kur", at=t("bar:2"))
    phone = sc.scene("phone", screen=take)
    sc.edit.track("scene").clip(phone)
    sc.edit.track("graphics").clip(sc.gfx("titles"))
    sc.audio.master(loudness=-14)
```

```bash
eks-harness plan .harness/scores/ad.py        # the clock, pins, scene embeddings and the edit, validated
```

- `@eks.score(fps=, song=, window=, size=(1080, 1920), seed=, bpm=, meter=, duration=)`. With `song`, the score
  analyses its beats and downbeats (the clock's tempo source); `window` cuts both song and video; `bpm=`
  sets a static tempo instead.
- `sc.edit` and `sc.audio` are the score's one edit and mix (skill `eks-harness:eks-edit`); `sc.cue(name, at)`
  names a time; `sc.scene(scene, **inputs)` and `sc.gfx(scene, **inputs)` embed scenes on the score clock.
- `sc.take(platform=, app=, seed=, as_=, backend=, viewport=, span=, lead_in=, warp=True, size=, permissions=,
  now=, root_seed=)` declares a device take; `with take.flow() as s:` is its session (once per take), and
  the take handle is media for textures and clips. `take.event(name)` refers to an app event.
- Inside a take, `at=` pins a step to the score clock. Pins must lie in the take's span and the clock
  bounds; a pin inside a runtime `eks.each` is a plan error (unroll it with a Python `for`).

## Not available yet

Scores plan and validate today; they do not render under `eks-harness run` yet. `score.take` (running the
take's flow, recording it and warping the footage so each pinned step lands on its time) answers
`op_unsupported`, and the Blender scene it textures has no executor (skill `eks-harness:eks-scenes`). Live
preview of a score is planned as well. All of this is milestone M5.

What works now: gfx scenes, edits and the audio mix on the same clock (an `@eks.edit` with a song track and
`e.gfx(...)`), and flows recorded separately with `s.record(...)` then cut in an edit. For a score with
device takes and Blender today, use the earlier score engine (skill `eks-harness:scores`, `eks-harness score
plan|render`).
