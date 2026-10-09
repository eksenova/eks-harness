# Video recipes index

Use this file when you know *what* you want to build but not which
cookbook script to start from. The Use case column is intent-level
("sync flashes to beats", "add captions to a talking head"); the Recipe
column points at one or more cookbook scripts that demonstrate the
shape; the Key IR types column lists the load-bearing primitives so
you know what to import once you copy the script.

For a complete walkthrough of any single recipe, see the matching
section in `EXAMPLES.md`. For exhaustive field-by-field type docs see
`API_REFERENCE.md`.

## Music-driven edits

| Use case | Recipe file | Key IR types |
|---|---|---|
| Flash on every kick over a montage | `eks_harness/video/cookbook/beat_flash_montage.py` | `BeatTracker`, `BeatRef`, `Flash`, `BeatPulse`, `ExpDecayEnv` |
| Sync line overlays to downbeats | `eks_harness/video/cookbook/lyric_video.py` | `BeatTracker`, `BeatRef(stream="downbeat", every=N)`, `TextOverlay` |
| Speed-ramp a music drop | `eks_harness/video/cookbook/speed_ramp_drop.py` | `SpeedRamp`, `Keyframe`, `EaseOutCubic`, `EaseInOutCubic`, `BeatTracker` |
| Beat-pulsed colour distortion | `eks_harness/video/cookbook/podcast_clip.py` | `RGBSplit`, `BeatPulse`, `BeatTracker` |
| Organic per-frame jitter without keyframes | `eks_harness/video/cookbook/lyric_video.py` | `Animated[float]`, `Noise` |

## Talking head and dialogue

| Use case | Recipe file | Key IR types |
|---|---|---|
| Burned-in TikTok-style captions | `eks_harness/video/cookbook/talking_head_meme.py` | `STTMarkers`, `Captions(style="tiktok")`, `WordRef` |
| Subtitle-style captions for a tutorial | `eks_harness/video/cookbook/captioned_tutorial.py` | `STTMarkers`, `Captions(style="subtitle")`, `PunchIn` |
| Kinetic / podcast-style captions | `eks_harness/video/cookbook/podcast_clip.py` | `STTMarkers`, `Captions(style="kinetic")`, `GeneratedCard` |
| Broadcast-style name strap (chyron) | `eks_harness/video/cookbook/reaction_overlay.py` | `LowerThird` |
| B-roll cuts on sentence boundaries | `eks_harness/video/cookbook/storytelling_bg_music.py` | `STTMarkers`, `MarkerRef(name="...", index=N)` |
| Flash the emphasis word | `eks_harness/video/cookbook/talking_head_meme.py` | `STTMarkers`, `WordRef(text=...)`, `Flash` |

For any of the caption recipes, the `STTMarkers` backend defaults to
`auto` (picks `winml-whisper` first on Windows). To force GPU-accelerated
transcription set `STTMarkers(..., backend="winml-whisper")` and install
`pip install eks-harness[captions-onnx]` plus `eks-harness[winml]`. Word
timings from `winml-whisper` are approximated; for tight per-word
alignment keep `faster-whisper` / `whisperx`. See `TROUBLESHOOTING.md`
("Captions are slow / I want GPU STT").

## Visual storytelling

| Use case | Recipe file | Key IR types |
|---|---|---|
| Ken Burns slideshow over music | `eks_harness/video/cookbook/slideshow_kenburns.py` | `ImageFile`, `Zoom`, `Pan`, `Crossfade`, `Keyframe`, `EaseInOutCubic` |
| Product showcase with LUT + crossfades | `eks_harness/video/cookbook/product_showcase.py` | `ImageFile`, `Zoom`, `ColorGrade`, `Mirror`, `Crossfade` |
| Before / after split-screen comparison | `eks_harness/video/cookbook/before_after_split.py` | `Crop`, `TextOverlay`, `Wipe`, multi-track `z` |
| Reaction-style picture-in-picture | `eks_harness/video/cookbook/reaction_overlay.py` | `Watermark`, `LowerThird`, `Zoom`, `Pan`, multi-track `z` |
| Multicam switching at scene boundaries | `eks_harness/video/cookbook/multicam_switching.py` | `SceneMarkers`, `EveryNth`, `MarkerRef`, multi-track `z` |
| Landscape → vertical reframe | `eks_harness/video/cookbook/auto_reframe_vertical.py` | `AutoReframe`, `RenderSettings.preset` |
| Persistent brand watermark | `eks_harness/video/cookbook/reaction_overlay.py` | `Watermark` |

## Audio mastering

| Use case | Recipe file | Key IR types |
|---|---|---|
| Voiceover ducks background music | `eks_harness/video/cookbook/storytelling_bg_music.py` | `AudioTrack`, `SidechainConfig` |
| Per-segment duck under VO | (compose) | `AudioSegment.ducking` (`Ducking`) |
| Loudness-normalise for delivery | (compose) | `AudioTrack.loudness` (`LoudnessNormalize`) |
| Fade an audio segment in / out | (compose) | `AudioSegment.fade` (`AudioFade`) |
| Parametric EQ on a track | (compose) | `AudioTrack.parametric_eq` (`EQEffect`, `EQBand`) |
| Reverb on dialogue or a sting | (compose) | `AudioSegment.reverb` (`Reverb`) |
| Vocals-only soundtrack via Demucs | (compose) | `SeparatedStem`, `AudioFile`, `AudioSegment.media` |
| Strip mic noise from voiceover | (compose) | `AudioSegment.denoise` (`DeepFilterDenoise`) |

