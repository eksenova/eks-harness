---
name: eks-gfx-audio
description: "2D motion graphics and the audio mix in eks: @eks.scene(kind=\"gfx\") with text, rect, ellipse, path, image, video and group layers, animated properties, gradients, masks, blend modes, beat and time triggers (pulse, assign, tween), shipped fonts and CPU or GPU rendering; the edit's audio mix with gain, pan, ducking, EQ, compression, fades, reverb, denoise, stems, beat analysis and a loudness-normalised, true-peak-limited master. Use for title cards, lower thirds, animated badges, music beds, voice-over mixes and anything an ad needs besides footage."
---

# gfx and audio

## Motion graphics (`@eks.scene(kind="gfx")`)

```python
import eks
from eks import t


@eks.scene(kind="gfx", size=(720, 1280), fps=30, duration=t("6s"), cpu=True)
def title_card(sc: eks.GfxScene):
    sc.rect(id="panel", width=640.0, height=360.0, radius=36.0, fill="#0d1b2a", y=-120.0)
    title = sc.text("EksenGOLD", id="title", y=-160.0, size=72.0, color="#f5c542")
    sc.text("Anlık kur", id="subtitle", y=-70.0, size=40.0, start=t("1s"))
    sc.image("../assets/logo.svg", id="logo", y=220.0, width=200.0, height=200.0)
    title.animate("opacity", {t("0s"): 0.0, t("600ms"): 1.0}, ease="out_cubic")
    sc.on(t.set(t("2s"), t("3s"), t("4s")), title.pulse(amount=1.08, duration=t("300ms")))


@eks.scene(kind="gfx", size=(1080, 1920), fps=30)
def lower_third(sc: eks.GfxScene):
    with sc.group(id="bar", y=700.0, start=t("bar:1")) as bar:
        plate = sc.rect(960.0, 180.0, id="plate", radius=24.0, fill="#0b1f3a")
        sc.text("Harman Kuyumculuk", id="name", y=-24.0, size=56.0, color="#f5c542")
        sc.path("M -440 70 L 440 70", id="rule", stroke="#f5c542", stroke_width=4.0,
                trim_end=eks.keys({t("bar:1"): 0.0, t("bar:2"): 1.0}, ease="out_cubic"))
    clip = sc.video("artifact:VID123", id="screen", y=-100.0, width=900.0, src=t.span(t("0s"), t("6s")))
    window = sc.ellipse(900.0, 900.0, id="window", y=-100.0, fill="#ffffff")
    sc.mask(clip, window)
    bar.animate("x", {t("bar:1"): -1080.0, t("bar:1") + t("12f"): 0.0}, ease="out_cubic")
    plate.animate("opacity", {t("bar:1"): 0.0, t("bar:1") + t("6f"): 1.0})
```

- Layers: `text(text, size=64.0, font="IBM Plex Sans", weight=600, color=, align=, max_width=, line_height=,
  tracking=)`, `rect(width, height, radius=, fill=, stroke=, stroke_width=)`, `ellipse(width, height, ...)`,
  `path(d, fill=, stroke=, trim_start=, trim_end=)` (SVG path data), `image(media, width=, height=, fit=)`
  (raster or SVG), `video(media, width=, height=, fit=, src=, speed=)`, `with group(id=, x=, y=, blend=)`.
  Every layer takes `id=`, `x=`, `y=` (relative to the frame centre), `start=` and `end=`.
- Animated properties take a number, `eks.keys({...}, ease=)` or a curve. `handle.animate(prop, keys, ease=)`,
  `handle.set(**props)`, `handle.show(start, end)`.
- Triggers: `sc.on(trigger, *actions)` with a `Time`, a `TimeSet` (`t.beats("downbeat")[0:8]`, `t.set(...)`)
  and the actions `handle.pulse(amount=, prop="scale", duration=, ease=)`, `handle.assign(prop, value)`,
  `handle.tween(prop, keys, ease=)`.
- Paint: a colour (`"#f5c542"`, `"#f5c54280"`) or `eks.gfx.linear_gradient((x0, y0), (x1, y1), [(0.0, "#000000"),
  (1.0, "#ffffff")])` / `radial_gradient(center, radius, stops)`.
