---
name: video-editing
description: Use this skill when authoring or editing eks-harness video projects (eks_harness.video Project files) for short-form video edits - beat-synced cuts, captioned talking heads, motion graphics, Blender scenes, TikTok/Reels/Shorts deliverables - or when rendering, publishing or inspecting them through the eks-harness video tools, CLI or studio.
---

# Video editing with eks-harness

`eks_harness.video` is the declarative, IR-driven video engine of eks-harness. A
project is a single `Project` pydantic model that describes a timeline
(tracks, segments, effects, audio, markers, render settings). The renderer
inspects the IR, decides which effects can fold into an ffmpeg filter
graph and which must run on the per-frame Python pipeline, and produces an
mp4. Nothing in the IR is executed at author time - it is pure data, JSON-
round-trippable, and validated by pydantic before any rendering happens.

When a user asks for a video edit, a render or a change to a video
project, write or modify a Python file (`project.py`) that defines a
top-level `project = Project(...)` value. Every constructor argument is
type-checked; the schema is the authoritative contract.

## How to think in the video engine

There are five concepts to internalise before you can move quickly:

1. **TimeRefs are symbolic.** Never hard-code "frame 480" - use
   `Seconds(t=16.0)`, `Frames(n=480)`, `BeatRef(stream="kick", every=4)`,
   `WordRef(text="hello")` or `MarkerRef(name="intro")`. The renderer
   resolves them once markers have been extracted. A compact string
   shorthand is also accepted: `"0:16.5"`, `"f:480"`, `"b:kick:4"`,
   `"w:hello"`, `"m:intro"`.
2. **Animated values are a sum type.** `Animated[T]` accepts a constant
   `T`, a list of `Keyframe[T]`, *or* a `Curve`. The curve family is
   `BeatPulse`, `ExpDecay`, `Sine`, `LFO`, `Lambda`, `Spring` (mass /
   stiffness / damping critically-damped overshoot), `Noise` (seeded
   value noise with optional `hold`), `RandomChoice` (seeded discrete
   pick from a value pool), `Step` (piecewise-constant), `Bezier` (cubic
   handle interpolation) and `Bounce` (decaying elastic). All curves are
   valid in any `Animated[float]` slot. Reach for `BeatPulse` when you
   want a per-beat reaction; reach for `Noise` when you want subtle
   organic jitter without enumerating keyframes. `BeatPulse` shapes each
   hit with an `envelope` - `ExpDecayEnv` (default decay), `LinearEnv`
   (rise/fall), `BoxEnv` (flat window), or `EasingEnv` (a duration-bounded
   window shaped by any `Easing`, e.g. `EasingEnv(duration=0.25,
   easing=ElasticOut())` for a 250 ms elastic pop on every beat).
3. **Markers are declared, not invoked.** Marker sources on
   `Project.markers` declare *what* to extract; named streams become
   addressable via `BeatRef` / `WordRef` / `MarkerRef`. The full
   catalogue is `BeatTracker`, `STTMarkers`, `SceneMarkers`,
   `SilenceMarkers`, `OnsetMarkers`, `EnergyMarkers`, `MomentumMarkers`,
   `FaceMarkers` and `MotionMarkers`. `MomentumMarkers(name="m", ...)`
   reads the song's structure: it publishes `m.drop` (where kick and bass
   arrive after a build), `m.build` (the valley the rise starts from),
   `m.break`, `m.section` and `m.peak`, and `curve_out` writes the sampled
   momentum curve with every event's strength as JSON for timeline
   planning; `MarkerRef(name="m.drop", index=0)` lands on the first drop.
   Audio-source markers (silence / onsets / energy / momentum)
   resolve from `audio_tracks[N]` like `BeatTracker`; video-source
   markers (faces / motion) resolve from `tracks[N].segments[M]` like
   `SceneMarkers`. The heavier backends (`librosa`, `madmom`, `cv2`,
   `scipy`) are lazy-imported - missing deps degrade to an empty stream
   rather than a crash. Validation fails fast if a TimeRef references a
   stream nobody declared. `BeatTracker` runs a `beatnet → madmom →
   librosa → stub` chain by default; set `backend="madmom"` (or
   `"librosa"`) to pin a specific extractor for reproducibility.
