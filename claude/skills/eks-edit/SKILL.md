---
name: eks-edit
description: "Edit and render video with @eks.edit: media sources (artifacts, files), tracks and clips with source spans, speed, transforms and opacity, the effect, transition, easing and curve catalogs (eks.fx, eks.tx, eks.ease, eks.curves, eks.keys), overlays on video tracks (titles, captions, callouts anchored on recorded elements), cues, beats from the song, render settings, review expectations and derived checks, and running it with eks-harness run. Use for demo cuts, beat-synced montages, captioned tutorials and any short-form video built on the new API."
---

# Edits

An edit is a timeline: tracks of clips with effects and transitions, video overlays, an audio mix and
review expectations. It renders deterministically: the same plan gives a byte-identical file on the same
machine and ffmpeg build.

```python
import eks
from eks import t


@eks.edit(fps=30, size=(720, 1280), seed=11)
def beat_flash_montage(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    e.audio.track("../assets/music.wav", name="music", gain=-6).beats("beat", "downbeat")
    main = e.track("main")
    clips = [e.media(f"../assets/clip_{index}.mp4") for index in range(4)]
    for index, clip in enumerate(clips):
        piece = main.clip(clip, start=t(f"beat:{index * 4}"), src=t.span(t("0s"), t("2s")))
        if index:
            piece.transition_in(eks.tx.beat_flash(t("8f"), stream="beat"))
    main.clip(clips[0], start=t("beat:16"), src=t.span(t("2s"), t("3s"))).effect(
        eks.fx.flash(t.beats("downbeat")[4:6], t("3f")))
    e.expect.derive()
```

```bash
eks-harness plan .harness/edits/beat_flash_montage.py      # timeline, clock and checks, no render
eks-harness run .harness/edits/beat_flash_montage.py       # render; the report lists the video artifact
eks-harness run .harness/edits/x.py --cache off            # render again even if the plan hash is cached
```

Then look at it: `eks-harness video sheet <artifact-id>` and Read the sheet (skill
`eks-harness:video-review`).

## Timeline

- `@eks.edit(fps=, size=(w, h), seed=, duration=None, window=None, background=None)`; without `duration` the
  edit lasts until its last clip ends.
- `e.media(uri)`: `artifact:<id>` (a capture or render; `artifact:<id>@sha256:<hex>` pins its content),
  a relative or `file:` path (relative to the edit file), `plugin:<id>/path`, `pkg:<module>/path`, or
  `http(s):`. `eks.media.source(uri, kind=)` is the same outside an edit.
- `e.track(name, z=None, blend=None, opacity=None)` gets or creates a track. Tracks composite by `z`, then
  declaration order; `blend` is `normal`, `add`, `multiply`, `screen` or `overlay`.
- `track.clip(media, start=, src=, speed=1.0, opacity=1.0, fit="cover", audio=False, gain=0.0)` returns a
  clip builder: `.effect(*effects)`, `.transition_in(tx)`, `.transition_out(tx)`, `.set(x=, y=, scale=,
  rotation=, ...)`. A clip covers the timeline from `start` until its `src` span is used up at its speed.
- Animated values (`speed`, `opacity`, `x`, `y`, `scale`, `rotation`, `gain`, many effect parameters) take a
  number, `eks.keys({t("0s"): 1.0, t("2s"): 0.3}, ease="in_out_cubic")` (times clip-local), or a curve
  (`eks.curves.noise(...)`, `eks.curves.beat_pulse(...)`).
- `e.cue("logo", t("bar:4"))` names a time; later `t("cue:logo")` refers to it.
- Beats: `e.audio.track(song).beats("beat", "downbeat")` analyses the song and makes `beat:N`, `bar:N`,
  `downbeat:N` and `t.beats(...)` resolve; `@eks.edit` has no `bpm=` (a score or scene does).
- `e.gfx(scene, **inputs)` and `e.scene(scene, **inputs)` embed a scene's render as media (skills
  `eks-harness:eks-gfx-audio`, `eks-harness:eks-scenes`).

## Effects, transitions, easings, curves

