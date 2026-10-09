# Video engine examples

Twenty-one worked examples that exercise the full IR. Examples 1–15
have a runnable companion under `eks_harness/video/cookbook/`; examples 16–21 cover the
newer IR (`HTMLOverlay`, `SeparatedStem`, `MatAnyoneRemove`, the expanded
transition set, the stylised glitch effects and the audio-effect chain)
and are documented inline here until cookbook scripts land. This file
annotates the *why* behind each one - copy a cookbook script when you
want a known-good starting point. See `RECIPES.md` for a use-case →
recipe index when you know what you want to build but not which
script to start from.

## 1. Beat-synced flash montage

**Goal** - Stitch four clips back-to-back over a music bed; flash the
frame on every kick.

**Source** - [`eks_harness/video/cookbook/beat_flash_montage.py`](eks_harness/video/cookbook/beat_flash_montage.py)

**What is happening** - A `BeatTracker` declares the `kick` stream from
the music track. A separate compositing track at `z=10` carries one
long `Flash` effect whose `at` is a `BeatRef` over the project window
and whose envelope is a `BeatPulse(ExpDecayEnv(tau=0.08))`. The
`intensity_ramp` is a two-keyframe linear interpolation from 0.2 to
0.9, so the flashes get visibly stronger across the 16-second window.

When `eks-harness[ml]` is installed, `BeatTracker` picks up BeatNet (CRNN
+ DBN) automatically and produces tightly-locked beat/downbeat
streams; kicks/snares still come from onset detection + a
spectral-centroid split, so all four streams stay populated. Pin a
specific extractor with `backend="beatnet" | "madmom" | "librosa"` if
the project must produce byte-identical renders across hosts.

**Features exercised** - `BeatTracker`, `BeatRef`, `Flash`, `BeatPulse`,
`ExpDecayEnv`, `Animated[float]` keyframes with `EaseInOut`, multi-track
compositing.

## 2. Talking-head meme with TikTok captions

**Goal** - Burn captions into a vertical talking-head clip; flash on the
emphasis word.

**Source** - [`eks_harness/video/cookbook/talking_head_meme.py`](eks_harness/video/cookbook/talking_head_meme.py)

**What is happening** - A single `STTMarkers` source extracts words from
the segment. `Captions(style="tiktok")` runs on the frame pipeline with
a 4-px black stroke for legibility on noisy backgrounds. A `Flash`
anchored to `WordRef(text="absolutely", source="speech")` fires once.

**Features exercised** - `STTMarkers`, `WordRef`, `Captions` (tiktok
style with stroke), `Flash` with a `WordRef` trigger.

## 3. Auto-reframe landscape → vertical

**Goal** - Convert a 1920×1080 interview to a 1080×1920 vertical edit
that keeps the speaker on-screen.

**Source** - [`eks_harness/video/cookbook/auto_reframe_vertical.py`](eks_harness/video/cookbook/auto_reframe_vertical.py)

**What is happening** - The project resolution is `(1080, 1920)` even
though the source is landscape. `AutoReframe(target_aspect=(9, 16),
tracker="face")` produces a per-frame crop window that follows faces;
`smoothing=0.9` keeps the camera move feeling editorial rather than
jittery.

**Features exercised** - `AutoReframe`, `RenderSettings.preset`
(`tiktok-1080-h264`).

## 4. Speed ramp on the music drop

**Goal** - Play at 1.0× until the drop, slam to 0.3× for the impact
moment, then return to 1.0×.

**Source** - [`eks_harness/video/cookbook/speed_ramp_drop.py`](eks_harness/video/cookbook/speed_ramp_drop.py)

**What is happening** - A `SpeedRamp` with five keyframes shapes the
factor curve. `EaseOutCubic` on the deceleration and `EaseInOutCubic`
through the slow-mo plateau give the ramp a deliberate, cinematic
feel. The `BeatTracker` is declared so future iterations can swap the
hard-coded ramp boundaries for `BeatRef`s.

**Features exercised** - `SpeedRamp`, multi-keyframe `Animated[float]`,
`EaseOutCubic` / `EaseInOutCubic`, `BeatTracker` (declarative).

## 5. Captioned screen-recording tutorial