4. **Effects compose top-to-bottom.** Each `Segment.effects` is a list
   that runs in order. Mixing ffmpeg-foldable effects (`Brightness`,
   `Crop`, `Mirror`, plus the filter family `Blur`, `Sharpen`,
   `Vignette`, `FilmGrain`, `LUT`, `Invert`, `Posterize`) with
   frame-pipeline effects (`RGBSplit`, `Captions`, `MatAnyoneRemove`,
   `BiRefNetRemove`, `SAM3Track`) is fine - the orchestrator splits
   the pipeline at the right boundary. Each effect declares its
   `compile_targets`. Overlay effects (`TextOverlay`, `LowerThird`,
   `Watermark`, `StickerOverlay`) composite on top of whatever frame is
   at that point in the chain; place them after colour/filter work so
   they aren't tinted by it.
5. **Tracks composite by `z`.** Higher `z` is on top. The lowest-`z`
   track is the base: its segments are concatenated and its transitions
   apply. Every other track is an overlay whose segments sit at their own
   `start` on a transparent canvas; overlay media must carry alpha
   (`ImageFile` PNG, `VideoFile` ProRes 4444 / PNG MOV / VP9-alpha WebM,
   `Solid` with alpha), overlay effects must be graph-compilable (a
   frame-pipeline effect would flatten the alpha and raises), and overlay
   segments use `Cut` transitions. `Track.blend` (`normal`, `add`,
   `multiply`, `screen`, `overlay`) blends only where the layer is opaque,
   and `Track.opacity` takes a constant or keyframes. Effects that drop
   alpha (`MatAnyoneRemove`, `BiRefNetRemove`, `ChromaKey`, `SAM3Track`)
   only do useful work when there is something underneath to show
   through. Prefer `MatAnyoneRemove` for video (temporal consistency,
   SOTA hair/edge quality) and `BiRefNetRemove` for per-frame stills.

## Effect catalogue

The full effect set, grouped by purpose. `API_REFERENCE.md` is
authoritative for fields and defaults; this is the "what exists and when
to reach for it" map. Effects that fold into ffmpeg are cheap; those
forced onto the `frame_pipeline` are per-frame Python and cost more.

- **Colour / tone** (foldable) - `Brightness`, `Contrast`, `Saturation`,
  `ColorGrade` (exposure + `.cube` LUT in one), `LUT`, `Invert`,
  `Posterize`.
- **Filter / texture** (foldable) - `Blur`, `Sharpen`, `Vignette`,
  `FilmGrain`.
- **Geometry / motion** - `Crop`, `Mirror`, `Pan` (foldable), `Zoom`,
  `Freeze`, `SpeedRamp` (time-remap; anchor the boundaries to a beat
  stream).
- **Reframe / tracking** (frame-pipeline) - `AutoReframe` (landscape →
  vertical with a face/saliency tracker), `PunchIn` (auto speaker
  zoom-in).
- **Stylised / glitch** (frame-pipeline) - `RGBSplit`, `Glitch`, `Shake`,
  `VHS`, `Datamosh`. These are the "music-video energy" family; combine
  with `BeatPulse`-driven `Animated` intensities for beat-reactive grime.
- **Flash / fade** - `Flash` (white/colour hit, usually on a `BeatRef`),
  `Fade` (in/out at a segment edge).
- **Keying / matte** - `ChromaKey` (green screen), plus the alpha-drop
  mattes `MatAnyoneRemove`, `BiRefNetRemove`, `SAM3Track` (see concept 5).
- **Overlays** (composite on top; place after colour/filter work) -
  `TextOverlay`, `LowerThird`, `Watermark`, `StickerOverlay`, `Captions`.

## New media kinds: HTMLOverlay and SeparatedStem

Two non-file `MediaSource` variants sit alongside `VideoFile` /
`ImageFile` / `AudioFile`. `HTMLOverlay(template=..., style_vars=...,
viewport=..., transparent=True, marker_animations=[...])` renders an
HTML+CSS template via headless Chromium with a virtual clock, capturing
one frame per project frame. Templates are pure HTML and CSS - **no
JavaScript is permitted**; sync to project markers is done by passing
`MarkerAnimation(stream=, selector=, animation_name=, duration=,
offset_ms=)` entries, which the renderer compiles into baked
`@keyframes` rules at each trigger time. `SeparatedStem(source=
AudioFile(...), stems=["vocals"], model="htdemucs")` lifts vocals,
drums, bass or other from a mixed track via Demucs; assign it to
`AudioSegment.media` to use the stem as if it were a normal audio file.
The renderer runs Demucs once per `(source, model)` pair and caches all
four stems. Both are lazy-imported optional extras (`eks-harness[html]` +
`playwright install chromium`, `eks-harness[stems]`).