`eks.fx` (35 effects, for example `zoom(1.0, 1.08, ease="out_cubic")`, `color_grade(lut=...)`, `blur(6.0)`,
`flash(t.beats("downbeat")[0:8], t("2f"))`, `speed(keys(...))`, `freeze(at, hold)`, `captions("speech")`,
`punch_in(zoom=1.15)`), `eks.tx` (22 transitions, `crossfade(t("500ms"))`, `beat_flash(t("8f"))`, ...),
`eks.ease` (20 easings), `eks.curves` (11 curves and 4 envelopes). Signatures: `references/catalog.md` and
`docs/reference/catalog.md`.

- A transition is B's `transition_in`, else A's `transition_out`; setting both on one cut is an error.
- Every random effect takes its seed from the edit's `seed` and the element path.
- `captions(source)` shows words of an `STTMarkers` analysis:
  `eks.media.markers(recording, eks.media.STTMarkers(name="speech", language="tr", model="small",
  device="cpu", compute_type="int8"))` (all three of model, device and compute type pin the result).
- Machine-learning effects (`matanyone_remove`, `birefnet_remove`, `sam3_track`, `auto_reframe`, `punch_in`
  with speaker tracking) need their optional packages and are keyed per device.

## Overlays on video

```python
e.track("notes", z=1).overlay(
    eks.overlay.title("Yeni fatura", subtitle="Kontör işlemleri", at=t.span(t("0s"), t("1500ms"))),
    eks.overlay.callout("#total", "Anlık kur", at=t.span(t("1500ms"), t("3s"))),
    eks.overlay.highlight(by.rect(120, 900, 840, 160), at=t.span(t("3s"), t("4500ms"))),
    eks.overlay.redact(by.test_id("customer-iban"), at=t.span(t("0s"), t("6s"))),
    eks.overlay.caption("Faturayı kaydedin", at=t.span(t("4500ms"), t("6s"))),
)
```

Element targets (`"#total"`, `by.text(...)`) anchor on the box map saved with a flow recording: the topmost
recording clip under the overlay's window. `by.point` and `by.rect` are timeline pixels. `emphasis="freeze"`
holds the recording frame during the window, `"slow"` plays it at half speed.

## Render settings

`e.render_settings(encoder=, quality="preview"|"draft"|"final", threads=, preset=, crf=, bitrate_kbps=,
pix_fmt=, color_primaries=, color_trc=, colorspace=)`. Software encoders (`libx264`, `libsvtav1`) with fixed
`threads` are byte-identical across runs; hardware encoders (`videotoolbox`, `nvenc`, `qsv`, `amf`) only when
named. Without `encoder`, a `final` render uses `libx264` and others the machine's hardware encoder.

## Review expectations

`e.expect.cut/beats/onset/loudness/text/motion/black/frozen/safe_area/template(...)` and
`e.expect.derive(cuts=, captions=, beats=, loudness=)` add media checks on the render (skill
`eks-harness:eks-checks`).

## Cookbook

`examples/cookbook/.harness/edits/` in the eks-harness repository holds 18 verified edits:
`annotated_walkthrough`, `beat_flash_montage`, `before_after_split`, `lyric_video`, `product_showcase`,
`reaction_overlay`, `slideshow_kenburns`, `speed_ramp_drop`, `storytelling_bg_music`, `podcast_clip`,
`titles`, `mix_only`, and analysis recipes `auto_reframe_vertical`, `captioned_tutorial`, `talking_head_meme`,
`chapter_markers_youtube`, `sponsor_skipper`, `multicam_switching`. Generate their fixtures first with
`uv run python examples/cookbook/.harness/assets/generate.py`; `docs/cookbook.md` describes each one.

## Known limits

`media.tts` needs a speech integration that is not installed (`resolution_unavailable`); `media.stems` and
`denoise` need Demucs and DeepFilterNet; `master.lra` is refused; gfx triggers on scene events cannot resolve
until scene executors exist.

The earlier video project API (`eks_harness.video`, `eks-harness video render`, studio) stays for existing
projects until scenes and scores move over; see skill `eks-harness:video-editing` only for those.
