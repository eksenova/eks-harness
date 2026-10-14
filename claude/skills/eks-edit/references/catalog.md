# Edit catalog

Builder signatures from `src/eks/effects.py`. `AnimFloat` and `AnimInt` take a number, `eks.keys(...)` or a
curve; `Time` values use the `t(...)` grammar. The generated `docs/reference/catalog.md` in the eks-harness
repository has every field with its description.

## eks.fx

```text
fx.brightness(amount: AnimFloat)
fx.contrast(amount: AnimFloat)
fx.saturation(amount: AnimFloat)
fx.crop(x: AnimInt, y: AnimInt, w: AnimInt, h: AnimInt)
fx.mirror(axis: Literal[horizontal, vertical, both] = 'horizontal')
fx.zoom(start: float, end: float | None = None, *, ease: EaseLike = None, cx: AnimFloat = 0.5, cy: AnimFloat = 0.5)
fx.pan(dx: AnimInt, dy: AnimInt)
fx.speed(factor: AnimFloat)
fx.freeze(at: Time, hold: Time)
fx.chroma_key(color: str = '#00ff00', *, similarity: float = 0.1, blend: float = 0.0)
fx.color_grade(*, lut: MediaLike | None = None, exposure: AnimFloat = 0.0, temperature: AnimFloat = 0.0, tint: AnimFloat = 0.0)
fx.fade(direction: Literal[in, out], duration: Time)
fx.blur(radius: AnimFloat)
fx.sharpen(amount: AnimFloat)
fx.vignette(*, angle: float = 0.628, x0: float = 0.5, y0: float = 0.5)
fx.film_grain(intensity: AnimFloat, *, seed: int | None = None)
fx.lut(path: MediaLike)
fx.invert()
fx.posterize(levels: AnimInt)
fx.text(text: Text, *, font: FontLike = 'IBM Plex Sans', size: int = 48, color: str = '#ffffffff', x: AnimInt = 100, y: AnimInt = 100, start: Time | None = None, duration: Time | None = None)
fx.lower_third(title: Text, subtitle: Text = '', *, font: FontLike = 'IBM Plex Sans', position: Literal[left, center, right] = 'left', margin: int = 120)
fx.watermark(path: MediaLike, *, x: AnimInt = 20, y: AnimInt = 20, opacity: AnimFloat = 1.0, scale: AnimFloat = 1.0)
fx.sticker(path: MediaLike, *, x: AnimInt = 0, y: AnimInt = 0, scale: AnimFloat = 1.0, rotation: AnimFloat = 0.0)
fx.flash(at: Time | TimeSet, duration: Time, *, color: str = '#ffffff', curve: CurveBase | None = None)
fx.rgb_split(offset_x: AnimInt, offset_y: AnimInt = 0)
fx.glitch(intensity: AnimFloat, *, block_size: int = 16, seed: int | None = None)
fx.shake(intensity: AnimFloat, *, freq_hz: float = 6.0, decay: float = 0.0, seed: int | None = None)
fx.vhs(*, scanlines: bool = True, chroma_blur: float = 1.5, noise: float = 0.05)
fx.datamosh(intensity: AnimFloat = 0.5)
fx.captions(source: str, **options: Unpack[CaptionOptions])
fx.matanyone_remove(*, background: str = '#00000000')
fx.birefnet_remove(*, background: str = '#00000000')
fx.sam3_track(prompt: str, *, output: Literal[mask, matte, highlight] = 'mask', confidence_threshold: float = 0.5)
fx.auto_reframe(*, target_aspect: tuple[int, int] = (9, 16), tracker: Literal[face, salience] = 'face', smoothing: float = 0.85)
fx.punch_in(*, zoom: AnimFloat = 1.2, trigger: Time | TimeSet | None = None, track_speaker: bool = True)
fx.custom(kind: str, **params: Any)
```

`fx.captions` options: `font`, `size`, `color`, `stroke`, `stroke_width`, `position` (`top`, `center`, `bottom`),
`margin`, `style` (`tiktok`, `subtitle`, `kinetic`), `word_animation` (`pop`, `fade`, `none`), `max_words_per_card`.

