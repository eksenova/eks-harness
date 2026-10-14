from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image as PILImage
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.ir.catalog import effects as fx
from eks_harness.ir.catalog.animated import Driven
from eks_harness.ir.catalog.curves import Sine
from eks_harness.ir.catalog.effects import EffectBase
from eks_harness.ir.catalog.keys import Keyframe, Keys
from eks_harness.ir.refs import RefExpr
from eks_harness.ir.time import TICKS_PER_SECOND, TimeContext, TimeSet, Word, parse_time
from eks_harness.media.animate import Timebase
from eks_harness.media.edit.effects import (
    TIMING_EFFECTS,
    EffectContext,
    EffectFrame,
    EffectRuntime,
    inference_record,
    open_effect,
)
from eks_harness.media.edit.fx.text import typeface

Image = NDArray[np.uint8]
FPS = Fraction(30)
WIDTH, HEIGHT = 96, 64
START, COUNT = 30, 30
TICKS_PER_FRAME = TICKS_PER_SECOND // 30


def synthetic() -> Image:
    ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
    image = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    image[..., 0] = (xs * 255 // (WIDTH - 1)).astype(np.uint8)
    image[..., 1] = (ys * 255 // (HEIGHT - 1)).astype(np.uint8)
    image[..., 2] = 64
    image[..., 3] = 255
    image[10:20, 10:30, :3] = 255
    circle = (xs - 70) ** 2 + (ys - 40) ** 2 < 100
    image[circle, :3] = 0
    image[50:60, 5:25, 3] = 128
    return image


def no_media(value: JsonValue) -> Path:
    raise AssertionError(f"unexpected media lookup {value!r}")


def media_of(files: Mapping[str, Path]) -> Callable[[JsonValue], Path]:
    def lookup(value: JsonValue) -> Path:
        assert isinstance(value, dict)
        ref = value["$ref"]
        assert isinstance(ref, str)
        return files[ref]

    return lookup


def ref(name: str) -> RefExpr:
    return RefExpr.model_validate({"$ref": name})


def base(seed: int = 7, start: int = START, count: int = COUNT) -> Timebase:
    origin = start * TICKS_PER_FRAME
    length = count * TICKS_PER_FRAME
    context = TimeContext.static(fps=FPS, end=origin + length + TICKS_PER_SECOND)
    return Timebase(context=context, origin=origin, length=length, seed=seed)


def make(effect: EffectBase, *, seed: int = 7, files: Mapping[str, Path] | None = None,
         words: Mapping[str, Sequence[Word]] | None = None) -> EffectRuntime:
    media = media_of(files) if files is not None else no_media
    return open_effect(effect, base=base(seed), fps=FPS, frames=range(START, START + COUNT), size=(WIDTH, HEIGHT),
                       media=media, words=words)


def frame(index: int) -> EffectFrame:
    return EffectFrame(index=index, seconds=index / 30, local=(index - START) / 30)


def run(runtime: EffectRuntime, index: int = START, image: Image | None = None) -> Image:
    return runtime.apply(synthetic() if image is None else image, frame(index))


def ramp(start: float, end: float) -> Keys[float]:
    return Keys[float](keys=(Keyframe[float](t=parse_time("0s"), v=start), Keyframe[float](t=parse_time("1s"), v=end)))


def int_ramp(start: int, end: int) -> Keys[int]:
    return Keys[int](keys=(Keyframe[int](t=parse_time("0s"), v=start), Keyframe[int](t=parse_time("1s"), v=end)))


def rgb_mean(image: Image) -> float:
    return float(image[..., :3].astype(np.float64).mean())


def changed(a: Image, b: Image) -> int:
    return int(np.count_nonzero(np.any(a != b, axis=-1)))


COLOR_EFFECTS: list[EffectBase] = [
    fx.Brightness(amount=0.2),
    fx.Contrast(amount=1.5),
    fx.Saturation(amount=0.0),
    fx.ColorGrade(exposure=0.5, temperature=0.2, tint=-0.1),
    fx.Vignette(angle=0.9),
    fx.FilmGrain(intensity=12.0),
    fx.Invert(),
    fx.Posterize(levels=3),
    fx.Sharpen(amount=2.0),
    fx.VHS(),
    fx.Flash(at=parse_time("0s"), duration=parse_time("0.5s"), color="#ff0000"),
]


@pytest.mark.parametrize("effect", COLOR_EFFECTS, ids=lambda effect: effect.kind)
def test_color_effects_change_rgb_and_keep_alpha(effect: EffectBase) -> None:
    image = synthetic()
    out = run(make(effect), START, image)
    assert out.shape == image.shape and out.dtype == np.uint8
    assert changed(out, image) > 0
    assert np.array_equal(out[..., 3], image[..., 3])


def test_brightness_contrast_saturation_directions() -> None:
    image = synthetic()
    assert rgb_mean(run(make(fx.Brightness(amount=0.2)))) > rgb_mean(image)
    assert rgb_mean(run(make(fx.Brightness(amount=-0.2)))) < rgb_mean(image)
    flat = run(make(fx.Contrast(amount=0.0)))
    assert np.all(np.abs(flat[..., :3].astype(int) - 128) <= 1)
    gray = run(make(fx.Saturation(amount=0.0)))
    assert np.all(np.abs(gray[..., 0].astype(int) - gray[..., 1].astype(int)) <= 1)
    inverted = run(make(fx.Invert()))
    assert np.array_equal(inverted[..., :3], 255 - image[..., :3])


def test_animated_parameters_vary_across_frames() -> None:
    runtime = make(fx.Brightness(amount=ramp(0.0, 0.5)))
    first, middle = run(runtime, START), run(runtime, START + 15)
    assert np.array_equal(first, synthetic())
    assert rgb_mean(middle) > rgb_mean(first)
    driven = make(fx.Contrast(amount=Driven(curve=Sine(freq_hz=1.0, amp=0.5), offset=1.0)))
    assert not np.array_equal(run(driven, START + 7), run(driven, START + 22))


@pytest.mark.parametrize("effect", [
    fx.FilmGrain(intensity=20.0),
    fx.Glitch(intensity=1.0, block_size=8),
    fx.Shake(intensity=8.0),
    fx.VHS(noise=0.2),
    fx.Datamosh(intensity=1.0),
], ids=lambda effect: effect.kind)
def test_seeded_effects_depend_on_seed_and_frame_only(effect: EffectBase) -> None:
    one, other = make(effect, seed=7), make(effect, seed=8)
    forward = [run(one, index) for index in (START + 3, START + 20)]
    backward = [run(one, index) for index in (START + 20, START + 3)][::-1]
    assert all(np.array_equal(a, b) for a, b in zip(forward, backward, strict=True))
    assert np.array_equal(run(make(effect, seed=7), START + 20), forward[1])
    assert not np.array_equal(run(other, START + 20), forward[1])


def test_explicit_seed_overrides_the_clip_seed() -> None:
    effect = fx.FilmGrain(intensity=20.0, seed=99)
    assert np.array_equal(run(make(effect, seed=1), START + 2), run(make(effect, seed=2), START + 2))


@pytest.mark.parametrize("effect", [
    fx.Crop(x=8, y=4, w=48, h=32),
    fx.Zoom(scale=2.0, cx=0.25, cy=0.75),
    fx.Pan(dx=10, dy=-5),
    fx.Mirror(axis="both"),
    fx.Shake(intensity=6.0),
    fx.RGBSplit(offset_x=4, offset_y=2),
    fx.Blur(radius=2.0),
], ids=lambda effect: effect.kind)
def test_geometric_effects_keep_the_size(effect: EffectBase) -> None:
    image = synthetic()
    out = run(make(effect), START + 5, image)
    assert out.shape == image.shape
    assert changed(out, image) > 0


def test_crop_and_zoom_magnify() -> None:
    cropped = run(make(fx.Crop(x=0, y=0, w=WIDTH // 2, h=HEIGHT // 2)))
    assert int(cropped[HEIGHT - 1, WIDTH - 1, 0]) < 140
    zoomed = run(make(fx.Zoom(scale=2.0, cx=0.0, cy=0.0)))
    assert np.allclose(zoomed.astype(int), cropped.astype(int), atol=2)
    with pytest.raises(NodeFailure) as failure:
        make(fx.Crop(x=50, y=0, w=60, h=10))
    assert failure.value.code == "args_invalid"
    with pytest.raises(NodeFailure):
        make(fx.Zoom(scale=0.5))


def test_pan_mirror_and_split() -> None:
    image = synthetic()
    panned = run(make(fx.Pan(dx=10, dy=0)))
    assert np.array_equal(panned[:, :WIDTH - 10], image[:, 10:])
    assert np.all(panned[:, WIDTH - 10:, 3] == 0)
    mirrored = run(make(fx.Mirror(axis="horizontal")))
    assert np.array_equal(mirrored, image[:, ::-1])
    split = run(make(fx.RGBSplit(offset_x=3)))
    assert np.array_equal(split[..., 1], image[..., 1])
    assert np.array_equal(split[:, 3:, 2], image[:, :-3, 2])


def test_animated_crop_varies() -> None:
    runtime = make(fx.Crop(x=int_ramp(0, 40), y=0, w=48, h=32))
    assert not np.array_equal(run(runtime, START), run(runtime, START + 29))


def test_chroma_key_removes_the_key_color() -> None:
    image = synthetic()
    image[30:40, 40:60, :3] = (0, 255, 0)
    out = run(make(fx.ChromaKey(color="#00ff00", similarity=0.1)), START, image)
    assert np.all(out[30:40, 40:60, 3] == 0)
    assert np.all(out[10:20, 10:30, 3] == 255)
    soft = run(make(fx.ChromaKey(color="#00ff00", similarity=0.0, blend=1.0)), START, image)
    assert 0 < int(soft[10:20, 10:30, 3].min()) < 255


def test_fade_and_flash_follow_the_clip_time() -> None:
    fade_in = make(fx.Fade(direction="in", duration=parse_time("0.5s")))
    assert np.all(run(fade_in, START)[..., 3] == 0)
    assert np.array_equal(run(fade_in, START + 20), synthetic())
    fade_out = make(fx.Fade(direction="out", duration=parse_time("0.5s")))
    assert np.array_equal(run(fade_out, START), synthetic())
    assert int(run(fade_out, START + COUNT - 1)[..., 3].max()) < 40
    flash = make(fx.Flash(at=TimeSet(times=(parse_time("0.2s"), parse_time("0.6s"))), duration=parse_time("0.1s"),
                          color="#ffffff"))
    assert np.array_equal(run(flash, START), synthetic())
    assert np.all(run(flash, START + 6)[..., :3] == 255)
    assert np.all(run(flash, START + 18)[..., :3] == 255)
    curved = make(fx.Flash(at=parse_time("0s"), duration=parse_time("0.5s"), color="#ffffff",
                           curve=Sine(freq_hz=1.0, amp=1.0)))
    assert rgb_mean(run(curved, START + 7)) > rgb_mean(run(curved, START + 1))


def write_cube(path: Path, size: int) -> Path:
    lines = ["TITLE \"identity\"", f"LUT_3D_SIZE {size}"]
    steps = [index / (size - 1) for index in range(size)]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for b in steps for g in steps for r in steps]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_identity_lut_is_identity(tmp_path: Path) -> None:
    files = {"lut": write_cube(tmp_path / "identity.cube", 17)}
    image = synthetic()
    out = run(make(fx.LUT(path=ref("lut")), files=files), START, image)
    assert int(np.abs(out.astype(int) - image.astype(int)).max()) <= 1
    graded = run(make(fx.ColorGrade(lut=ref("lut")), files=files), START, image)
    assert int(np.abs(graded.astype(int) - image.astype(int)).max()) <= 1


def test_lut_inverts_and_rejects_bad_files(tmp_path: Path) -> None:
    rows = [f"{1 - r} {1 - g} {1 - b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)]
    inverse = tmp_path / "invert.cube"
    inverse.write_text("LUT_3D_SIZE 2\n" + "\n".join(rows) + "\n", encoding="utf-8")
    out = run(make(fx.LUT(path=ref("lut")), files={"lut": inverse}))
    assert int(np.abs(out[..., :3].astype(int) - (255 - synthetic()[..., :3].astype(int))).max()) <= 1
    broken = tmp_path / "broken.cube"
    broken.write_text("LUT_1D_SIZE 4\n0 0 0\n", encoding="utf-8")
    with pytest.raises(NodeFailure) as failure:
        make(fx.LUT(path=ref("lut")), files={"lut": broken})
    assert "1D" in failure.value.message


def test_posterize_quantizes_and_rejects_one_level() -> None:
    out = run(make(fx.Posterize(levels=2)))
    assert set(np.unique(out[..., :3]).tolist()) <= {0, 255}
    with pytest.raises(NodeFailure):
        make(fx.Posterize(levels=1))


def write_png(path: Path, color: tuple[int, int, int, int], size: tuple[int, int] = (10, 10)) -> Path:
    PILImage.new("RGBA", size, color).save(path)
    return path


def test_watermark_and_sticker_composite_images(tmp_path: Path) -> None:
    files = {"logo": write_png(tmp_path / "logo.png", (255, 0, 0, 255))}
    marked = run(make(fx.Watermark(path=ref("logo"), x=40, y=20, opacity=1.0), files=files))
    assert tuple(marked[25, 45].tolist()) == (255, 0, 0, 255)
    faint = run(make(fx.Watermark(path=ref("logo"), x=40, y=20, opacity=0.5), files=files))
    assert 0 < int(faint[25, 45, 1]) < int(synthetic()[25, 45, 1])
    sticker = run(make(fx.StickerOverlay(path=ref("logo"), x=60, y=5, scale=2.0, rotation=45.0), files=files))
    assert tuple(sticker[15, 70].tolist()) == (255, 0, 0, 255)
    assert changed(sticker, synthetic()) > 200
    moving = make(fx.StickerOverlay(path=ref("logo"), x=int_ramp(0, 60), y=5), files=files)
    assert not np.array_equal(run(moving, START), run(moving, START + 20))


def black() -> Image:
    image = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    image[..., 3] = 255
    return image


def test_text_overlay_draws_glyphs_in_its_window() -> None:
    effect = fx.TextOverlay(text="Hi", size=24, x=10, y=10, start=parse_time("0.5s"), duration=parse_time("0.2s"))
    runtime = make(effect)
    assert np.array_equal(run(runtime, START, black()), black())
    drawn = run(runtime, START + 16, black())
    assert int(np.count_nonzero(drawn[..., 0] > 128)) > 20
    assert np.all(drawn[..., 3] == 255)
    assert np.array_equal(run(runtime, START + 25, black()), black())


def test_lower_third_draws_panel_and_text() -> None:
    effect = fx.LowerThird(title="Title", subtitle="Sub", title_size=16, subtitle_size=10, margin=8,
                           font="IBM Plex Mono")
    out = run(make(effect), START, black())
    assert np.array_equal(out[:HEIGHT * 2 // 3 - 1], black()[:HEIGHT * 2 // 3 - 1])
    assert int(np.count_nonzero(out[HEIGHT * 2 // 3:, :, 0] > 128)) > 20


def test_fonts_are_strict() -> None:
    with pytest.raises(NodeFailure) as failure:
        typeface("IBM Plex Sans", 700, no_media)
    assert failure.value.code == "font_unavailable"
    assert typeface("IBM Plex Sans", 600, no_media) is not None
    with pytest.raises(NodeFailure) as missing:
        make(fx.TextOverlay(text="x", font=ref("font")), files={"font": Path("/nonexistent/font.ttf")})
    assert missing.value.code == "font_unavailable"


def words() -> dict[str, list[Word]]:
    starts = [0.1, 0.3, 0.5, 0.7]
    texts = ["one", "two", "three", "four"]
    return {"stt": [Word(text, round((START / 30 + at) * TICKS_PER_SECOND)) for text, at in zip(texts, starts,
                                                                                                strict=True)]}


@pytest.mark.parametrize("style", ["tiktok", "subtitle", "kinetic"])
def test_captions_show_words_while_spoken(style: Literal["tiktok", "subtitle", "kinetic"]) -> None:
    runtime = make(fx.Captions(source="stt", size=14, margin=4, max_words_per_card=2, style=style), words=words())
    assert np.array_equal(run(runtime, START, black()), black())
    first = run(runtime, START + 6, black())
    second = run(runtime, START + 18, black())
    assert int(np.count_nonzero(first[..., :3])) > 20
    assert not np.array_equal(first, second)


def test_tiktok_highlights_the_active_word() -> None:
    runtime = make(fx.Captions(source="stt", size=14, margin=4, word_animation="none"), words=words())
    out = run(runtime, START + 4, black())
    yellow = (out[..., 0] > 200) & (out[..., 1] > 180) & (out[..., 2] < 80)
    assert int(np.count_nonzero(yellow)) > 5


def test_captions_need_their_word_source() -> None:
    with pytest.raises(NodeFailure) as failure:
        make(fx.Captions(source="other"), words=words())
    assert failure.value.code == "time_unresolved"


@pytest.mark.parametrize(("effect", "package"), [
    (fx.MatAnyoneRemove(), "matanyone"),
    (fx.BiRefNetRemove(), "rembg"),
    (fx.SAM3Track(prompt="person"), "sam3"),
    (fx.AutoReframe(), "mediapipe"),
    (fx.AutoReframe(tracker="salience"), "cv2"),
    (fx.PunchIn(), "mediapipe"),
], ids=["matanyone_remove", "birefnet_remove", "sam3_track", "auto_reframe_face", "auto_reframe_salience",
        "punch_in"])
def test_ml_effects_need_their_packages(effect: EffectBase, package: str) -> None:
    if importlib.util.find_spec(package) is not None:
        pytest.skip(f"{package} is installed here")
    with pytest.raises(NodeFailure) as opened:
        make(effect)
    assert opened.value.code == "resolution_unavailable"
    assert "eks-harness[ml]" in opened.value.message
    with pytest.raises(NodeFailure) as recorded:
        inference_record(effect)
    assert recorded.value.code == "resolution_unavailable"


def test_inference_record_is_none_for_plain_effects() -> None:
    assert inference_record(fx.Brightness(amount=0.1)) is None


def test_punch_in_without_speaker_tracking_runs_the_legacy_processor() -> None:
    effect = fx.PunchIn(track_speaker=False, zoom=1.5)
    record = inference_record(effect)
    assert record is not None and set(record) == {"device", "model", "weights", "backend_version"}
    opaque = synthetic()
    opaque[..., 3] = 255
    runtime = make(effect)
    out = run(runtime, START + 3, opaque)
    assert out.shape == opaque.shape
    assert changed(out, opaque) > 0
    with pytest.raises(NodeFailure):
        run(runtime, START + 3)


def test_timing_effects_are_refused() -> None:
    assert {"speed_ramp", "freeze"} == TIMING_EFFECTS
    with pytest.raises(NodeFailure):
        make(fx.SpeedRamp(factor=2.0))
    with pytest.raises(NodeFailure):
        make(fx.Freeze(at=parse_time("0s"), hold=parse_time("1s")))


class Tinted(EffectBase, frozen=True):
    kind: Literal["test_tinted"] = "test_tinted"

    def apply(self, image: Image, frame: EffectFrame, ctx: EffectContext) -> Image:
        image[..., 2] = 255 if ctx.position(frame.index) % 2 else 0
        return image


class Inert(EffectBase, frozen=True):
    kind: Literal["test_inert"] = "test_inert"


def test_plugin_effects_use_their_apply_method() -> None:
    out = run(make(Tinted()), START + 1)
    assert np.all(out[..., 2] == 255)
    with pytest.raises(NodeFailure) as failure:
        make(Inert())
    assert failure.value.code == "executor_error"
    assert "apply(image, frame, ctx)" in failure.value.message


def test_frames_outside_the_clip_and_wrong_images_fail() -> None:
    runtime = make(fx.Invert())
    with pytest.raises(NodeFailure):
        run(runtime, START + COUNT)
    with pytest.raises(NodeFailure):
        runtime.apply(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8), frame(START))
