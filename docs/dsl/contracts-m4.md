# M4 contracts: media

M4 gives every media op of the plan a real executor: media sources and analyses, the audio mix, 2D motion graphics,
the edit render, the screenshot and video overlay renderers and the media checks, and derives review checks from an
edit. The legacy `eks_harness.video` engine stays as the edit renderer's internals; its known bugs are fixed and its
output made deterministic. Exit check (design 16): the cookbook edits render byte-identical twice.

Builds on contracts-m0.md 2.9 and 2.10 (time resolution, clip-local time), 10.4 (media IR), contracts-m1.md 4.6, 9.2
to 9.4 (resolvers, cache keys, required resolution fields) and 19 (fakes keep serving `--fake` runs).

## 1. Module map

| Module | Owns |
| --- | --- |
| `engine/clock.py` | `run_time_context(plan, node, value_of)`: the run-time `TimeContext` of a node |
| `media/files.py` | media refs to local files, ffprobe, deterministic ffmpeg flags, output ingestion |
| `media/animate.py` | IR `AFloat`, `AColor`, `AVec2`, keys, easings and curves evaluated per frame or per sample with the IR semantics (contracts-m0 10.4) |
| `media/fonts.py` | strict font resolution: shipped families by weight, font files (BL-003) |
| `ops/media.py` | `media.source`, `media.markers`, `media.tts`, `media.stems` |
| `media/audio/` and `ops/audio.py` | the mix graph renderer and `audio.mix` |
| `media/gfx/` and `ops/gfx.py` | the Skia layer renderer (CPU reference and GPU), `gfx.render` |
| `media/edit/` and `ops/edit.py` | the frame compositor (timing, sources, effects, transitions), `edit.render` |
| `media/overlays/` and `ops/overlay.py` | the screenshot renderer (`overlay.annotate`), the video overlay renderer, box maps |
| `ops/check.py` | `check.media` over the review checks |
| `eks/editing.py` | `e.expect.derive(...)` |
| `examples/cookbook/` | the cookbook: edits, scenes, generated fixtures |

`ops.register_media_executors(registry, ...)` registers every M4 executor; `register_builtin_executors` calls it.

## 2. Executor context additions

* `call.time()` returns the node's run-time `TimeContext`: fps from the plan clock (or the timeline or scene), the
  beat and downbeat grids from `tempo.bpm` (static) or from the `media.markers` node named by `tempo.source`, cues
  resolved in that context, `markers` and `words` from every `media.markers` output the node depends on
  (`clock_dependencies`, already `after` edges), `start` 0 and `end` the clock duration. A missing provider value
  or an unresolvable time fails the node with `time_unresolved`.
* `call.media(value)` takes a resolved `RMedia` value (`MediaInfo` dump) and returns `MediaFile(path, info,
  sha256)`: artifacts through the store, `file:`, `plugin:`, `pkg:` and `http(s):` through the run's
  `ContentResolver` (which verifies pinned digests).
* `markers` output (shared by the real executor and the fake): `{source name: {stream: [time text, ...]}}` with
  times in canonical `Time` text of ticks; an STT source adds `{"words": [{"text", "start", "end"}, ...]}`.

## 3. Determinism rules for every media executor

* Every ffmpeg call uses `-nostdin -y -hide_banner`, `-fflags +bitexact`, `-flags:v +bitexact`, `-flags:a +bitexact`,
  `-map_metadata -1`, `-map_chapters -1` and an explicit `-threads N`. No hardware decode (`-hwaccel` is never
  passed). Containers are written without creation times.