## eks.tx

```text
tx.cut()
tx.crossfade(duration: Time)
tx.dip_to_black(duration: Time)
tx.dip_to_white(duration: Time)
tx.wipe(duration: Time, *, direction: Direction = 'right')
tx.slide(duration: Time, *, direction: Direction = 'right')
tx.push(duration: Time, *, direction: Direction = 'left')
tx.glitch(duration: Time, *, intensity: float = 0.6)
tx.zoom(duration: Time)
tx.dissolve(duration: Time)
tx.blur_through(duration: Time)
tx.iris(duration: Time, *, mode: Literal[open, close] = 'open')
tx.radial(duration: Time)
tx.pixelize(duration: Time)
tx.fade_grays(duration: Time)
tx.luma_wipe(duration: Time, *, direction: Direction = 'right')
tx.rgb_shift_wipe(duration: Time, *, direction: Direction = 'right', intensity: float = 0.6)
tx.whip_pan(duration: Time, *, direction: Direction = 'right', intensity: float = 0.6)
tx.light_leak(duration: Time, *, intensity: float = 0.7)
tx.film_burn(duration: Time, *, intensity: float = 0.7)
tx.beat_flash(duration: Time, *, stream: str = 'beat', intensity: float = 0.7)
tx.beat_glitch(duration: Time, *, stream: str = 'beat', intensity: float = 0.7)
tx.custom(kind: str, **params: Any)
```

## eks.curves

```text
curves.sine(freq_hz: float, *, phase: float = 0.0, amp: float = 1.0, offset: float = 0.0)
curves.lfo(shape: Literal[tri, saw, square], freq_hz: float, *, amp: float = 1.0, offset: float = 0.0, phase: float = 0.0)
curves.noise(*, seed: int | None = None, min_value: float = 0.0, max_value: float = 1.0, hold: Time | None = None)
curves.random_choice(values: Sequence[float], hold: Time | None = None, *, seed: int | None = None)
curves.step(values: Sequence[float], hold: Time)
curves.spring(target: float, *, stiffness: float = 100.0, damping: float = 10.0, initial: float = 0.0)
curves.bezier(p0: float, p1: float, p2: float, p3: float, duration: Time)
curves.bounce(amplitude: float, period: Time, *, decay: float = 1.0)
curves.exp_decay(tau: Time, *, baseline: float = 0.0)
curves.expr(expr: str)
curves.beat_pulse(trigger: TimeSet | Time, envelope: Envelope, *, intensity: float | Keys[float] = 1.0, baseline: float = 0.0)
curves.exp_decay_env(tau: Time)
curves.linear_env(rise: Time, fall: Time)
curves.box_env(width: Time)
curves.easing_env(duration: Time, ease: EaseLike, *, direction: Literal[in, out] = 'out')
curves.custom(kind: str, **params: Any)
```

`curves.expr` takes a small arithmetic language over `t` (seconds): numbers, `pi`, `e`, `tau`, `+ - * / // % **`,
comparisons, `and`/`or`/`not`, `a if c else b` and `sin cos tan asin acos atan sinh cosh tanh exp log log2 log10 sqrt
abs floor ceil atan2 pow clamp min max hypot`. Anything else is a plan error.

## Easings

`eks.ease(name)` or `ease="name"`: `cubic_bezier`, `in_cubic`, `in_expo`, `in_out_cubic`, `in_out_expo`, `in_out_quad`, `in_out_quart`, `in_out_sine`, `in_quad`, `in_quart`, `in_sine`, `linear`, `out_back`, `out_bounce`, `out_cubic`, `out_elastic`, `out_expo`, `out_quad`, `out_quart`, `out_sine`.

## Keys

```python
eks.keys({t("0s"): 1.0, t("2s"): 0.3, t("4s"): 2.0}, ease="in_out_cubic")
```

Key times are clip-local (`t.start` is the clip's start). `ease` applies to every key after the first.