## Plugin media renderers: BlenderScene

A `media_renderer` plugin contribution (in a `harness-plugin.toml`, see the
plugin development skill) adds its own media kind to `MediaSource`; before planning, every segment of
that kind is rendered once into the cache and swapped for a `VideoFile`, so
it works on base and overlay tracks with effects, speed and transitions.
`BlenderScene(blend=..., script=..., params={...}, inputs=[...],
engine=, samples=, device="AUTO", transparent=True, workers=,
worker_env=[{"CUDA_VISIBLE_DEVICES": "0"}, ...])` from
`eks_harness.video.plugins.builtin.media.blender` renders a Blender scene headless:
the scene gets the project fps and resolution, scene frame `frame_start`
(default 1) is source time 0, and the segment's `in`/`out` pick the window.
The bpy script can `import eks_harness.blender as vdb` and key animation to the
music with `vdb.frames("beat")`, `vdb.named("drop")`, `vdb.words()`,
`vdb.frame_at_project(t)` and `vdb.params`; marker times are mapped through
the segment's start, `in` and constant speed, so a key lands exactly on its
project time. Output is ProRes 4444 with alpha; renders resume and are cached
by the contents of the blend file, script and `inputs`, the parameters,
timing, markers and Blender version. Blender is optional twice over: the
built-in `eks.blender` plugin is off by default (enable it per repository in
`.harness/project.toml` with `[plugins] enable = ["eks.blender"]`, or for every
project with the `plugins.enabled` setting), and the project opts in with
`Project(plugins=[PluginRequirement(name="blender")])`. Only then is Blender
4.2+ looked up (the plugin's `executable` setting, `EKS_HARNESS_BLENDER`, then
`PATH`), and a render fails up front with an install hint when it is missing.
Projects that do not use it never touch Blender; no Python packages are needed
either way.

`BlenderScene(media={"screen": <any media>})` renders other media first for the
same window (a file path or any media kind, e.g. a live app capture from a
repo-local plugin) and hands the script local paths as `vdb.media[name]`;
movies get a `.framemd5` sidecar so the frame cache tracks the exact frame
shown. Frames are kept per segment with a scene fingerprint each and frames
whose scene did not change are reused (`frame_cache=True`;
`EKS_HARNESS_BLENDER_FRAME_CACHE=0` forces every frame).

## Render farms (`eks_harness.video.farm`)

Media renderers spread their work over a farm when one is configured; nothing
machine-specific lives in code or projects. The config is a TOML file, first
match wins: `$EKS_HARNESS_FARM_CONFIG`, `<workspace>/.harness/farm.toml`,
`<eks-harness config dir>/farm.toml` (`EKS_HARNESS_FARM=0` disables). `[local]`
(`enabled`, `slots`, `workers_per_slot`, `env`, `tools`) plus any number of
`[[remote]]` hosts (`host` as an ssh destination, `root`, `slots` with per-slot
`env` such as `CUDA_VISIBLE_DEVICES`, `workers_per_slot`, `env`, `tools` like
`blender = "~/opt/blender/blender"`). Remote workers need key-based ssh and
rsync; inputs are mirrored under `<root>/abs/<absolute path>`, workers pull
frames one at a time over stdin (fast machines take more), results come back
by rsync. `BlenderScene` uses it automatically (`farm=False` opts out,
`sync_exclude` trims what is mirrored, `EKS_HARNESS_CYCLES_DEVICE` in a slot's
`env` pins the Cycles backend). Plugins use it through `farm.run(FarmJob(...),
farm.load(workspace))`.

## Publishing renders and studio links

`eks-harness studio publish <output> --project <dir>` (or
`eks_harness.studio.publish.publish(...)`) links a render made outside the
studio into `<project>/renders/<job_id>/` with metadata and a thumbnail,
uploads it to the harness artifact store (harness project from the repo's
`.harness/project.toml` `[video] project`, else `[project] id`, else
`video/<project>`; session = the project folder name) and prints the studio
deep link `<harness>/studio?project=<name>&render=<job_id>` plus the
artifact's raw link. `--no-upload` only lists it in the studio. Studio
projects live in the `video.workspace` setting (default `<data dir>/video`).
Always give the user the printed links after a render.

## Audio effects

