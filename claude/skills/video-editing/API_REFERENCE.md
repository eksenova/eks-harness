# Video engine API reference

This file is generated from the live SDK by `tooling/gen_api_reference.py`.
Do not hand-edit. Regenerate after any IR change.

Every type listed here is a `pydantic.BaseModel` (or `RootModel`) that
round-trips through JSON. Constructor arguments are the field list shown
under each entry; defaults are explicit; `Animated[T]` accepts a constant,
a list of `Keyframe[T]`, or a `Curve` (see [Curves](#curves)).

Imports follow this convention:

```python
from eks_harness.video import *                     # IR types re-exported here
from eks_harness.video.ir.effects import (           # effect IR types not in the
    Captions, AutoReframe, ChromaKey,       # top-level star-import yet
    ColorGrade, PunchIn, RGBSplit, SAMTrack,
    Saturation, Contrast, Mirror, Pan,
    Zoom, SpeedRamp, Freeze, Glitch,
    Shake, VHS, Datamosh, BackgroundRemove,
)
```

## Effects

Every effect declares `compile_targets` - the orchestrator uses this to
decide whether to fold the effect into ffmpeg's filter graph or run it
on the per-frame Python pipeline (or split between the two).

### `Brightness`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['brightness']` = 'brightness'
- `amount: Animated[float]`

```python
Brightness(amount=0.4)
```

### `Contrast`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['contrast']` = 'contrast'
- `amount: Animated[float]`

```python
Contrast(amount=1.2)
```

### `Saturation`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['saturation']` = 'saturation'
- `amount: Animated[float]`

```python
Saturation(amount=1.5)
```

### `Crop`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['crop']` = 'crop'
- `x: Animated[int]`
- `y: Animated[int]`
- `w: Animated[int]`
- `h: Animated[int]`

```python
Crop(x=0, y=420, w=1080, h=1920)
```

### `Mirror`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['mirror']` = 'mirror'
- `axis: Literal['horizontal', 'vertical', 'both']` = 'horizontal'

```python
Mirror(axis="horizontal")
```

### `Zoom`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['zoom']` = 'zoom'
- `scale: Animated[float]`
- `cx: Animated[float]` = Animated[float](root=0.5)
- `cy: Animated[float]` = Animated[float](root=0.5)

```python
Zoom(scale=1.25, cx=0.5, cy=0.5)
```

### `Pan`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['pan']` = 'pan'
- `dx: Animated[int]`
- `dy: Animated[int]`

```python
Pan(dx=120, dy=0)
```

### `SpeedRamp`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['speed_ramp']` = 'speed_ramp'
- `factor: Animated[float]`

```python
SpeedRamp(factor=Animated[float](root=[
    Keyframe[float](t=Seconds(t=0.0), v=1.0),
    Keyframe[float](t=Seconds(t=2.0), v=0.3),
]))
```

### `Freeze`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['freeze']` = 'freeze'
- `at: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef`
- `hold: float`

```python
Freeze(at=Seconds(t=4.0), hold=1.0)
```

### `ChromaKey`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['chroma_key']` = 'chroma_key'
- `color: tuple[int, int, int]` = (0, 255, 0)
- `similarity: float` = 0.1
- `blend: float` = 0.0

```python
ChromaKey(color=(0, 255, 0), similarity=0.12)
```

### `ColorGrade`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['color_grade']` = 'color_grade'
- `lut_path: Path | None` = None
- `exposure: Animated[float]` = Animated[float](root=0.0)
- `temperature: Animated[float]` = Animated[float](root=0.0)
- `tint: Animated[float]` = Animated[float](root=0.0)

```python
ColorGrade(lut_path="luts/teal_orange.cube", exposure=0.2)
```

### `Fade`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['fade']` = 'fade'
- `direction: Literal['in', 'out']`
- `duration: float`

```python
Fade(direction="in", duration=0.5)
```

### `Blur`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['blur']` = 'blur'
- `radius: Animated[float]`

```python
Blur(radius=Animated[float](root=3.0))
```

### `Sharpen`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['sharpen']` = 'sharpen'
- `amount: Animated[float]`

```python
Sharpen(amount=Animated[float](root=0.6))
```

### `Vignette`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['vignette']` = 'vignette'
- `angle: float` = 0.628
- `x0: float` = 0.5
- `y0: float` = 0.5

```python
Vignette(angle=0.628, x0=0.5, y0=0.5)
```

### `FilmGrain`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['film_grain']` = 'film_grain'
- `intensity: Animated[float]`
- `seed: int | None` = None

```python
FilmGrain(intensity=Animated[float](root=0.25), seed=1234)
```

### `LUT`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['lut']` = 'lut'
- `path: Path`

```python
LUT(path="luts/teal_orange.cube")
```

### `Invert`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['invert']` = 'invert'

```python
Invert()
```

### `Posterize`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['posterize']` = 'posterize'
- `levels: Animated[int]`

```python
Posterize(levels=Animated[int](root=6))
```

### `TextOverlay`

Compile targets: `ffmpeg_graph`.

Fields:

- `kind: Literal['text_overlay']` = 'text_overlay'
- `text: str`
- `font: str` = 'Inter'
- `size: int` = 48
- `color: tuple[int, int, int, int]` = (255, 255, 255, 255)
- `x: Animated[int]` = Animated[int](root=100)
- `y: Animated[int]` = Animated[int](root=100)
- `start: Optional[Annotated[time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef, FieldInfo(annotation=None, required=True, discriminator='kind'), BeforeValidator(func=<function _coerce_time_ref at 0x103e0dbc0>, json_schema_input_type=PydanticUndefined)]]` = None
- `duration: float | None` = None

```python
TextOverlay(
    text="Tap to learn more",
    x=Animated[int](root=80),
    y=Animated[int](root=1700),
    size=56,
    start=Seconds(t=1.0),
    duration=3.0,
)
```

### `LowerThird`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['lower_third']` = 'lower_third'
- `title: str`
- `subtitle: str` = ''
- `font: str` = 'Inter'
- `title_size: int` = 72
- `subtitle_size: int` = 36
- `position: Literal['left', 'center', 'right']` = 'left'
- `margin: int` = 120
- `color: tuple[int, int, int, int]` = (255, 255, 255, 255)
- `bg_color: tuple[int, int, int, int]` = (0, 0, 0, 180)

```python
LowerThird(
    title="Alex Smith",
    subtitle="Founder, Acme",
    position="left",
    margin=120,
)
```

### `Watermark`

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['watermark']` = 'watermark'
- `path: Path`
- `x: Animated[int]` = Animated[int](root=20)
- `y: Animated[int]` = Animated[int](root=20)
- `opacity: Animated[float]` = Animated[float](root=1.0)
- `scale: Animated[float]` = Animated[float](root=1.0)

```python
Watermark(
    path="brand/logo.png",
    x=Animated[int](root=40),
    y=Animated[int](root=40),
    opacity=Animated[float](root=0.85),
)
```

### `StickerOverlay`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['sticker']` = 'sticker'
- `path: Path`
- `x: Animated[int]` = Animated[int](root=0)
- `y: Animated[int]` = Animated[int](root=0)
- `scale: Animated[float]` = Animated[float](root=1.0)
- `rotation: Animated[float]` = Animated[float](root=0.0)

```python
StickerOverlay(
    path="stickers/fire.png",
    x=Animated[int](root=400),
    y=Animated[int](root=600),
    scale=Animated[float](root=1.2),
)
```

### `Flash`

Solid-color flash with envelope.

    ``at`` may be a TimeRef or an Animated[float] of trigger times.
    ``curve`` (when set, typically :class:`BeatPulse`) drives the per-frame
    alpha envelope used by the frame-pipeline composite path.

Compile targets: `ffmpeg_graph, frame_pipeline`.

Fields:

- `kind: Literal['flash']` = 'flash'
- `at: Union[Annotated[time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef, FieldInfo(annotation=None, required=True, discriminator='kind'), BeforeValidator(func=<function _coerce_time_ref at 0x103e0dbc0>, json_schema_input_type=PydanticUndefined)], Animated[float]]`
- `duration: Animated[float]`
- `color: tuple[int, int, int]` = (255, 255, 255)
- `curve: Optional[Annotated[curves.BeatPulse | curves.ExpDecay | curves.Sine | curves.LFO | curves.Lambda | curves.Spring | curves.Noise | curves.RandomChoice | curves.Step | curves.Bezier | curves.Bounce, FieldInfo(annotation=None, required=True, discriminator='kind')]]` = None

```python
Flash(
    at=BeatRef(stream="kick"),
    duration=Animated[float](root=0.12),
    color=(255, 255, 255),
)
```

### `RGBSplit`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['rgb_split']` = 'rgb_split'
- `offset_x: Animated[int]`
- `offset_y: Animated[int]` = Animated[int](root=0)

```python
RGBSplit(offset_x=Animated[int](root=8))
```

### `Glitch`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['glitch']` = 'glitch'
- `intensity: Animated[float]`
- `block_size: int` = 16
- `seed: int | None` = None

```python
Glitch(intensity=0.4, block_size=24)
```

### `Shake`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['shake']` = 'shake'
- `intensity: Animated[float]`
- `freq_hz: float` = 6.0
- `decay: float` = 0.0

```python
Shake(intensity=Animated[float](root=0.6), freq_hz=6.0, decay=0.5)
```

### `VHS`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['vhs']` = 'vhs'
- `scanlines: bool` = True
- `chroma_blur: float` = 1.5
- `noise: float` = 0.05

```python
VHS(scanlines=True, chroma_blur=1.5, noise=0.05)
```

### `Datamosh`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['datamosh']` = 'datamosh'
- `intensity: Animated[float]` = Animated[float](root=0.5)

```python
Datamosh(intensity=Animated[float](root=0.7))
```

### `Captions`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['captions']` = 'captions'
- `source: str`
- `font: str` = 'Inter'
- `size: int` = 64
- `color: tuple[int, int, int, int]` = (255, 255, 255, 255)
- `stroke: tuple[int, int, int, int] | None` = None
- `stroke_width: int` = 0
- `position: Literal['top', 'center', 'bottom']` = 'bottom'
- `margin: int` = 120
- `style: Literal['tiktok', 'subtitle', 'kinetic']` = 'tiktok'
- `word_animation: Literal['pop', 'fade', 'none']` = 'pop'
- `max_words_per_card: int` = 4

```python
Captions(source="speech", style="tiktok", max_words_per_card=4)
```

### `MatAnyoneRemove`

Background matting. Current backend: ``rembg`` + BiRefNet-portrait
    (per-frame, no temporal propagation). The native MatAnyone 2 integration
    is pending - for now this and :class:`BiRefNetRemove` share the rembg
    backend. Requires ``pip install eks-harness[matting]``.

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['matanyone_remove']` = 'matanyone_remove'
- `background: tuple[int, int, int, int]` = (0, 0, 0, 0)

```python
MatAnyoneRemove()
```

### `BiRefNetRemove`

Per-frame BiRefNet matting via ``rembg``. Requires
    ``pip install eks-harness[matting]``.

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['birefnet_remove']` = 'birefnet_remove'
- `background: tuple[int, int, int, int]` = (0, 0, 0, 0)

```python
BiRefNetRemove()
```

### `SAM3Track`

SAM 3 (Meta, 2025) - open-vocabulary segmentation + tracking.

    Lazy-loaded; requires ``pip install eks-harness[sam3]``.

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['sam3_track']` = 'sam3_track'
- `prompt: str`
- `output: Literal['mask', 'matte', 'highlight']` = 'mask'
- `confidence_threshold: float` = 0.5

```python
SAM3Track()
```

### `AutoReframe`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['auto_reframe']` = 'auto_reframe'
- `target_aspect: tuple[int, int]` = (9, 16)
- `tracker: Literal['face', 'salience']` = 'face'
- `smoothing: float` = 0.85

```python
AutoReframe(target_aspect=(9, 16), tracker="face")
```

### `PunchIn`

Compile targets: `frame_pipeline`.

Fields:

- `kind: Literal['punch_in']` = 'punch_in'
- `track_speaker: bool` = True
- `zoom: Animated[float]` = Animated[float](root=1.2)
- `trigger: Optional[Annotated[time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef, FieldInfo(annotation=None, required=True, discriminator='kind'), BeforeValidator(func=<function _coerce_time_ref at 0x103e0dbc0>, json_schema_input_type=PydanticUndefined)]]` = None

```python
PunchIn(track_speaker=True, zoom=Animated[float](root=1.25))
```

## Audio effects

Audio effects are typed optional fields on the carrier model, not
entries in a discriminated union (they attach the same way `eq` and
`sidechain` do). Segment-scoped effects live on `AudioSegment`
(`ducking`, `fade`, `reverb`, `denoise`); track-scoped effects live on
`AudioTrack` (`loudness`, `parametric_eq`) and operate on the summed
bus after `amix`.

### `Ducking`

Segment-level sidechain ducking keyed off another audio source.

    Mirrors :class:`SidechainConfig` (which is track-level) but attaches to
    an individual :class:`AudioSegment`, enabling per-clip ducking decisions
    such as ducking music only under a specific VO segment.

Fields:

- `source: str`
- `threshold_db: float` = -20.0
- `ratio: float` = 4.0
- `attack_ms: float` = 10.0
- `release_ms: float` = 200.0

```python
Ducking(source="vo", threshold_db=-20.0, ratio=4.0)
```

### `AudioFade`

Per-segment fade-in and/or fade-out applied via ffmpeg ``afade``.

Fields:

- `fade_in: float` = 0.0
- `fade_out: float` = 0.0

```python
AudioFade(fade_in=0.5, fade_out=1.0)
```

### `Reverb`

Algorithmic reverb via a tuned ``aecho`` tap chain.

    ``room_size`` and ``damping`` shape the underlying echo taps; ``wet``
    is the post-effect mix amount (0..1, animatable).

Fields:

- `wet: Animated[float]` = Animated[float](root=0.3)
- `room_size: float` = 0.5
- `damping: float` = 0.5

```python
Reverb(wet=Animated[float](root=0.3), room_size=0.5, damping=0.5)
```

### `DeepFilterDenoise`

Per-segment audio noise removal via DeepFilterNet 3.

    Runs as a pre-pass on the segment's input audio (before ``atrim`` /
    ``afade`` / ``adelay``) and the rest of the audio chain operates on the
    denoised WAV. Cached on disk by ``(source path, source mtime, params)``
    so repeated renders reuse the result.

    Lazy-loaded; requires ``pip install eks-harness[denoise]``.

Fields:

- `enabled: bool` = True
- `attenuation_limit_db: float` = 60.0

```python
DeepFilterDenoise(enabled=True, attenuation_limit_db=60.0)
```

### `LoudnessNormalize`

EBU R128 loudness normalization applied to the post-mix track bus.

    Defaults target short-form social platforms (TikTok / Reels / Shorts).

Fields:

- `target_lufs: float` = -16.0
- `true_peak_db: float` = -1.5
- `lra: float` = 11.0

```python
LoudnessNormalize(target_lufs=-16.0, true_peak_db=-1.5, lra=11.0)
```

### `EQEffect`

Multi-band parametric EQ; supersedes the 3-band :class:`EQ` when set.

    Each :class:`EQBand` becomes one ``equalizer`` filter node in the
    rendered track chain.

Fields:

- `bands: list[audio_effects.EQBand]` = []

```python
EQEffect(bands=[
    EQBand(frequency_hz=80.0, gain_db=Animated[float](root=-3.0), type="high_shelf"),
    EQBand(frequency_hz=3000.0, gain_db=Animated[float](root=2.0), q=1.2),
])
```

### `EQBand`

One band of a parametric EQ.

Fields:

- `frequency_hz: float`
- `gain_db: Animated[float]` = Animated[float](root=0.0)
- `q: float` = 1.0
- `type: Literal['peak', 'low_shelf', 'high_shelf']` = 'peak'

```python
EQBand(frequency_hz=120.0, gain_db=Animated[float](root=2.5), q=1.0, type="peak")
```

## Easings

Easings interpolate between two `Keyframe` values. Each variant
exposes `evaluate(t01)` returning the eased value in `[0, 1]`.
`PluginEasing` is a deferred reference resolved through the
`eks_harness.video.easings` entry-point group.

### `LinearEasing`

Fields:

- `kind: Literal['linear']` = 'linear'

```python
LinearEasing()
```

### `EaseIn`

Fields:

- `kind: Literal['ease_in']` = 'ease_in'

```python
EaseIn()
```

### `EaseOut`

Fields:

- `kind: Literal['ease_out']` = 'ease_out'

```python
EaseOut()
```

### `EaseInOut`

Fields:

- `kind: Literal['ease_in_out']` = 'ease_in_out'

```python
EaseInOut()
```

### `EaseInCubic`

Fields:

- `kind: Literal['ease_in_cubic']` = 'ease_in_cubic'

```python
EaseInCubic()
```

### `EaseOutCubic`

Fields:

- `kind: Literal['ease_out_cubic']` = 'ease_out_cubic'

```python
EaseOutCubic()
```

### `EaseInOutCubic`

Fields:

- `kind: Literal['ease_in_out_cubic']` = 'ease_in_out_cubic'

```python
EaseInOutCubic()
```

### `BounceOut`

Fields:

- `kind: Literal['bounce_out']` = 'bounce_out'

```python
BounceOut()
```

### `ElasticOut`

Fields:

- `kind: Literal['elastic_out']` = 'elastic_out'

```python
ElasticOut()
```

### `BackOut`

Fields:

- `kind: Literal['back_out']` = 'back_out'

```python
BackOut()
```

### `CubicBezier`

CSS-style cubic bezier easing with control points in [0, 1].

Fields:

- `kind: Literal['cubic_bezier']` = 'cubic_bezier'
- `p1x: float`
- `p1y: float`
- `p2x: float`
- `p2y: float`

```python
CubicBezier(p1x=0.25, p1y=0.1, p2x=0.25, p2y=1.0)
```

### `PluginEasing`

Deferred reference to an ``EasingPlugin`` looked up by name at render time.

Fields:

- `kind: Literal['plugin']` = 'plugin'
- `name: str`
- `params: dict[str, Any]` = {}

```python
PluginEasing(name="my-easing", params={"strength": 0.7})
```

## Curves

Curves describe a continuous-time function `f(t) -> float` sampled
at every project frame. Use a `Curve` anywhere `Animated[float]`
is accepted (e.g. `Brightness.amount`, `Flash.duration`).

### `BeatPulse`

Pulse curve triggered on every beat in the referenced stream.

    ``intensity_ramp`` is either a constant amplitude or a list of keyframes
    spanning the project; the amplitude at each trigger is sampled from the
    ramp and multiplied into the envelope.

Fields:

- `kind: Literal['beat_pulse']` = 'beat_pulse'
- `trigger: BeatRef`
- `envelope: curves.ExpDecayEnv | curves.LinearEnv | curves.BoxEnv | curves.EasingEnv`
- `intensity_ramp: float | list[Keyframe[float]]`
- `baseline: float` = 0.0

```python
BeatPulse(
    trigger=BeatRef(stream="kick"),
    envelope=ExpDecayEnv(tau=0.08),
    intensity_ramp=0.6,
)
```

### `ExpDecay`

Single-shot exponential decay anchored at t=0.

Fields:

- `kind: Literal['exp_decay']` = 'exp_decay'
- `tau: float`
- `baseline: float` = 0.0

```python
ExpDecay(tau=0.5, baseline=0.0)
```

### `Sine`

Fields:

- `kind: Literal['sine']` = 'sine'
- `freq_hz: float`
- `phase: float` = 0.0
- `amp: float` = 1.0
- `offset: float` = 0.0

```python
Sine(freq_hz=1.5, amp=0.4, offset=0.5)
```

### `LFO`

Low-frequency oscillator with a selectable waveform shape.

Fields:

- `kind: Literal['lfo']` = 'lfo'
- `shape: Literal['tri', 'saw', 'square']`
- `freq_hz: float`
- `amp: float` = 1.0
- `offset: float` = 0.0
- `phase: float` = 0.0

```python
LFO(shape="tri", freq_hz=2.0, amp=0.5)
```

### `Lambda`

Sandboxed expression. Only ``t`` (seconds) is exposed.

Fields:

- `kind: Literal['lambda']` = 'lambda'
- `expr: str`

```python
Lambda(expr="0.5 + 0.5 * sin(t)")
```

### `Spring`

Physics-based damped harmonic oscillator settling toward ``target``.

    Integrated per-frame with ``dt = 1 / fps`` starting from ``initial``
    position with zero velocity. ``stiffness`` controls oscillation
    frequency; ``damping`` controls how quickly oscillations die out.

Fields:

- `kind: Literal['spring']` = 'spring'
- `target: float`
- `stiffness: float` = 100.0
- `damping: float` = 10.0
- `initial: float` = 0.0

```python
Spring(target=1.0, stiffness=120.0, damping=8.0, initial=0.0)
```

### `Noise`

Deterministic uniform random noise in ``[min_value, max_value]``.

    With ``hold == 0`` a fresh sample is drawn every frame. With ``hold > 0``
    each sample is held for ``hold`` seconds before redrawing (sample-and-hold).
    The seed is salted with the project-level ``random_seed`` (if set) so
    several curves in the same project can be jittered together.

Fields:

- `kind: Literal['noise']` = 'noise'
- `seed: int`
- `min_value: float` = 0.0
- `max_value: float` = 1.0
- `hold: float` = 0.0

```python
Noise(seed=1, min_value=0.0, max_value=1.0, hold=0.0)
```

### `RandomChoice`

Discrete random pick from ``values``, holding each pick for ``hold`` seconds.

    Salted with the project-level ``random_seed`` like :class:`Noise`.

Fields:

- `kind: Literal['random_choice']` = 'random_choice'
- `seed: int`
- `values: list[float]`
- `hold: float` = 0.5

```python
RandomChoice(seed=1, values=[0.0, 0.5, 1.0], hold=0.25)
```

### `Step`

Deterministic round-robin through ``values``, advancing every ``hold`` seconds.

Fields:

- `kind: Literal['step']` = 'step'
- `values: list[float]`
- `hold: float`

```python
Step(values=[0.0, 0.5, 1.0], hold=0.5)
```

### `Bezier`

Cubic Bezier curve in project-time.

    Goes from ``p0`` at ``t=0`` to ``p3`` at ``t=duration`` via control points
    ``p1`` and ``p2``. Beyond ``duration`` the value is clamped to ``p3``.

    Distinct from :class:`eks_harness.video.ir.easing.CubicBezier` which is a
    normalized easing (0..1 -> 0..1); this is a value-producing curve in
    seconds.

Fields:

- `kind: Literal['bezier']` = 'bezier'
- `p0: float`
- `p1: float`
- `p2: float`
- `p3: float`
- `duration: float`

```python
Bezier(p0=0.0, p1=0.2, p2=0.8, p3=1.0, duration=2.0)
```

### `Bounce`

Damped absolute-value sine: ``amplitude * exp(-decay*t) * |sin(2*pi*t/period)|``.

    Useful for "settling" animations such as a UI element bouncing into place.

Fields:

- `kind: Literal['bounce']` = 'bounce'
- `amplitude: float`
- `period: float`
- `decay: float` = 1.0

```python
Bounce(amplitude=0.5, period=0.6, decay=1.4)
```

## Envelopes

Used by `BeatPulse.envelope` to shape each beat hit. All envelopes
produce an amplitude in `[0, 1]` over their declared duration.

### `ExpDecayEnv`

Exponential decay envelope: ``exp(-t / tau)``.

Fields:

- `kind: Literal['exp_decay_env']` = 'exp_decay_env'
- `tau: float`

```python
ExpDecayEnv(tau=0.08)
```

### `LinearEnv`

Triangular envelope with explicit rise / fall durations (seconds).

Fields:

- `kind: Literal['linear_env']` = 'linear_env'
- `rise: float`
- `fall: float`

```python
LinearEnv(rise=0.02, fall=0.18)
```

### `BoxEnv`

Rectangular envelope of ``width`` seconds at amplitude 1.0.

Fields:

- `kind: Literal['box_env']` = 'box_env'
- `width: float`

```python
BoxEnv(width=0.1)
```

### `EasingEnv`

Duration-bounded envelope shaped by any :class:`Easing` variant.

    Lets the user spell things like "250ms glitch on every beat with
    easeOutElastic-shaped strength" via::

        BeatPulse(
            trigger=BeatRef(stream="kick", offset_ms=50),
            envelope=EasingEnv(duration=0.25, easing=ElasticOut()),
            intensity_ramp=0.8,
        )

    The envelope is zero outside ``[trigger, trigger + duration]``. Inside
    that window, the easing's ``evaluate(t01)`` is sampled at
    ``t01 = (frame - trigger) / duration_in_frames``. ``direction``
    controls whether the eased curve runs forward (``"in"``: 0→1, like a
    fade-up) or is inverted (``"out"``: 1→0, like a fade-down / one-shot
    decay). Default is ``"out"`` because the most common use is a strong
    initial pop that decays, matching ``ExpDecayEnv`` semantics.

Fields:

- `kind: Literal['easing_env']` = 'easing_env'
- `duration: float`
- `easing: easing.LinearEasing | easing.EaseIn | easing.EaseOut | easing.EaseInOut | easing.EaseInCubic | easing.EaseOutCubic | easing.EaseInOutCubic | easing.BounceOut | easing.ElasticOut | easing.BackOut | easing.CubicBezier | easing.PluginEasing`
- `direction: Literal['in', 'out']` = 'out'

```python
EasingEnv(duration=0.25, easing=ElasticOut(), direction="out")
```

## Time references

Every `start`, `in_`, `out`, keyframe `t` and trigger time accepts
a `TimeRef`. A compact string shorthand is also supported via
`eks_harness.video.parse_time_string`:

```
"0:16.5"      -> Seconds(t=16.5)            # mm:ss.ms
"f:480"       -> Frames(n=480)              # frames at project fps
"b:kick"      -> BeatRef(stream="kick")     # every beat
"b:kick:4"    -> BeatRef(stream="kick", every=4)
"w:hello"     -> WordRef(text="hello")
"w:#3"        -> WordRef(index=3)
"m:intro"     -> MarkerRef(name="intro")
```

### `Seconds`

Absolute time in seconds from project start.

Fields:

- `kind: Literal['seconds']` = 'seconds'
- `t: float`

```python
Seconds(t=4.5)
```

### `Frames`

Absolute time in frames at the project frame rate.

Fields:

- `kind: Literal['frames']` = 'frames'
- `n: int`

```python
Frames(n=120)
```

### `BeatRef`

Reference to a beat marker stream emitted by a ``MarkerSource``.

    ``every=N`` selects every N-th beat in the stream. ``offset_ms`` shifts
    the resolved time. ``range`` optionally bounds resolution to a sub-window
    of the project.

Fields:

- `kind: Literal['beat']` = 'beat'
- `stream: str`
- `every: int` = 1
- `offset_ms: float` = 0.0
- `range: tuple[Annotated[time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef, FieldInfo(annotation=None, required=True, discriminator='kind'), BeforeValidator(func=<function _coerce_time_ref at 0x103e0dbc0>, json_schema_input_type=PydanticUndefined)], Annotated[time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef, FieldInfo(annotation=None, required=True, discriminator='kind'), BeforeValidator(func=<function _coerce_time_ref at 0x103e0dbc0>, json_schema_input_type=PydanticUndefined)]] | None` = None

```python
BeatRef(stream="kick", every=2, offset_ms=-15.0)
```

### `WordRef`

Reference to a word or word-index marker emitted by an STT marker source.

Fields:

- `kind: Literal['word']` = 'word'
- `text: str | None` = None
- `index: int | None` = None
- `source: str | None` = None

```python
WordRef(text="hello")
```

### `MarkerRef`

Reference to a named marker (e.g. scene cut).

Fields:

- `kind: Literal['marker']` = 'marker'
- `name: str`
- `index: int | None` = None

```python
MarkerRef(name="intro")
```

## Media sources

Backs every `Segment.media`. The renderer materialises a media
source through a `MediaProvider` plugin (filesystem, yt-dlp, TTS,
generated card, solid colour).

### `VideoFile`

Fields:

- `kind: Literal['video_file']` = 'video_file'
- `path: Path | str`

```python
VideoFile(path="assets/clip.mp4")
```

### `ImageFile`

Fields:

- `kind: Literal['image_file']` = 'image_file'
- `path: Path | str`

```python
ImageFile(path="assets/poster.png")
```

### `Solid`

Solid colour fill. Color is RGBA in 0-255.

Fields:

- `kind: Literal['solid']` = 'solid'
- `color: tuple[int, int, int, int]`

```python
Solid(color=(0, 0, 0, 255))
```

### `GeneratedCard`

Text card rendered at compile time.

Fields:

- `kind: Literal['generated_card']` = 'generated_card'
- `text: str`
- `font: str` = 'DejaVuSans'
- `size: int` = 64
- `fg: tuple[int, int, int, int]` = (255, 255, 255, 255)
- `bg: tuple[int, int, int, int]` = (0, 0, 0, 255)

```python
GeneratedCard(text="Episode 4", size=96)
```

### `AudioFile`

Fields:

- `kind: Literal['audio_file']` = 'audio_file'
- `path: Path | str`

```python
AudioFile(path="assets/music.mp3")
```

### `TTSGenerated`

Fields:

- `kind: Literal['tts_generated']` = 'tts_generated'
- `text: str`
- `voice: str` = 'default'
- `backend: str` = 'kokoro'

```python
TTSGenerated(text="Welcome back!", voice="default")
```

### `SeparatedStem`

Audio source separated via Demucs HTDemucs into vocals/drums/bass/other.

    The renderer runs Demucs once per ``(source path, model)`` pair and caches
    the four stems on disk; the segment's effective audio is the mix of the
    listed ``stems``. When a single stem is requested the cached file is used
    directly; multiple stems are mixed via ffmpeg ``amix``.

    Lazy-loaded; requires ``pip install eks-harness[stems]``.

Fields:

- `kind: Literal['separated_stem']` = 'separated_stem'
- `source: AudioFile`
- `stems: list[Literal['vocals', 'drums', 'bass', 'other']]`
- `model: Literal['htdemucs', 'htdemucs_ft', 'mdx_extra']` = 'htdemucs'

```python
SeparatedStem(
    source=AudioFile(path="assets/song.mp3"),
    stems=["vocals"],
    model="htdemucs",
)
```

### `HTMLOverlay`

HTML+CSS template rendered via headless Chromium with frame-
    deterministic virtual-time capture.

    The renderer drives Playwright's headless Chromium, advances the page's
    virtual clock one frame at a time (Replit's
    ``Emulation.setVirtualTimePolicy`` pattern), screenshots each frame,
    and encodes the resulting PNG sequence to mp4 via ffmpeg.

    No JavaScript is executed in the user's template - CSS animations
    only. Marker sync is achieved by baking extra ``@keyframes`` /
    ``animation-delay`` declarations into the page at compile time, one
    rule per marker trigger that falls inside the segment window.

    Lazy-loaded; requires ``pip install eks-harness[html] && playwright
    install chromium``.

Fields:

- `kind: Literal['html_overlay']` = 'html_overlay'
- `template: Path | str`
- `style_vars: dict[str, str] | None` = None
- `viewport: tuple[int, int] | None` = None
- `transparent: bool` = True
- `marker_animations: list[media.MarkerAnimation]` = []

```python
HTMLOverlay(
    template="overlays/title.html",
    style_vars={"--accent": "#ff0066"},
    transparent=True,
    marker_animations=[
        MarkerAnimation(
            stream="kick", selector=".flash",
            animation_name="pop", duration=0.3,
        )
    ],
)
```

### `MarkerAnimation`

Binds a CSS ``@keyframes`` animation to a marker stream.

    At compile time, the HTML renderer generates one CSS rule per trigger
    time in ``markers.streams[stream]`` that falls inside the segment's
    ``[start, end]`` window. Each rule applies ``animation_name`` to the
    given ``selector`` at ``trigger_t - segment_start + offset_ms / 1000``
    seconds.

    The user's HTML template must declare the corresponding
    ``@keyframes <animation_name> { ... }`` in its own CSS. No JavaScript
    is involved; marker sync is purely declarative.

Fields:

- `stream: str`
- `selector: str`
- `animation_name: str`
- `duration: float`
- `offset_ms: float` = 0.0

```python
MarkerAnimation(
    stream="kick", selector=".flash",
    animation_name="pop", duration=0.3, offset_ms=0.0,
)
```

## Marker sources

Declare beat / word / scene streams the renderer will resolve into
concrete frame indices. `BeatRef`, `WordRef` and `MarkerRef` time
references must reference a declared marker source by name.

### `BeatTracker`

Beat / kick / snare / downbeat extraction from an audio source.

    ``source`` references either an audio track name or a path-like asset id;
    the marker extractor plugin resolves it.

    ``backend`` selects the extraction backend. ``"auto"`` (the default)
    tries BeatNet, then madmom, then librosa, then falls back to an
    evenly-spaced stub. Explicit values pin a backend for reproducibility;
    if the pinned backend is unavailable, the extractor logs a warning and
    falls back through the remaining backends in auto order.

Fields:

- `kind: Literal['beat_tracker']` = 'beat_tracker'
- `name: str`
- `source: str`
- `streams: list[Literal['kick', 'snare', 'beat', 'downbeat']]` = ['kick', 'snare', 'beat', 'downbeat']
- `bpm: float | None` = None
- `backend: Literal['auto', 'beatnet', 'madmom', 'librosa']` = 'auto'

```python
BeatTracker(name="kicks", source="audio_tracks[0]", streams=["kick"])
```

### `STTMarkers`

Fields:

- `kind: Literal['stt_markers']` = 'stt_markers'
- `name: str`
- `source: str`
- `backend: Literal['faster-whisper', 'whisperx', 'mlx-whisper']` = 'faster-whisper'

```python
STTMarkers(name="speech", source="tracks[0].segments[0]")
```

### `SceneMarkers`

Fields:

- `kind: Literal['scene_markers']` = 'scene_markers'
- `name: str`
- `source: str`
- `threshold: float` = 27.0

```python
SceneMarkers(name="scenes", source="tracks[0].segments[0]")
```

### `SilenceMarkers`

Silent-region markers from an audio source.

    Emits one marker per detected silent region whose RMS stays below
    ``threshold_db`` for at least ``min_duration`` seconds. The marker
    sits at the *start* of each silent region, mirroring how other
    marker sources publish event times rather than ranges.

Fields:

- `kind: Literal['silence_markers']` = 'silence_markers'
- `name: str`
- `source: str`
- `threshold_db: float` = -40.0
- `min_duration: float` = 0.3

```python
SilenceMarkers(name="silences", source="audio_tracks[0]", threshold_db=-40.0, min_duration=0.3)
```

### `OnsetMarkers`

Generic audio onset markers.

    Broader than :class:`BeatTracker`: any perceptible onset, not just
    musical pulses. Useful for sound-design cues, foley hits, or
    speech-onset alignment.

Fields:

- `kind: Literal['onset_markers']` = 'onset_markers'
- `name: str`
- `source: str`
- `sensitivity: float` = 0.5

```python
OnsetMarkers(name="onsets", source="audio_tracks[0]", sensitivity=0.5)
```

### `EnergyMarkers`

Band-filtered RMS energy markers.

    Emits markers at moments where the band-filtered RMS exceeds the
    ``percentile`` percentile measured over the whole source. Useful for
    "hype" or peak-energy detection.

Fields:

- `kind: Literal['energy_markers']` = 'energy_markers'
- `name: str`
- `source: str`
- `band: Literal['low', 'mid', 'high', 'full']` = 'full'
- `percentile: float` = 0.85

```python
EnergyMarkers(name="hype", source="audio_tracks[0]", band="low", percentile=0.85)
```

### `MomentumMarkers`

Musical momentum analysis: drops, builds, breaks, sections and peaks.

    Fuses loudness, onset density, low-end weight and spectral brightness
    into one smoothed ``momentum`` curve in ``[0, 1]`` and publishes the
    structural moments of the song as separate streams named
    ``<name>.drop``, ``<name>.build``, ``<name>.break``, ``<name>.section``
    and ``<name>.peak``. ``MarkerRef(name="<name>.drop", index=0)`` lands
    on the first drop. Drops and breaks snap to the strongest onset within
    ``snap_window`` seconds so cuts hit the transient, not the envelope.

    ``curve_out`` (workspace-relative) also writes the sampled curve and
    every event with its strength as JSON, for planning a timeline.

Fields:

- `kind: Literal['momentum_markers']` = 'momentum_markers'
- `name: str`
- `source: str`
- `smoothing: float` = 0.75
- `drop_threshold: float` = 0.22
- `break_threshold: float` = 0.2
- `min_build: float` = 1.5
- `min_section: float` = 4.0
- `snap_window: float` = 0.12
- `curve_out: str | None` = None

```python
MomentumMarkers()
```

### `FaceMarkers`

Face-presence markers from a video source.

    Emits the start time of every contiguous run of frames containing at
    least one face larger than ``min_size`` pixels. Backed by OpenCV's
    Haar cascade (no extra weights required).

Fields:

- `kind: Literal['face_markers']` = 'face_markers'
- `name: str`
- `source: str`
- `min_size: int` = 80

```python
FaceMarkers(name="faces", source="tracks[0].segments[0]", min_size=80)
```

### `MotionMarkers`

High-motion markers from a video source.

    Emits a marker for every frame whose mean absolute difference against
    the previous frame exceeds ``threshold``. Backed by OpenCV.

Fields:

- `kind: Literal['motion_markers']` = 'motion_markers'
- `name: str`
- `source: str`
- `threshold: float` = 30.0

```python
MotionMarkers(name="motion", source="tracks[0].segments[0]", threshold=30.0)
```

## Transitions

Attached as `Segment.transition_in` / `transition_out` (or on the
track via `Track.transitions`). `Cut` is the default and adds zero
frames. `Crossfade` overlaps adjacent segments by `duration`
seconds. `PluginTransition` defers to a registered plugin by name.

### `Cut`

Hard cut. Default for adjacent segments.

Fields:

- `kind: Literal['cut']` = 'cut'

```python
Cut()
```

### `Crossfade`

Fields:

- `kind: Literal['crossfade']` = 'crossfade'
- `duration: float`

```python
Crossfade(duration=0.4)
```

### `DipToBlack`

Fade outgoing segment to black, then fade in incoming.

    Renders via ffmpeg ``xfade=transition=fadeblack``.

Fields:

- `kind: Literal['dip_to_black']` = 'dip_to_black'
- `duration: float`

```python
DipToBlack(duration=0.5)
```

### `DipToWhite`

Fade outgoing segment to white, then fade in incoming.

    Renders via ffmpeg ``xfade=transition=fadewhite``.

Fields:

- `kind: Literal['dip_to_white']` = 'dip_to_white'
- `duration: float`

```python
DipToWhite(duration=0.5)
```

### `Wipe`

Hard-edged wipe in the given direction.

    Renders via ffmpeg ``xfade=transition=wipe<direction>``.

Fields:

- `kind: Literal['wipe']` = 'wipe'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'right'

```python
Wipe(duration=0.4, direction="right")
```

### `Slide`

Incoming segment slides over outgoing one.

    Renders via ffmpeg ``xfade=transition=slide<direction>``.

Fields:

- `kind: Literal['slide']` = 'slide'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'right'

```python
Slide(duration=0.4, direction="left")
```

### `Push`

Incoming segment pushes outgoing one off-screen.

    Renders via ffmpeg ``xfade=transition=squeeze<direction>`` - the
    squeeze presets are the closest analogue to a true push in ffmpeg's
    built-in xfade set.

Fields:

- `kind: Literal['push']` = 'push'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'left'

```python
Push(duration=0.4, direction="left")
```

### `GlitchTransition`

Digital-glitch transition between two segments.

    Not a plain ffmpeg ``xfade`` preset - the boundary is rendered as a
    blocky ``pixelize`` dissolve with a chromatic RGB-channel split
    (``rgbashift``) gated to the transition window, giving a genuine
    digital-corruption look rather than a smooth blend. ``intensity``
    scales the chroma-split magnitude (0 = none, 1 = strong).

Fields:

- `kind: Literal['glitch_transition']` = 'glitch_transition'
- `duration: float`
- `intensity: float` = 0.6

```python
GlitchTransition(duration=0.3, intensity=0.6)
```

### `ZoomTransition`

Zoom-blur push into the incoming segment.

    Renders via ffmpeg ``xfade=transition=zoomin``. Named ``ZoomTransition``
    rather than ``Zoom`` because :class:`eks_harness.video.ir.effects.Zoom` already
    occupies that name in the public namespace; the discriminator ``kind`` is
    still ``"zoom"``.

Fields:

- `kind: Literal['zoom']` = 'zoom'
- `duration: float`

```python
ZoomTransition(duration=0.4)
```

### `Dissolve`

Random-noise dissolve between segments.

    Renders via ffmpeg ``xfade=transition=dissolve``.

Fields:

- `kind: Literal['dissolve']` = 'dissolve'
- `duration: float`

```python
Dissolve(duration=0.5)
```

### `BlurThrough`

Blur-out / blur-in blend through the boundary.

    Renders via ffmpeg ``xfade=transition=hblur``.

Fields:

- `kind: Literal['blur_through']` = 'blur_through'
- `duration: float`

```python
BlurThrough(duration=0.4)
```

### `Iris`

Circular iris that opens onto or closes off the incoming segment.

    Renders via ffmpeg ``xfade=transition=circleopen`` (``mode="open"``) or
    ``circleclose`` (``mode="close"``).

Fields:

- `kind: Literal['iris']` = 'iris'
- `duration: float`
- `mode: Literal['open', 'close']` = 'open'

```python
Iris(duration=0.5, mode="open")
```

### `Radial`

Radial sweep wipe around the frame centre.

    Renders via ffmpeg ``xfade=transition=radial``.

Fields:

- `kind: Literal['radial']` = 'radial'
- `duration: float`

```python
Radial(duration=0.5)
```

### `Pixelize`

Mosaic pixelisation blend between segments.

    Renders via ffmpeg ``xfade=transition=pixelize``.

Fields:

- `kind: Literal['pixelize']` = 'pixelize'
- `duration: float`

```python
Pixelize(duration=0.4)
```

### `FadeGrays`

Desaturate to grayscale through the boundary, then recolour.

    Renders via ffmpeg ``xfade=transition=fadegrays``.

Fields:

- `kind: Literal['fade_grays']` = 'fade_grays'
- `duration: float`

```python
FadeGrays(duration=0.5)
```

### `LumaWipe`

Soft gradient (luma) wipe in the given direction.

    Renders via ffmpeg's built-in gradient wipes
    ``xfade=transition=smooth<direction>`` (smoothleft / smoothright /
    smoothup / smoothdown) - a feathered edge rather than the hard edge of
    :class:`Wipe`.

Fields:

- `kind: Literal['luma_wipe']` = 'luma_wipe'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'right'

```python
LumaWipe(duration=0.4, direction="right")
```

### `RGBShiftWipe`

Directional wipe with a chromatic RGB-channel split over the boundary.

    Not a plain xfade preset: an ``xfade=transition=wipe<direction>`` is
    followed by an ``rgbashift`` gated to the transition window, splitting the
    red and blue channels apart for a chromatic-aberration edge. ``intensity``
    scales the channel-split magnitude (0 = none, 1 = strong).

Fields:

- `kind: Literal['rgb_shift_wipe']` = 'rgb_shift_wipe'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'right'
- `intensity: float` = 0.6

```python
RGBShiftWipe(duration=0.4, direction="right", intensity=0.6)
```

### `WhipPan`

Motion-blurred whip-pan slide in the given direction.

    Not a plain xfade preset: an ``xfade=transition=slide<direction>`` is
    followed by a directional ``avgblur`` gated to the transition window,
    smearing the frame along the pan axis. ``intensity`` scales the blur
    kernel size (0 = none, 1 = strong).

Fields:

- `kind: Literal['whip_pan']` = 'whip_pan'
- `duration: float`
- `direction: Literal['left', 'right', 'up', 'down']` = 'right'
- `intensity: float` = 0.6

```python
WhipPan(duration=0.25, direction="left", intensity=0.7)
```

### `LightLeak`

Crossfade overlaid with a Gaussian brightness bloom peaking mid-window.

    Not a plain xfade preset: an ``xfade=transition=fade`` is followed by a
    time-varying ``eq`` brightness pulse (``eval=frame``) that swells to a
    peak at the centre of the transition window, mimicking a lens light
    leak. ``intensity`` scales the peak brightness (0 = none, 1 = strong).

Fields:

- `kind: Literal['light_leak']` = 'light_leak'
- `duration: float`
- `intensity: float` = 0.7

```python
LightLeak(duration=0.5, intensity=0.7)
```

### `FilmBurn`

Stylized warm film-burn over a crossfade boundary.

    A procedural approximation: a crossfade is overlaid with a Gaussian
    brightness bloom, a warm ``colorbalance`` push, and a gated ``rgbashift``
    chroma bleed. This is a synthesised burn from ffmpeg primitives, not a
    photoreal film-burn - a texture-overlay burn is future work. ``intensity``
    scales the bloom, chroma bleed, and overall strength (0 = none, 1 = strong).

Fields:

- `kind: Literal['film_burn']` = 'film_burn'
- `duration: float`
- `intensity: float` = 0.7

```python
FilmBurn(duration=0.5, intensity=0.7)
```

### `BeatFlash`

Crossfade that flashes white on every beat inside the transition window.

    Not a plain xfade preset: an ``xfade=transition=fade`` is overlaid with a
    sum of narrow Gaussian brightness bumps (``eq``, ``eval=frame``), one per
    beat of ``stream`` that falls inside the window. The renderer threads the
    window-local beat times from the project's resolved markers; when no beat
    lands in the window it falls back to a single pulse at mid-window so the
    transition is never a silent no-op. ``intensity`` scales the flash
    brightness (0 = none, 1 = strong).

Fields:

- `kind: Literal['beat_flash']` = 'beat_flash'
- `duration: float`
- `stream: str` = 'kick'
- `intensity: float` = 0.7

```python
BeatFlash(duration=0.5, stream="kick", intensity=0.7)
```

### `BeatGlitch`

Pixelize blend that bursts a chromatic glitch on every beat in the window.

    Not a plain xfade preset: an ``xfade=transition=pixelize`` is followed by an
    ``rgbashift`` whose channel split is gated to a short pulse around each beat
    of ``stream`` inside the transition window. The renderer threads the
    window-local beat times from the project's resolved markers; when no beat
    lands in the window it falls back to a single pulse at mid-window so the
    transition is never a silent no-op. ``intensity`` scales the channel-split
    magnitude (0 = none, 1 = strong).

Fields:

- `kind: Literal['beat_glitch']` = 'beat_glitch'
- `duration: float`
- `stream: str` = 'kick'
- `intensity: float` = 0.7

```python
BeatGlitch(duration=0.4, stream="kick", intensity=0.7)
```

### `PluginTransition`

Fields:

- `kind: Literal['plugin']` = 'plugin'
- `name: str`
- `params: dict[str, Any]` = {}

```python
PluginTransition(name="whip-pan", params={"angle": 30})
```

## Encoders and presets

Encoders are selected by name in `RenderSettings.encoder` (e.g.
`"libx264"`, `"nvenc"`, `"qsv"`, `"videotoolbox"`); when left
unset, the orchestrator probes available encoders and picks the
best fit for the host. Bitrate / CRF can be overridden per project
via `RenderSettings.bitrate_kbps` / `RenderSettings.crf`.

### Built-in encoder plugins

- `libx264` - software fallback, always available. Honors `crf`.
- `nvenc` - NVIDIA hardware. Subject to consumer-driver session limits.
- `qsv` - Intel Quick Sync. Linux/Windows.
- `videotoolbox` - Apple Silicon / macOS. Bitrate-only (does not honor `crf`).
- `svtav1` - software AV1 (SVT-AV1). Slow; use for `av1-experimental` delivery.

### Hardware acceleration and `EKS_HARNESS_HWACCEL`

When `RenderSettings.encoder` is unset, `eks_harness.video.render.hwaccel`
auto-selects the best HW encoder for the host (AMF → NVENC → QSV →
VideoToolbox, else `libx264`) and decodes with `-hwaccel auto`. Every
render leg - segments, transitions, HTML overlay, dev-server previews -
HW-encodes. `HTMLOverlay` also rasterises on the GPU
(`--use-angle=d3d11 --enable-gpu-rasterization`).

Set env var **`EKS_HARNESS_HWACCEL=0`** to force software libx264, drop
`-hwaccel`, and run Chromium in software - use it for reproducible,
byte-identical output across machines. Setting `RenderSettings.encoder`
explicitly still overrides the auto-probe.

ONNX-backed effects (rembg / BiRefNet matting, the `winml-whisper`
captioner) get their execution providers from
`eks_harness.video.render.winml_ep` (`onnx_providers()`): on Windows it
best-effort downloads + registers the AMD **MIGraphX** EP via Windows
ML's `ExecutionProviderCatalog`, falling back to CPU when unavailable.
It never uses DirectML (OOMs the big models on shared-VRAM APUs). The
MIGraphX EP needs a recent Adrenalin driver; older drivers fail its DLL
init (Error 1114) and the path transparently uses CPU. `EKS_HARNESS_HWACCEL=0`
also forces these onto CPU.

### Encoding presets

#### `tiktok-1080-h264`

```
codec=h264  1080x1920@30fps
bitrate=10M  maxrate=12M  bufsize=20M
profile=high  pixfmt=yuv420p  color=bt709
audio=aac@192k  movflags=+faststart
```

#### `reels-1080-h264`

```
codec=h264  1080x1920@30fps
bitrate=9M  maxrate=11M  bufsize=18M
profile=high  pixfmt=yuv420p  color=bt709
audio=aac@192k  movflags=+faststart
```

#### `shorts-1080-h264`

```
codec=h264  1080x1920@30fps
bitrate=12M  maxrate=14M  bufsize=24M
profile=high  pixfmt=yuv420p  color=bt709
audio=aac@192k  movflags=+faststart
```

#### `archival-h265`

```
codec=hevc  3840x2160@60fps
bitrate=40M  maxrate=60M  bufsize=80M
profile=main10  pixfmt=yuv420p10le  color=bt2020
audio=aac@320k  movflags=+faststart  tag=hvc1
```

#### `av1-experimental`

```
codec=av1  1920x1080@30fps
bitrate=6M  maxrate=8M  bufsize=12M
profile=main  pixfmt=yuv420p  color=bt709
audio=aac@192k  movflags=+faststart
```

## RenderSettings

Controls encoder selection, color and quality profile.

### `RenderSettings`

Fields:

- `encoder: str | None` = None
- `preset: str` = 'tiktok-1080-h264'
- `quality_profile: Literal['preview', 'draft', 'final']` = 'final'
- `color_primaries: str` = 'bt709'
- `color_trc: str` = 'bt709'
- `colorspace: str` = 'bt709'
- `pix_fmt: str` = 'yuv420p'
- `bitrate_kbps: int | None` = None
- `crf: int | None` = None

```python
RenderSettings(
    encoder="libx264",
    preset="tiktok-1080-h264",
    quality_profile="final",
    crf=18,
)
```

## Project / tracks / segments

The root timeline. A `Project` holds video tracks, audio tracks,
marker sources and render settings. Segments are placed on tracks at
an explicit `start` time and bounded by `in_` / `out` (source-time).

### `Project`

Root timeline model.

    Holds tracks, audio tracks, marker declarations, render settings and a
    list of plugin requirements. The ``schema_version`` field is reserved for
    future migrations.

    ``random_seed`` is the global salt for reproducible randomness across
    every ``Noise``, ``RandomChoice``, ``RandomTrigger`` (and similar)
    node in the project. Per-node seeds are combined with this salt so
    that a project rendered twice with the same ``random_seed`` produces
    identical output.

Fields:

- `schema_version: str` = '0.1'
- `fps: float`
- `resolution: tuple[int, int]`
- `duration: float`
- `tracks: list[tracks.Track]` = []
- `audio_tracks: list[tracks.AudioTrack]` = []
- `markers: list[Annotated[markers.BeatTracker | markers.STTMarkers | markers.SceneMarkers | markers.SilenceMarkers | markers.OnsetMarkers | markers.EnergyMarkers | markers.MomentumMarkers | markers.FaceMarkers | markers.MotionMarkers, FieldInfo(annotation=None, required=True, discriminator='kind')]]` = []
- `render_settings: RenderSettings` = <RenderSettings>
- `plugins: list[project.PluginRequirement]` = []
- `random_seed: int` = 0
- `metadata: dict[str, Any]` = {}

### `Track`

Video track. ``z`` is the compositing depth (higher = on top).

Fields:

- `name: str`
- `z: int` = 0
- `blend: Literal['normal', 'add', 'multiply', 'screen', 'overlay']` = 'normal'
- `opacity: Animated[float]` = Animated[float](root=1.0)
- `segments: list[tracks.Segment]` = []
- `transitions: list[Annotated[transitions.Cut | transitions.Crossfade | transitions.DipToBlack | transitions.DipToWhite | transitions.Wipe | transitions.Slide | transitions.Push | transitions.GlitchTransition | transitions.ZoomTransition | transitions.Dissolve | transitions.BlurThrough | transitions.Iris | transitions.Radial | transitions.Pixelize | transitions.FadeGrays | transitions.LumaWipe | transitions.RGBShiftWipe | transitions.WhipPan | transitions.LightLeak | transitions.FilmBurn | transitions.BeatFlash | transitions.BeatGlitch | transitions.PluginTransition, FieldInfo(annotation=None, required=True, discriminator='kind')]]` = []

### `Segment`

A clip placed on a video ``Track``.

Fields:

- `id: str`
- `start: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef`
- `media: media.VideoFile | media.ImageFile | media.Solid | media.GeneratedCard | media.AudioFile | media.TTSGenerated | media.SeparatedStem | media.HTMLOverlay`
- `in_: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef` = Seconds(kind='seconds', t=0.0)
- `out: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef`
- `speed: Animated[float]` = Animated[float](root=1.0)
- `effects: list[Annotated[Union[effects.Brightness, effects.Contrast, effects.Saturation, effects.Crop, effects.Mirror, effects.Zoom, effects.Pan, effects.SpeedRamp, effects.Freeze, effects.ChromaKey, effects.ColorGrade, effects.Fade, effects.Blur, effects.Sharpen, effects.Vignette, effects.FilmGrain, effects.LUT, effects.Invert, effects.Posterize, effects.TextOverlay, effects.LowerThird, effects.Watermark, effects.StickerOverlay, effects.Flash, effects.RGBSplit, effects.Glitch, effects.Shake, effects.VHS, effects.Datamosh, effects.Captions, effects.MatAnyoneRemove, effects.BiRefNetRemove, effects.SAM3Track, effects.AutoReframe, effects.PunchIn], FieldInfo(annotation=None, required=True, discriminator='kind')]]` = []
- `transition_in: transitions.Cut | transitions.Crossfade | transitions.DipToBlack | transitions.DipToWhite | transitions.Wipe | transitions.Slide | transitions.Push | transitions.GlitchTransition | transitions.ZoomTransition | transitions.Dissolve | transitions.BlurThrough | transitions.Iris | transitions.Radial | transitions.Pixelize | transitions.FadeGrays | transitions.LumaWipe | transitions.RGBShiftWipe | transitions.WhipPan | transitions.LightLeak | transitions.FilmBurn | transitions.BeatFlash | transitions.BeatGlitch | transitions.PluginTransition` = Cut(kind='cut')
- `transition_out: transitions.Cut | transitions.Crossfade | transitions.DipToBlack | transitions.DipToWhite | transitions.Wipe | transitions.Slide | transitions.Push | transitions.GlitchTransition | transitions.ZoomTransition | transitions.Dissolve | transitions.BlurThrough | transitions.Iris | transitions.Radial | transitions.Pixelize | transitions.FadeGrays | transitions.LumaWipe | transitions.RGBShiftWipe | transitions.WhipPan | transitions.LightLeak | transitions.FilmBurn | transitions.BeatFlash | transitions.BeatGlitch | transitions.PluginTransition` = Cut(kind='cut')

### `AudioTrack`

Audio track with full ``Animated`` parity for gain / pan / EQ.

Fields:

- `name: str`
- `gain_db: Animated[float]` = Animated[float](root=0.0)
- `pan: Animated[float]` = Animated[float](root=0.0)
- `eq: Animated[EQ] | None` = None
- `sidechain: tracks.SidechainConfig | None` = None
- `loudness: audio_effects.LoudnessNormalize | None` = None
- `parametric_eq: audio_effects.EQEffect | None` = None
- `segments: list[tracks.AudioSegment]` = []

### `AudioSegment`

A clip placed on an ``AudioTrack``.

Fields:

- `id: str`
- `start: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef`
- `media: media.VideoFile | media.ImageFile | media.Solid | media.GeneratedCard | media.AudioFile | media.TTSGenerated | media.SeparatedStem | media.HTMLOverlay`
- `in_: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef` = Seconds(kind='seconds', t=0.0)
- `out: time.Seconds | time.Frames | time.BeatRef | time.WordRef | time.MarkerRef`
- `gain_db: Animated[float]` = Animated[float](root=0.0)
- `ducking: audio_effects.Ducking | None` = None
- `fade: audio_effects.AudioFade | None` = None
- `reverb: audio_effects.Reverb | None` = None
- `denoise: audio_effects.DeepFilterDenoise | None` = None

### `EQ`

Three-band EQ used by ``AudioTrack.eq``.

Fields:

- `low_db: float` = 0.0
- `mid_db: float` = 0.0
- `high_db: float` = 0.0

### `SidechainConfig`

Auto-ducking config attaching this track to a source track.

Fields:

- `source: str`
- `threshold_db: float` = -20.0
- `ratio: float` = 4.0
- `attack_ms: float` = 10.0
- `release_ms: float` = 200.0

### `PluginRequirement`

Pinned plugin requirement declared at project level.

Fields:

- `name: str`
- `version_spec: str` = '*'

## Animated values

`Animated[T]` accepts three shapes:

- A bare value: `Brightness(amount=0.5)`
- A keyframe list:
  ```python
  Brightness(amount=Animated[float](root=[
      Keyframe[float](t=Seconds(t=0.0), v=0.0, easing=EaseInOut()),
      Keyframe[float](t=Seconds(t=2.0), v=1.0),
  ]))
  ```
- A curve:
  ```python
  Brightness(amount=Animated[float](root=BeatPulse(
      trigger=BeatRef(stream="kick"),
      envelope=ExpDecayEnv(tau=0.08),
      intensity_ramp=0.6,
  )))
  ```

Keyframe `interp` modes are `linear`, `bezier`, `hold`, `spline`. The
`easing` field selects the per-segment shaping function (see
[Easings](#easings)).