**Goal** - Add subtitle-style captions and a subtle 1.15× punch-in to a
landscape screencast.

**Source** - [`eks_harness/video/cookbook/captioned_tutorial.py`](eks_harness/video/cookbook/captioned_tutorial.py)

**What is happening** - `Captions(style="subtitle")` produces a calm,
multi-word card that is readable for a tutorial audience. `PunchIn`
with `track_speaker=False` applies a constant zoom (no AI tracking, no
ML dependency) - useful when the screen content does not have a face
to follow.

**Features exercised** - `STTMarkers`, `Captions(style="subtitle")` with
stroke, `PunchIn` in non-tracking mode.

## 6. Audio-first podcast clip

**Goal** - Promote a 30-second podcast excerpt with a generated cover
card, beat-pulsing chromatic aberration, and kinetic captions.

**Source** - [`eks_harness/video/cookbook/podcast_clip.py`](eks_harness/video/cookbook/podcast_clip.py)

**What is happening** - There is no source video - `GeneratedCard` paints
the background. `RGBSplit` uses a `BeatPulse` curve so the colour
fringes pulse on every detected kick. Captions run in `kinetic` mode
for visual interest. The audio segment uses `in_=Seconds(t=120.0)` so
the clip starts at the 2-minute mark of the source `.wav`.

**Features exercised** - `GeneratedCard`, `RGBSplit` driven by `BeatPulse`,
`Captions(style="kinetic")`, in-point trimming on `AudioSegment`.

## 7. Voiceover storytelling with sidechain ducking

**Goal** - Voiceover narration over background music that ducks
automatically; b-roll cuts on sentence boundaries.

**Source** - [`eks_harness/video/cookbook/storytelling_bg_music.py`](eks_harness/video/cookbook/storytelling_bg_music.py)

**What is happening** - Two `AudioTrack`s. The second declares
`SidechainConfig(source="vo")` so its level drops whenever the
voiceover is loud. The video track has three b-roll segments, each
anchored with `MarkerRef(name="vo_sentences", index=N)` so they cut to
the next clip when the narrator finishes a sentence - no manual
timestamps required.

**Features exercised** - Multi-track audio, `SidechainConfig`,
`STTMarkers` producing `sentence` indices, `MarkerRef` with `index`.

## 8. Product showcase with crossfades and LUT

**Goal** - Three product photos, each held for 4 seconds, with a slow
zoom, a colour grade, alternating mirror, and crossfade transitions.

**Source** - [`eks_harness/video/cookbook/product_showcase.py`](eks_harness/video/cookbook/product_showcase.py)

**What is happening** - `Zoom` keyframed from 1.0 → 1.15 with
`EaseInOutCubic` for a smooth Ken-Burns feel. `ColorGrade` applies a
warm filmic LUT plus a small exposure / temperature shift. The middle
segment is mirrored for visual variety. `Crossfade(duration=0.4)` on
both `transition_in` and `transition_out` for the inner segments
produces a half-second cross-dissolve between adjacent clips.

**Features exercised** - `ImageFile`, `Zoom`, `ColorGrade(lut_path=...)`,
`Mirror`, `Crossfade`, helper-function pattern for repeated segment
construction.

## 9. Before/after split-screen comparison

**Goal** - Open on a full-frame "before", wipe into a side-by-side
comparison with both halves labelled, then wipe out to a full-frame
"after".

**Source** - [`eks_harness/video/cookbook/before_after_split.py`](eks_harness/video/cookbook/before_after_split.py)

**What is happening** - Three stacked tracks. The base track holds the
two full-frame intro/outro clips with `Wipe(duration=0.4, direction=...)`
transitions on either side of the comparison window. Two compositing
tracks at `z=1` each carry one half of the split: a `Crop(x=0, w=540, ...)`
clips the left source to the left half of the 1080×1920 frame; a
matching crop with `x=540` clips the right source to the right half.
A `TextOverlay` ("BEFORE" / "AFTER") sits in the upper-left of each
half so the viewer never loses orientation.

**Features exercised** - `Wipe` (directional), `Crop` for split-screen
layout, `TextOverlay` for static labels, multi-track compositing with
explicit `z` ordering.