Audio effects are typed optional fields on `AudioSegment` and
`AudioTrack`, not entries in a discriminated union. They attach the
same way `eq` and `sidechain` already do.

- **`Ducking`** (segment-level) - sidechain-style level attenuation
  driven by a reference track. Use when a single segment should duck
  under voiceover but the rest of the track should not.
- **`LoudnessNormalize`** (track-level, field `loudness`) - EBU R128 /
  loudnorm pass that targets a specific `target_lufs` (default `-16.0`),
  `true_peak_db` and `lra`. Apply to the master music track for delivery.
- **`AudioFade`** (segment-level, field `fade`) - `fade_in` / `fade_out`
  durations. Preferred over hand-keyframed gain when you only want
  curtain edges.
- **`EQEffect` + `EQBand`** (track-level, field `parametric_eq`) -
  multi-band parametric EQ with `peak` / `low_shelf` / `high_shelf`
  bands. Supersedes the 3-band `eq` slot when set; each band's `gain_db`
  is animatable.
- **`Reverb`** (segment-level, field `reverb`) - algorithmic reverb via a
  tuned `aecho` tap chain. `wet` (animatable mix), `room_size` and
  `damping` are the knobs; everything else has sensible defaults.
- **`DeepFilterDenoise`** (segment-level, field `denoise`) - DeepFilterNet 3
  noise removal run as a pre-pass on the segment's input audio, cached on
  disk by `(source, mtime, params)`. Lazy-loaded; needs
  `pip install eks-harness[denoise]`. Reach for it on noisy field-recorded VO
  before ducking/EQ.

The existing `SidechainConfig` (on `AudioTrack`) is unchanged. `Ducking`
is the per-segment counterpart for cases where sidechaining the whole
track is too coarse.

## Transitions

Each `Segment` carries `transition_in` and `transition_out` (both default
to `Cut`, which adds zero frames). A timed transition overlaps the
adjacent segment by its `duration` seconds, so budget for it when placing
segment `start` times. `Track.transitions` is the list-form alternative
for declaring boundaries at the track level. Full fields/defaults live in
`API_REFERENCE.md`; this is the selection map:

- **Hard / plain** - `Cut` (default), `Crossfade`, `DipToBlack`,
  `DipToWhite`.
- **Directional wipes / slides** - `Wipe` (hard edge), `LumaWipe`
  (feathered gradient edge), `Slide`, `Push`. All take a
  `direction` of `left` / `right` / `up` / `down`.
- **Shape / pattern reveals** (ffmpeg `xfade` presets) - `Iris`
  (`mode="open"`/`"close"`), `Radial`, `Pixelize`, `Dissolve`,
  `BlurThrough`, `FadeGrays`, `ZoomTransition` (zoom-blur push; `kind`
  is `"zoom"`).
- **Stylised** (xfade plus a gated filter, `intensity` 0–1) -
  `GlitchTransition` (blocky pixelize + chroma split), `RGBShiftWipe`
  (directional wipe + chromatic aberration), `WhipPan` (motion-blurred
  slide), `LightLeak` (brightness bloom), `FilmBurn` (warm bloom + chroma
  bleed).
- **Beat-reactive** - `BeatFlash` and `BeatGlitch` flash / glitch on every
  beat of `stream` (default `"kick"`) that falls inside the transition
  window; the renderer threads window-local beat times from the resolved
  markers, falling back to one pulse at mid-window if none land. Declare
  the matching `BeatTracker` stream so they have beats to react to.
- **Plugin** - `PluginTransition(name=..., params={...})` defers to a
  registered transition plugin by name.

## Randomness and reproducibility

`Project.random_seed: int = 0` is the global salt. Every node that
samples randomness (`Noise`, `RandomChoice`, `RandomTrigger`) combines
its own `seed` with this salt, so a project rendered twice with the
same `random_seed` produces byte-identical output. Pin both the
project-level seed and the per-node seeds when reproducibility matters
(QA renders, regression tests, A/B variants).

`eks_harness.video.ir.randomness` also defines two trigger primitives:

- **`RandomTrigger(seed, rate_hz, jitter)`** - a stochastic event
  stream, useful for "flash sometimes" rather than "flash every beat".
- **`EveryNth(source, n, offset, jitter, seed)`** - every-Nth picker
  over an existing reference stream (`BeatRef` / `WordRef` /
  `MarkerRef`), with optional jitter.