- `sc.mask(layer, mask, mode=)` with `alpha`, `luminance`, `alpha_inverted`, `luminance_inverted`. Group
  blend modes: `normal`, `add`, `multiply`, `screen`, `overlay`, `src_atop`, `dst_in`, `dst_out`, `xor`,
  `darken`, `lighten`, `color_dodge`, `color_burn`, `soft_light`, `hard_light`, `difference`, `exclusion`.
- Fonts are strict: `IBM Plex Sans` (400, 600) and `IBM Plex Mono` (400) ship; any other family is a font
  file (`font="assets/Brand-Bold.ttf"`). Another weight of a shipped family fails with `font_unavailable`.
- `cpu=True` renders on the CPU reference path (byte-identical everywhere the same skia-python runs); the GPU
  path (Skia GL) is faster and keyed by adapter and driver.
- Beat and bar times need a clock: `bpm=` on the scene, or the song of the edit or score that embeds it.
  A standalone scene without a tempo cannot run.

Run a scene alone or embed it:

```bash
eks-harness run .harness/scenes/cards.py::title_card     # a transparent .mov (PNG codec, RGBA)
```

```python
e.track("card", z=1).clip(e.gfx("title_card"), start=t("0s")).transition_out(eks.tx.crossfade(t("500ms")))
```

## Audio mix

Every edit (and score) has one mix, `e.audio`; it renders to 24-bit WAV and joins the video.

```python
import eks
from eks import t
from eks.audio import EqBand


@eks.edit(fps=30, size=(640, 360), seed=41, duration=t("10s"), background="#0d1b2a")
def mix_only(e: eks.Edit):
    e.render_settings(encoder="libx264", quality="draft", threads=4)
    music = e.audio.track("../assets/music.wav", name="music", gain=-4, pan=-0.2)
    voice = e.audio.track("../assets/vo.wav", name="voice", start=t("1s"), pan=0.1)
    music.duck("voice", amount_db=-10).fade(fade_in=t("300ms"), fade_out=t("800ms"))
    voice.eq(EqBand(frequency_hz=120, gain_db=-6, type="high_pass"), EqBand(frequency_hz=3000, gain_db=2))
    voice.compress(threshold_db=-20, ratio=3, attack=t("10ms"), release=t("120ms"), makeup_db=2)
    e.audio.master(loudness=-14, true_peak=-1)
    e.expect.loudness(-14, true_peak=-1)
```

- `e.audio.track(media, name=, gain=0.0, pan=0.0, start=, src=, mute=False)`; gain in dB and pan in -1..1,
  both animatable.
- Per track: `.duck(under, amount_db=-12.0, threshold_db=-30.0, attack=, release=)`, `.eq(*EqBand(frequency_hz,
  gain_db=, q=, type="peak"|"low_shelf"|"high_shelf"|"low_pass"|"high_pass"))`, `.compress(threshold_db=,
  ratio=, attack=, release=, makeup_db=)`, `.fade(fade_in=, fade_out=)`, `.reverb(wet=, room_size=, damping=)`
  (seeded), `.denoise(attenuation_limit_db=)`, `.stems("drums", "bass", "other", "vocals")`, `.set(...)`.
- `.beats("beat", "downbeat", bpm=None, backend="auto")` analyses the track and becomes the clock's tempo
  source; `.markers(*sources)` adds onset, energy, silence, scene, momentum, motion, face or speech markers
  (`eks.media.OnsetMarkers(...)`, `STTMarkers(...)`, ...).
- `e.audio.master(loudness=, true_peak=, limiter=True, gain=0.0)` normalises integrated loudness (BS.1770-4)
  and limits true peak with 4x oversampling.
- Clip audio joins the mix with `track.clip(..., audio=True, gain=...)`.
- Not available: `master(lra=...)` is refused; `stems` and `denoise` need Demucs and DeepFilterNet installed;
  `eks.media.tts(...)` has no speech engine installed yet.

Generated reference: `docs/reference/ops.md` (families `gfx`, `audio`, `media`).