* `edit.render` with a software encoder (`libx264`, `libsvtav1`, the IR's `SOFTWARE_ENCODERS`) and fixed `threads`
  is byte-identical on the same machine and ffmpeg build; the ffmpeg version and the encoder are in the resolution
  record and in the executor's runtime version. With `encoder` unset the executor picks `libx264` when the render
  is a `final` quality render, else the machine's hardware encoder, and reports it (BL-001).
* Randomness only from seeds derived from the timeline, scene or mix seed and the element path
  (`derived_seed(seed, "clip:track/0/effect:2")`); frame indices handed to effects are absolute timeline frames.
* Audio is mixed in float64 and written as 24-bit PCM WAV; no dithering noise.
* gfx CPU renders (`cpu=True`) are byte-identical everywhere the same skia-python version runs; GPU renders are
  keyed by `gpu.adapter` and `gpu.driver` (contracts-m1 9.4) and render a clip on one machine.
* A pinned backend, model, device, engine or encoder that is not available fails the node with
  `resolution_unavailable`; nothing falls back (contracts-m1 4.6).

## 4. Media sources and analyses (`ops/media.py`)

* `media.source`: resolves the file (section 2), probes it with ffprobe (`kind`, `duration` as `Time`, `width`,
  `height`, `fps`) and returns `MediaInfo` with `artifact` set for artifacts. A `renderer` names an `@eks.renderer`
  media kind whose implementation turns the file into frames; M4 calls it with the file and returns its output.
  `kind=file` and `bundle` skip probing.
* `media.markers`: runs each marker source with the legacy analysers (`video/plugins/builtin/markers`): beat tracker
  (`librosa`, `madmom`, `beatnet` backends), onset, energy, silence, scene, momentum, motion, face and STT. Output as
  in section 2. Resolution: `markers.backend`, `markers.version` and, for STT, `stt.model`, `stt.device`,
  `stt.compute_type`, `stt.backend`, `stt.version`.
* `media.tts` and `media.stems`: the legacy kokoro and demucs integrations when installed; otherwise
  `resolution_unavailable` naming the extra to install (`eks-harness[tts]`, `eks-harness[stems]`).

## 5. Audio mix (`audio.mix`)

The renderer decodes each track to float64 at `Mix.sample_rate` and `Mix.channels`, then per track: source span
(`src`), placement at `start`, `stems` (through `media.stems` semantics), `denoise`, EQ (RBJ biquads, `gain_db`
animated per 10 ms block), compressor (feed-forward, RMS detector with attack and release), fades, reverb (a
seeded Freeverb network), gain (dB, animated per sample) and pan (equal power, animated). Ducks: the ducked track's
gain is reduced by `amount_db` while the RMS envelope of the `under` track is above `threshold_db`, with attack and
release. The master sums the tracks, applies `gain`, normalises integrated loudness (ITU-R BS.1770-4 with K weighting
and gating) to `loudness`, then a true-peak limiter (4x oversampling) at `true_peak`. `duration` cuts or pads with
silence. Output: WAV 24-bit, kind `audio`. The executor reports the measured integrated loudness and true peak in
its detail.

## 6. Motion graphics (`gfx.render`)

* Renderer: skia-python. `cpu=True` draws on raster surfaces (the reference); otherwise Skia's GL backend on an
  offscreen GL context: CGL on macOS (OpenGL over Metal; skia-python does not expose a Metal backend context), EGL on
  Linux. Rendering runs in a child process
  (`python -m eks_harness.media.gfx.child job.json`) so GPU state and crashes stay out of the hub.
* Output: QuickTime `.mov` with the `png` codec (RGBA, transparent where `background` is unset), the scene's `fps`
  (or the embedding clock's) and duration (or the last layer's end).
* Layers per contracts-m0 10.5: text (font resolved strictly, section 9), rect, ellipse, path (SVG path data,
  `trim_start`/`trim_end`), image (raster or SVG through `skia.SVGDOM`), video (decoded frames, clip-local `src` and
  `speed`), group; transforms `x`, `y`, `scale`, `rotation` about `anchor`; `opacity`; blend modes; masks; shadow;
  blur. Triggers: `Pulse`, `SetProp`, `Animate` at resolved `Time`/`TimeSet`/event times.
* IR additions (all optional, defaults keep current plans' hashes): `fill` and `stroke` accept a `Gradient`
  (`linear` with `start`, `end` points, or `radial` with `center` and `radius`, and `stops` of `(offset, color)`);
  `mask_mode`: `alpha` (default), `luminance`, `alpha_inverted`, `luminance_inverted`; blend modes add `src_atop`,
  `dst_in`, `dst_out`, `xor`, `darken`, `lighten`, `color_dodge`, `color_burn`, `soft_light`, `hard_light`,
  `difference`, `exclusion`; easings add `in_out_quad` variants missing so far: `in_expo`, `out_expo`, `in_out_expo`,
  `in_sine`, `out_sine`, `in_out_sine`, `in_quart`, `out_quart`, `in_out_quart`.
* Resolution: `gpu.adapter`, `gpu.driver` (GPU renders); executor `fonts=True`.

## 7. Edit render (`edit.render`)

The legacy orchestrator cannot express the IR's clip transforms (`x`, `y`, `scale`, `rotation`) and its time model is
where the known bugs live, so `edit.render` is a new frame compositor (`media/edit/`). It reuses the legacy effect
plugins only for the five machine-learning effects; every other effect is implemented on the IR models
(`media/edit/effects.py`).

* Timing (`media/edit/timing.py`): every clip time is resolved in ticks with `call.time()`. A clip covers the
  timeline frames from its `start` until its source span is used up at its `speed` (animated speed is integrated per
  frame); `speed_ramp` and `freeze` effects change that source time map. An image clip without `src` lasts until the
  next clip of its track starts, else to the timeline end. The timeline lasts `duration`, else to the last clip end.
* Sources (`media/edit/sources.py`): video clips decode with ffmpeg at the source's own rate, scaled to the clip's fit
  box, read sequentially; each timeline frame takes the source frame at `floor(source_seconds * source_fps)`. Images
  decode once. gfx and Blender clips are their render outputs (RGBA).
* Per clip and frame: source frame, then effects in order (absolute frame index, seeds
  `derived_seed(timeline.seed, "<clip id>/effect:<n>")`), then the clip transform (`fit` box, `x`, `y`, `scale` about
  the frame centre, `rotation`) and `opacity`.
* Transitions: for consecutive clips A and B of a track, the transition is B's `transition_in`, else A's
  `transition_out` (both set is an error naming the clips). It runs over `[B.start, B.start + duration]`; the
  outgoing image is A's frame at that time, or A's last frame once A has ended. A `transition_out` with no next clip
  goes to what lies below the track over `[A.end - duration, A.end]`; a `transition_in` with no previous clip comes
  from it over `[B.start, B.start + duration]`. All 22 kinds are implemented (`media/edit/transitions.py`); seeded
  ones use `derived_seed(timeline.seed, "<clip id>/transition")`.
* Compositing: Skia raster (deterministic): background colour, then tracks by `z` (then declaration order), each
  track drawn into a layer with its animated `opacity` and `blend`; video overlays (section 8) render into their own
  layer above their track.
* Encoding: RGBA frames piped to ffmpeg, `libx264` or `libsvtav1` with the `RenderSettings` (preset, crf or
  bitrate, threads, colour tags, pixel format), AAC audio from `Timeline.audio` plus the clips with `audio=True`
  (decoded, gained per sample and summed in float64), all with the bitexact flags of section 3. Hardware encoders
  are used only when named; the resolution record reports `encoder` and `encoder_version`.
* Legacy bugs of design 10.2: the new compositor resolves every time once in ticks (absolute versus segment-local,
  primary track `start`, `BeatRef.every`), evaluates every animated value with `media/animate.py` (easing on every
  path, every curve kind), derives every seed from the timeline seed and the element path, applies every
  `RenderSettings` field, and is cached by the engine's node cache key (contracts-m1 9.2: node hash, content
  digests, executor and runtime versions, fonts). The legacy orchestrator keeps serving the legacy video API until M7
  removes it.
* Output: MP4, kind `video`.

## 8. Overlay renderers

* Box maps: `capture.shot`, `check.checkpoint` shots and recordings save a box map with the image: every visible
  element with a test id, role and accessible name, label, placeholder or own text, with its box in image pixels
  (`{"schema": "eks/boxes@1", "size": [w, h], "scale": dpr, "nodes": [{"testId", "role", "name", "label",
  "placeholder", "text", "kind", "box": [x, y, w, h], "parent": index}]}`). Web: a DOM walk in the page; mobile: the
  bridge tree; native surfaces: the uiautomator or idb dump. Recordings save one box map per step with its time
  (`boxes.json` beside the interactions sidecar). Box maps are artifact sidecars (`meta.boxes` names the sidecar
  artifact).
* Target resolution on a box map (`media/overlays/targets.py`) follows the M2 rules: strict matching, `within`,
  `near`, `nth`; `point` and `rect` targets are image coordinates; `css` is `target_unsupported` (no DOM).
* Screenshot renderer (`overlay.annotate`): resolves anchors on the artifact's box map, draws the overlays with the
  annotate SVG renderer and the overlay style, writes a PNG artifact with `meta.annotation` (overlays, style,
  versions). `title` is refused on screenshots (M0 decision). Fonts strict (section 9).
* Video renderer: overlays of an edit track draw over the composite in their `window` (or `hold` from their start).
  Anchors resolve on the box map of the topmost recording clip visible at the window start, at the last step at or
  before the clip-source time, mapped through the clip's fit and transform; point and rect targets are timeline
  frame coordinates. Each overlay is rasterised once (SVG) and composited per frame with a 150 ms fade in and out.
  `emphasis="freeze"` holds the underlying recording frame during the window; `slow` plays it at half speed for the
  window and the clip's remaining content keeps its timeline position (the window shows the first half of its
  source span).