Both round-trip through JSON and validate. They are not yet wired into
any effect field - until orchestrator support lands, park them under
`Project.metadata` (see `eks_harness/video/cookbook/multicam_switching.py` for the
forward-looking shape).

## Canonical edit patterns

- **Beat-synced flash on every kick.** Declare a `BeatTracker(streams=["kick"])`
  on `Project.markers`. Add a `Flash` effect with `at=BeatRef(stream="kick", range=...)`
  and `curve=BeatPulse(trigger=BeatRef(stream="kick"), envelope=ExpDecayEnv(tau=0.08), intensity_ramp=0.6)`.
  See `eks_harness/video/cookbook/beat_flash_montage.py`.
- **TikTok-style burned-in captions.** Declare an `STTMarkers` source, then
  add `Captions(source="<marker_name>", style="tiktok", max_words_per_card=3)`
  to the talking-head segment. See `eks_harness/video/cookbook/talking_head_meme.py`.
- **Landscape → vertical reframe.** One segment with `AutoReframe(target_aspect=(9, 16), tracker="face")`
  in its effect chain, plus a `tiktok-1080-h264` render preset. See
  `eks_harness/video/cookbook/auto_reframe_vertical.py`.
- **Speed ramp on a music drop.** Use `SpeedRamp` with keyframes; anchor
  the ramp boundaries to the kick or downbeat stream from a `BeatTracker`.
  See `eks_harness/video/cookbook/speed_ramp_drop.py`.
- **Sidechain ducking.** Add `SidechainConfig(source="<vo_track_name>")`
  to the music `AudioTrack`. See `eks_harness/video/cookbook/storytelling_bg_music.py`.
- **Talking-head name strap.** `LowerThird(title="@handle", subtitle="...",
  position="left")` on the talking-head segment gives a broadcast-style
  chyron without burning the text into the source. Pair with `TextOverlay`
  for headline cards anchored to `BeatRef` / `MarkerRef`.
- **Brand watermark or sticker.** `Watermark(path="assets/logo.png", x=..., y=...)`
  for a persistent corner mark; `StickerOverlay` for a timed reaction
  graphic that appears for `duration` seconds at a `start` TimeRef.
- **Loudness-normalised delivery.** Set `LoudnessNormalize(target_lufs=-14.0,
  true_peak_db=-1.5)` as the master `AudioTrack.loudness` to hit
  YouTube/TikTok loudness targets in one pass (the default `target_lufs`
  is `-16.0`). Pair with `Ducking` on the music segments under voiceover,
  not on the whole track, when only specific regions should duck.
- **Organic randomness without keyframes.** `Animated[float](root=Noise(
  seed=42, min_value=-0.08, max_value=0.08, hold=0.25))` on `Brightness`
  gives a flickering "energy" feel without spelling out every value.
  See `eks_harness/video/cookbook/lyric_video.py`.

## Hardware acceleration (encode / decode / ML)

The renderer hardware-accelerates by default; nothing in the IR opts in.

- **ffmpeg HW encode/decode.** `eks_harness.video.render.hwaccel` auto-selects the
  best video encoder for the host (AMD **AMF** `h264_amf`, NVIDIA NVENC,
  Intel QSV, Apple VideoToolbox) and decodes with `-hwaccel auto`. Every
  render path - segments, transitions, HTML overlay, dev-server previews -
  HW-encodes. A new `amf` encoder plugin backs the AMD path.
- **Chromium HTML overlay → GPU.** `HTMLOverlay` rasterises with the
  ANGLE/D3D11 GPU path (`--use-angle=d3d11 --enable-gpu-rasterization`).
- **ONNX GPU/NPU via Windows ML.** `eks_harness.video.render.winml_ep` best-effort
  downloads + registers the AMD **MIGraphX** execution provider through
  Windows ML on Windows, so rembg / BiRefNet matting and the
  `winml-whisper` captioner run on the AMD GPU. Falls back to CPU when
  unavailable (it never uses DirectML - that OOMs the big models on
  shared-VRAM APUs).
- **Kill switch.** Set env var `EKS_HARNESS_HWACCEL=0` to force software
  libx264 + no `-hwaccel` + software Chromium. Use it for reproducible,
  byte-identical output across machines.

For burned-in captions, `winml-whisper` (ONNX Whisper on the AMD GPU via
the WinML MIGraphX EP) is auto-selected first on Windows, ahead of the
CPU-only `faster-whisper`. See `API_REFERENCE.md` for the captioner
backends.