## 10. Ken Burns slideshow

**Goal** - Six stills, each held for five seconds with a slow zoom and
pan, crossfaded into a continuous 30-second montage over a music bed.

**Source** - [`eks_harness/video/cookbook/slideshow_kenburns.py`](eks_harness/video/cookbook/slideshow_kenburns.py)

**What is happening** - A helper builds one `Segment` per still:
`ImageFile` as media, `Zoom` keyframed from 1.0 → 1.15 with
`EaseInOutCubic` for the Ken-Burns push, `Pan` keyframed with an
alternating horizontal direction so adjacent slides drift opposite
ways. Every interior segment carries `Crossfade(duration=0.6)` on
both `transition_in` and `transition_out`; the first and last slides
use `Cut()` on their outer edges. A single `AudioSegment` on the music
track spans the full duration.

**Features exercised** - `ImageFile`, keyframed `Zoom` / `Pan` with
`EaseInOutCubic`, `Crossfade` chained across a long sequence,
helper-function pattern for repeated segment construction.

## 11. Lyric video with beat-locked flashes

**Goal** - One looping background clip; four lyric lines appear on
successive downbeats; kick-driven flashes give every beat a visual
punch; subtle background flicker via seeded noise.

**Source** - [`eks_harness/video/cookbook/lyric_video.py`](eks_harness/video/cookbook/lyric_video.py)

**What is happening** - `BeatTracker` declares `kick` and `downbeat`
streams. The background segment's effect chain layers, in order:
`Brightness` driven by `Animated[float](root=Noise(seed=42,
min_value=-0.08, max_value=0.08, hold=0.25))` for organic flicker;
`Flash` with `BeatPulse(ExpDecayEnv(tau=0.09))` on every kick; four
`TextOverlay` lines whose `start` is `BeatRef(stream="downbeat",
every=idx+1)` so each line lands on its own downbeat and holds for
two seconds.

**Features exercised** - `Noise` curve in an `Animated[float]` slot,
`BeatTracker` with multiple streams, `Flash` driven by `BeatPulse`,
`TextOverlay` anchored to a `BeatRef`.

## 12. Reaction-style picture-in-picture

**Goal** - Show the main clip full-frame and a reactor's video in the
upper-right corner. Add a lower-third name strap and a small static
brand thumbnail.

**Source** - [`eks_harness/video/cookbook/reaction_overlay.py`](eks_harness/video/cookbook/reaction_overlay.py)

**What is happening** - Two tracks. The main track at `z=0` carries
the action, plus a `Watermark` (static image PIP) and a `LowerThird`
chyron with `@reactor_handle / watching live`. A second track at
`z=10` holds the live reaction clip; `Zoom(scale=0.28)` shrinks it and
`Pan(dx=640, dy=-700)` places it in the upper-right corner. The
recipe deliberately demonstrates both PIP strategies so the user can
pick: the cheap watermark when a thumbnail suffices, the track-based
overlay when frame-to-frame expression matters.

**Features exercised** - `Watermark` (image overlay via ffmpeg graph),
`LowerThird` chyron, track-based PIP via `Zoom` + `Pan`, multi-track
compositing.

## 13. Multicam switching with scene markers

**Goal** - Three camera angles on stacked tracks; switch between them
at scene boundaries detected from the shared floor-mic audio.

**Source** - [`eks_harness/video/cookbook/multicam_switching.py`](eks_harness/video/cookbook/multicam_switching.py)

**What is happening** - `SceneMarkers(name="scenes", source="audio_tracks[0]",
threshold=27.0)` produces a stream of scene-boundary times. An
`EveryNth(source=MarkerRef(name="scenes"), n=2, seed=11)` expresses
"switch on every second scene boundary". The trigger is forward-
looking - no effect field currently accepts a `TriggerSource`, so the
recipe parks it under `Project.metadata` so it round-trips through
JSON. When orchestrator support lands, projects authored today
upgrade in place without re-authoring.

**Features exercised** - `SceneMarkers`, `EveryNth` (`TriggerSource`
primitive), forward-looking IR usage via `Project.metadata`, multi-
track compositing with explicit `z` ordering.

## 14. Long-form chapters with YouTube export