### Vocals-only soundtrack via `SeparatedStem`

Pull just the vocal stem out of a mixed song and use it as a hook
under a montage. The renderer caches all four stems on disk after the
first separation pass, so adding `stems=["drums"]` to a second
segment is cheap.

```python
from eks_harness.video import AudioSegment, AudioFile, AudioTrack, Seconds
from eks_harness.video.ir.media import SeparatedStem

vocals = AudioSegment(
    id="hook",
    start=Seconds(t=0.0),
    media=SeparatedStem(
        source=AudioFile(path="assets/song.mp3"),
        stems=["vocals"],
        model="htdemucs",
    ),
    out=Seconds(t=12.0),
)

music_track = AudioTrack(name="music", segments=[vocals])
```

Combine with `AudioSegment.denoise=DeepFilterDenoise()` on a noisy
voiceover track for a two-step clean-up: stem-isolate the desired
content, then DeepFilterNet 3 the residue.

## Compositing and matting

| Use case | Recipe file | Key IR types |
|---|---|---|
| Remove background from a talking head (video) | (compose) | `MatAnyoneRemove`, multi-track `z` |
| Remove background from a still / generated card | (compose) | `BiRefNetRemove` |
| Track and matte an arbitrary object | (compose) | `SAM3Track(output="matte")` |
| Highlight an object without cutting it out | (compose) | `SAM3Track(output="highlight")` |
| Kinetic-typography caption synced to beat | (compose) | `HTMLOverlay`, `MarkerAnimation`, `BeatTracker` |

### Kinetic-typography caption on beat via `HTMLOverlay` + `MarkerAnimation`

The HTML template carries the layout and a `@keyframes punch-in` rule
the renderer cannot see at IR time. `MarkerAnimation` binds that
animation to the kick stream - at compile time the renderer emits one
CSS rule per kick that fires the animation on the `.lyric` element.
The template is pure HTML/CSS; **no JavaScript**.

```python
from eks_harness.video import BeatTracker, Project, Segment, Track, Seconds
from eks_harness.video.ir.media import HTMLOverlay, MarkerAnimation

overlay = HTMLOverlay(
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
)

overlay_seg = Segment(
    id="caption",
    start=Seconds(t=0.0),
    media=overlay,
    out=Seconds(t=16.0),
)

project = Project(
    fps=30,
    resolution=(1080, 1920),
    duration=16.0,
    tracks=[
        Track(name="bg", z=0, segments=[...]),
        Track(name="captions", z=10, segments=[overlay_seg]),
    ],
    markers=[BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"])],
)
```

## Long-form and delivery

| Use case | Recipe file | Key IR types |
|---|---|---|
| YouTube chapters with sidecar export | `eks_harness/video/cookbook/chapter_markers_youtube.py` | `SceneMarkers`, `MarkerRef(name=, index=)`, `TextOverlay` |
| Skip a sponsor segment | `eks_harness/video/cookbook/sponsor_skipper.py` | `SilenceMarkers`, `Segment.in_` / `out`, `Cut` |
| AV1 software delivery | (compose) | `RenderSettings(encoder="libsvtav1", preset=10)` |

## Quick edits and utilities

| Use case | Recipe file | Key IR types |
|---|---|---|
| Voiceover-driven 30-second promo | `eks_harness/video/cookbook/podcast_clip.py` | `GeneratedCard`, `AudioSegment.in_`, `Captions` |
| Talking-head meme | `eks_harness/video/cookbook/talking_head_meme.py` | `STTMarkers`, `Captions`, `Flash` |
| Hold a still with subtle motion | `eks_harness/video/cookbook/slideshow_kenburns.py` | `ImageFile`, `Zoom`, `Pan` |
| Diagonal wipe between two clips | `eks_harness/video/cookbook/before_after_split.py` | `Wipe(direction=...)` |

## Transitions

Set `Segment.transition_in` / `transition_out` (or `Track.transitions`).
A timed transition overlaps the neighbouring segment by `duration`
seconds - budget for it when placing `start` times. `Cut` (default) adds
zero frames.

| Use case | Recipe file | Key IR types |
|---|---|---|
| Smooth cut between two clips | (compose) | `Crossfade`, `DipToBlack`, `DipToWhite` |
| Directional wipe / slide / push | `eks_harness/video/cookbook/before_after_split.py` | `Wipe`, `LumaWipe`, `Slide`, `Push` |
| Shape / pattern reveal | (compose) | `Iris`, `Radial`, `Pixelize`, `Dissolve`, `BlurThrough`, `FadeGrays`, `ZoomTransition` |
| Music-video glitch / whip between cuts | (compose) | `GlitchTransition`, `RGBShiftWipe`, `WhipPan`, `LightLeak`, `FilmBurn` |
| Flash / glitch the cut on every beat | (compose) | `BeatFlash`, `BeatGlitch`, `BeatTracker` |