## 9. Fonts (BL-003)

`media/fonts.py` resolves `(family, weight)` strictly: shipped `IBM Plex Sans` (400, 600) and `IBM Plex Mono` (400)
from `annotate/fonts`; a font file (`RMedia` of kind `file`) is used as is (its weight is the file's). Any other
weight of a shipped family fails the node with `font_unavailable` listing the weights there are. Executors that draw
text set `fonts=True`, so the fonts digest is in their cache keys.

## 10. Media checks (`check.media`)

The executor runs the legacy review checks on the media file: `cut`, `beats` (the `stream` resolved to times through
`call.time()`), `onset`, `loudness`, `text`, `motion`, `black`, `frozen`, `safe_area`, `template` (legacy `visual`),
with a parameter adapter per kind; `at` and spans are resolved through `call.time()`, `tolerance` becomes frames.
Output `{"ok", "kind", "at", "detail"}`. A failed check fails the node with `assertion` (soft when `soft=True`).
Resolution for `text` and `safe_area`: `engine` and `version` (contracts-m1 9.4), mirrored as `ocr.engine`, `ocr.version`.

## 11. Review checks derived from the edit

`e.expect.derive(cuts=True, captions=True, beats=True, loudness=True)` queues, at finalize:

* `cut` at every boundary between consecutive clips of the lowest-z track joined by a hard cut;
* `text` at the middle of every `caption` and `title` overlay window with its text, `soft=True` (OCR is a hint);
* `beats` over the cut times when any clip start is anchored to `beat`, `downbeat` or `bar`;
* `loudness` with the master's `loudness` and `true_peak` when the edit has an audio mix with a master target.

Derived checks carry `label="derived:<kind>:<n>"` so they read apart from hand-written ones.

How the rules read the timeline (decided while implementing; the list above leaves them open):

* A cut is at the next clip's `start`, in clip order. Two clips of the same media whose `src` spans touch are one
  continuous shot and give no cut; two clips with the same `start` give none either.
* The middle of a window is one `Time` when both ends have the same anchor (or the same `beat`, `bar`, `downbeat`
  or `frame` anchor kind); otherwise the text check covers the whole window. A window that needs the clock bounds
  (`end`) is skipped when the edit has neither a duration nor a clock window, since the check could not resolve it
  and the plan would fail validation. Overlays without a window, and those whose text is a `$ref` or `$fmt`, are
  skipped.
* The beats check uses the `beat` stream over the span from the first to the last cut.

## 12. Cookbook and exit check

`examples/cookbook/.harness/` holds new-DSL edits ported from the legacy recipes, gfx scenes and a mix:
`beat_flash_montage`, `before_after_split`, `lyric_video`, `product_showcase`, `reaction_overlay`,
`slideshow_kenburns`, `speed_ramp_drop`, `storytelling_bg_music`, `podcast_clip`, `annotated_walkthrough`, `titles`,
`mix_only`, and the analysis recipes `auto_reframe_vertical` (mediapipe), `captioned_tutorial` and `talking_head_meme`
(faster-whisper), `chapter_markers_youtube` (scenedetect), `sponsor_skipper` and `multicam_switching` (librosa).
Fixtures are generated into `.harness/assets/` by `.harness/assets/generate.py` from ffmpeg sources and numpy with
bitexact flags (gitignored; a test regenerates them and compares bytes). A recipe whose analysis package is missing
fails with `resolution_unavailable` naming it.

Exit check: every cookbook edit renders twice with the cache off and the two outputs have equal SHA-256, in tests
(`tests/media/test_cookbook.py`: a fast subset by default, all edits with `EKS_COOKBOOK_FULL=1`) and on a temp hub.