**Goal** - A five-minute horizontal talking head with broadcast-style
chapter cards, plus a sidecar `youtube_chapters.txt` ready to paste
into the YouTube description.

**Source** - [`eks_harness/video/cookbook/chapter_markers_youtube.py`](eks_harness/video/cookbook/chapter_markers_youtube.py)

**What is happening** - Six chapters are modelled as a frozen dataclass
tuple. `SceneMarkers(name="chapters", source="tracks[0].segments[0]")`
declares the marker stream so each `TextOverlay` can anchor its
`start` to `MarkerRef(name="chapters", index=N)` instead of a literal
time. The `__main__` block writes both `project.json` and a
`youtube_chapters.txt` formatted as `MM:SS Title` (or `H:MM:SS Title`
when hours > 0), which YouTube parses into clickable description
chapters.

**Features exercised** - `SceneMarkers` with `tracks[N].segments[M]`
source, `MarkerRef(name=, index=)` for chapter anchoring, `TextOverlay`
as a chyron card, sidecar export pattern.

## 15. Sponsor-segment skipper

**Goal** - Reassemble a long-form source into two adjacent segments
that omit the sponsor break, using silence boundaries as the inaudible
join points.

**Source** - [`eks_harness/video/cookbook/sponsor_skipper.py`](eks_harness/video/cookbook/sponsor_skipper.py)

**What is happening** - `SilenceMarkers(threshold_db=-40.0,
min_duration=0.4)` declares the silence stream; the cut points
(62.0 s and 105.0 s in the recipe) were picked by a human listening
pass and happen to land on silent regions. Two `Segment`s on the video
track use `in_` / `out` to splice around the sponsor read; matching
`AudioSegment`s keep audio in lockstep. Both joins use the default
`Cut()` transitions - when picked on silent boundaries, the join is
imperceptible. Automatic detection (matching silence patterns against
a SponsorBlock-style list) is forward-looking; this recipe freezes the
manual cut shape that automation would emit.

**Features exercised** - `SilenceMarkers` declaration, `Segment.in_` /
`out` for source-time splicing, matched video + audio segment splits,
`Cut()` transition for inaudible joins.

## 16. Kinetic-typography caption via `HTMLOverlay`

**Goal** - Burn a lyric line over a montage, punching the text on
every kick. The visual treatment lives in CSS where it belongs;
sync is declarative.

**What is happening** - One track at `z=10` carries a single
`Segment` whose `media` is an `HTMLOverlay(template="overlays/lyric.html")`.
The template declares the layout and a `@keyframes punch-in` rule
(scale 1.0 → 1.18 → 1.0 with a tiny x-shake). A
`MarkerAnimation(stream="kick", selector=".lyric",
animation_name="punch-in", duration=0.22)` tells the renderer to bake
one CSS rule per kick at compile time, binding the keyframes to the
`.lyric` element at every trigger. The user's template is pure HTML
and CSS - **no JavaScript** is permitted, because the virtual-clock
frame capture cannot observe runtime DOM mutations reliably.

```python
markers=[BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"])],
tracks=[
    Track(name="captions", z=10, segments=[
        Segment(
            id="lyric",
            start=Seconds(t=0.0),
            media=HTMLOverlay(
                template="overlays/lyric.html",
                style_vars={"--accent": "#ff3"},
                transparent=True,
                marker_animations=[
                    MarkerAnimation(
                        stream="kick",
                        selector=".lyric",
                        animation_name="punch-in",
                        duration=0.22,
                    ),
                ],
            ),
            out=Seconds(t=16.0),
        ),
    ]),
],
```

**Features exercised** - `HTMLOverlay` as a `MediaSource`,
`MarkerAnimation` baking `@keyframes` against a `BeatTracker` stream,
multi-track compositing with `z`. Requires `pip install
eks-harness[html] && playwright install chromium`.

## 17. Vocals-only hook via `SeparatedStem`

**Goal** - Lift the vocal stem out of a full mix and use it as the
hook under a montage; the original instrumental does not play.