## When to reach for which TimeRef

- `Seconds(t=...)` / `"0:16.5"` - fixed timestamps, intros/outros,
  declarative segment bounds.
- `Frames(n=...)` / `"f:480"` - when you genuinely want frame precision
  (rare; usually only inside one-frame holds).
- `BeatRef(stream=..., every=N)` - anything that should react to musical
  pulse. `every=4` lands on every fourth beat (downbeat-feel).
- `WordRef(text=..., source=...)` / `WordRef(index=...)` - caption
  emphasis, lyric flashes, "punch the word".
- `MarkerRef(name=..., index=...)` - sentence boundaries, scene cuts,
  any custom stream a `MarkerSource` produces.

## Video tools over MCP

The eks-harness MCP server exposes the video tools; they take a
`project_path` (a project folder, `project.py` or `project.json`, absolute or
relative to the studio workspace):

- **`render_project`** returns a job reference at once and reports progress;
  follow it on `eks-harness://video/render_jobs/<id>` and stop it with
  `cancel_job`. `mode="preview"` uses light quality profiles, `mode="final"`
  produces the deliverable.
- **Validation / inspection** - `validate_project` (pydantic and cross-field
  checks); `inspect_project` (compile-split summary: which effects fold into
  ffmpeg vs. which are forced onto the frame pipeline, useful for "why is my
  render slow").
- **Mutation** - `add_segment`, `add_effect`, `set_property`. These edit
  `project.json` directly, not `project.py`; round-trip through pydantic so
  an invalid edit is rejected before write.
- **Marker extraction** - `extract_markers` runs the declared marker sources
  and returns the resolved streams for inspection.
- **Catalogue** - `list_effects` enumerates every effect kind the active
  plugins provide, built-in and repository plugins alike.

Resources under `eks-harness://video/...` serve templates, project snapshots
and sources, effect schemas, easing previews, media library listings, plugin
metadata, encoding presets, render job status and preview frames.

## What lives where

- `API_REFERENCE.md` - every IR type with its fields, defaults and a one-
  line code snippet. Generated from the live SDK; treat as authoritative.
- `EXAMPLES.md` - twenty-one worked examples covering the full IR with
  explanations of *why* each pattern is structured that way (the first
  fifteen have runnable cookbook companions; the rest are inline).
- `RECIPES.md` - use-case index ("sync flashes to beats", "loudness-
  normalise for delivery"). Start here when you know what you want to
  build but not which cookbook script demonstrates it.
- the cookbook (`eks_harness/video/cookbook/*.py` in the installed package) -
  the same fifteen examples as runnable Project files. Run
  `eks-harness video init <name> <dest>` to scaffold one and diverge from it.
- `TROUBLESHOOTING.md` - concrete fixes for the recurring failure modes
  (missing ffmpeg, NVENC session limit, caption-source typos, frame OOM
  on 4K, Pillow / cv2 missing, AV1 slowness, …).
- `MIGRATIONS.md` - schema-version policy and the shape of future
  migration scripts. Currently `schema_version = "0.1"` and everything
  is additive.
- `schema/master.schema.json` - the frozen JSON Schema snapshot. Useful
  if the user wants to validate a `project.json` outside Python.

## Working defaults to suggest

Unless the user says otherwise, a fresh short-form project should use
`fps=30`, `resolution=(1080, 1920)`, the `tiktok-1080-h264` preset, and
the `libx264` encoder (`nvenc`/`videotoolbox` only when the user has
confirmed the host has them). Always set `Project.duration` explicitly -
it is required and validated against segment placement.

When a user hands you a vague brief ("make a 30-second meme clip"),
prefer scaffolding `eks-harness video init talking_head_meme <dest>` and editing in place over
synthesising from scratch - the cookbook scripts are guaranteed to
validate against the current SDK.

Two CLI helpers worth running once per host:

- **`eks-harness video doctor`** probes the local install - ffmpeg presence,
  available encoders (libx264, AMF, NVENC, QSV, VideoToolbox, libsvtav1),
  ONNX execution providers (the WinML MIGraphX EP), and ML backends
  (BeatNet, madmom, librosa, opencv, scipy). Run it before filing an
  issue; most "render failed" reports are a missing dep.
- **`eks-harness video init <template> <dest>`** scaffolds a project from any
  cookbook recipe (`eks-harness video init --list` enumerates them). Faster than
  copy-pasting from the docs and guaranteed to match the current SDK.