**What is happening** - A single `AudioSegment` on the music
`AudioTrack` carries a `SeparatedStem(source=AudioFile(path="song.mp3"),
stems=["vocals"], model="htdemucs")` as its `media`. The renderer
runs Demucs HTDemucs once per `(source, model)` pair on first render
and caches all four stems on disk; later edits that add a second
segment requesting `stems=["drums"]` from the same source are
near-instant.

```python
audio_tracks=[
    AudioTrack(name="music", segments=[
        AudioSegment(
            id="hook",
            start=Seconds(t=0.0),
            media=SeparatedStem(
                source=AudioFile(path="assets/song.mp3"),
                stems=["vocals"],
                model="htdemucs",
            ),
            out=Seconds(t=16.0),
        ),
    ]),
],
```

**Features exercised** - `SeparatedStem` on `AudioSegment.media`,
Demucs caching behaviour. Requires `pip install eks-harness[stems]`.

## 18. Matted talking head via `MatAnyoneRemove`

**Goal** - Drop the studio background from a vertical talking-head
clip and composite it over a static brand frame.

**What is happening** - Two tracks. The base track at `z=0` carries
the brand background (an `ImageFile` or `GeneratedCard`). The
talking-head track at `z=10` carries a single `Segment` whose effect
chain ends in `MatAnyoneRemove(background=(0, 0, 0, 0))` - the alpha
channel is dropped where the matte says "background", so the brand
shows through. Use `MatAnyoneRemove` (CVPR 2026, temporally
consistent, SOTA on hair) for video; use `BiRefNetRemove` for stills
or when the source is a `GeneratedCard`.

```python
effects=[
    AutoReframe(target_aspect=(9, 16), tracker="face"),
    MatAnyoneRemove(background=(0, 0, 0, 0)),
    LowerThird(title="@host", subtitle="Live"),
],
```

**Features exercised** - `MatAnyoneRemove` as the alpha source for
multi-track compositing, `AutoReframe` before the matte so the matter
runs on a tighter buffer.

`pip install eks-harness[matting]` installs the `rembg` + `thinplate`
runtime, but `MatAnyoneRemove` also needs the MatAnyone package itself,
which has no PyPI release:

```bash
pip install eks-harness[matting]
pip install --no-deps git+https://github.com/pq-yang/MatAnyone
# ensure torchvision matches your torch build; on Windows+AMD ROCm:
#   pip install https://repo.radeon.com/rocm/windows/rocm-rel-<ver>/torchvision-<ver>+rocm<ver>-cp312-cp312-win_amd64.whl
```

Weights (~135 MB, ungated) auto-download from HF `PeiqingYang/MatAnyone`
on first inference. `BiRefNetRemove` (stills) needs only the base
`[matting]` extra - it uses rembg's bundled BiRefNet-portrait ONNX.

## 19. Beat-reactive transitions between cuts

**Goal** - Cut between four montage clips, but make the joins react to
the music: a white flash on the beat between the first two, a chromatic
glitch on the next, and a motion-blurred whip-pan into the last.

**What is happening** - `transition_in` / `transition_out` carry the
boundary effects; each timed transition overlaps its neighbour by
`duration` seconds, so the segment `start` times account for the
overlap. `BeatFlash` and `BeatGlitch` read window-local beat times from
the resolved `kick` stream - the renderer threads the beats that fall
inside each transition window and falls back to a single mid-window pulse
if none land, so the join is never a silent no-op. `WhipPan` needs no
markers; it is a directional motion-blur slide.

```python
markers=[BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"])],
tracks=[Track(name="main", segments=[
    Segment(id="a", start=Seconds(t=0.0), media=VideoFile(path="a.mp4"),
            out=Seconds(t=4.0),
            transition_out=BeatFlash(duration=0.5, stream="kick", intensity=0.7)),
    Segment(id="b", start=Seconds(t=3.5), media=VideoFile(path="b.mp4"),
            out=Seconds(t=4.0),
            transition_out=BeatGlitch(duration=0.4, stream="kick", intensity=0.7)),
    Segment(id="c", start=Seconds(t=7.0), media=VideoFile(path="c.mp4"),
            out=Seconds(t=4.0),
            transition_out=WhipPan(duration=0.25, direction="left", intensity=0.7)),
    Segment(id="d", start=Seconds(t=10.75), media=VideoFile(path="d.mp4"),
            out=Seconds(t=4.0)),
])],
```

**Features exercised** - `BeatFlash` / `BeatGlitch` driven by a
`BeatTracker` stream, `WhipPan`, `transition_out` overlap budgeting. Swap
in `Iris`, `LumaWipe`, `LightLeak` or `FilmBurn` the same way - see the
Transitions section of `API_REFERENCE.md` for the full set.

## 20. Music-video grime stack

**Goal** - Push a clip into "corrupted music-video" territory: chromatic
RGB fringing and a digital glitch that both pulse on the kick, over a
constant VHS texture with a low-frequency camera shake.

**What is happening** - A single effect chain stacks four stylised
(frame-pipeline) effects. `RGBSplit.offset_x` and `Glitch.intensity` are
`Animated` from a `BeatPulse`, so the corruption spikes on every kick and
decays between hits; `VHS` lays down a constant scanline + chroma-bleed
texture; `Shake` adds a steady handheld wobble. Order matters - the VHS
texture sits under the glitch so the scanlines themselves get torn.

```python
effects=[
    RGBSplit(offset_x=Animated[int](root=BeatPulse(
        trigger=BeatRef(stream="kick"), envelope=ExpDecayEnv(tau=0.08),
        intensity_ramp=12.0))),
    Glitch(intensity=Animated[float](root=BeatPulse(
        trigger=BeatRef(stream="kick"), envelope=ExpDecayEnv(tau=0.06),
        intensity_ramp=0.7)), block_size=24),
    VHS(scanlines=True, chroma_blur=1.5, noise=0.05),
    Shake(intensity=Animated[float](root=0.4), freq_hz=5.0, decay=0.0),
],
```

**Features exercised** - `RGBSplit` / `Glitch` driven by `BeatPulse`,
`VHS`, `Shake`, frame-pipeline effect ordering. `Datamosh(intensity=...)`
slots into the same chain for a heavier compression-smear look.

## 21. Audio mastering chain

**Goal** - Clean a noisy field-recorded voiceover, give it a touch of
room, EQ the music bed, and hit a delivery loudness target - using the
typed audio-effect fields rather than hand-keyframed gain.

**What is happening** - Audio effects are typed fields on the carrier
model, not a discriminated union. Segment-scoped effects live on the
`AudioSegment` (`denoise`, `reverb`, `fade`); track-scoped effects live
on the `AudioTrack` (`parametric_eq`, `loudness`) and run on the summed
bus after `amix`. `DeepFilterDenoise` runs first as a pre-pass on the VO
input and is cached on disk; `LoudnessNormalize` is the final delivery
pass on the master.

```python
audio_tracks=[
    AudioTrack(name="vo", segments=[
        AudioSegment(
            id="narration", start=Seconds(t=0.0), out=Seconds(t=30.0),
            media=AudioFile(path="assets/vo.wav"),
            denoise=DeepFilterDenoise(attenuation_limit_db=40.0),
            reverb=Reverb(wet=Animated[float](root=0.15), room_size=0.4),
            fade=AudioFade(fade_in=0.3, fade_out=1.0),
        ),
    ]),
    AudioTrack(
        name="music",
        segments=[AudioSegment(id="bed", start=Seconds(t=0.0),
                               out=Seconds(t=30.0),
                               media=AudioFile(path="assets/bed.mp3"))],
        sidechain=SidechainConfig(source="vo"),
        parametric_eq=EQEffect(bands=[
            EQBand(frequency_hz=120.0, gain_db=Animated[float](root=-3.0),
                   type="high_shelf"),
            EQBand(frequency_hz=3000.0, gain_db=Animated[float](root=2.0), q=1.2),
        ]),
        loudness=LoudnessNormalize(target_lufs=-14.0, true_peak_db=-1.5),
    ),
]
```

**Features exercised** - `DeepFilterDenoise` (`eks-harness[denoise]`),
`Reverb`, `AudioFade`, `EQEffect` / `EQBand`, `LoudnessNormalize`,
`SidechainConfig` for VO-ducked music. `Ducking` on an individual
`AudioSegment` is the finer-grained alternative to track-wide
`SidechainConfig`.
