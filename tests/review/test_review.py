from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from eks_harness.review import (
    CheckResult,
    ExpectationError,
    load_markers,
    make_sheets,
    parse_at,
    parse_expectations,
    parse_markers,
    probe,
    register,
    run_checks,
)
from eks_harness.review import media, ocr
from eks_harness.review.checks.audio import detect_onsets
from eks_harness.review.checks.frames import match_template, pixel_box
from eks_harness.review.sheet import auto_frame_count, layout_for, plan_tiles, split

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

pytestmark = pytest.mark.skipif(not synth.have_ffmpeg(), reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return synth.build(tmp_path_factory.mktemp("review") / "synth.mp4")


@pytest.fixture(scope="module")
def silent_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return synth.build(tmp_path_factory.mktemp("review-silent") / "silent.mp4", audio=False)


@pytest.fixture(scope="module")
def ramp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("ramp") / "ramp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=64x64:r=30:d=4,geq=lum='16+mod(N*2\\,200)':cb=128:cr=128", "-c:v", "libx264",
                    "-crf", "0", "-pix_fmt", "yuv420p", "-g", "30", str(out)], check=True)
    return out


def _level(image) -> float:
    return (float(np.asarray(image.convert("L"), dtype=np.float32).mean()) * 219 / 255) / 2


def test_probe_reads_timing(clip: Path) -> None:
    info = probe(clip)
    assert (info.width, info.height, info.frames) == (1280, 720, 240)
    assert info.fps == pytest.approx(30.0)
    assert info.duration == pytest.approx(8.0, abs=0.05)
    assert info.has_audio
    assert info.frame_at(2.0) == 60 and info.frame_at(2.0 - 1e-3) == 59
    assert info.time_of(75) == pytest.approx(2.5)


def test_frame_decoding_is_frame_exact(ramp: Path) -> None:
    info = probe(ramp)
    for frame in (0, 7, 29, 30, 31, 77, 119):
        assert _level(media.frame_image(info, frame)) == pytest.approx(frame % 100, abs=0.6)
    picked = media.select_frames(info, [3, 50, 99], 64, 64)
    assert {k: round(_level(v)) for k, v in picked.items()} == {3: 3, 50: 50, 99: 99}
    gray = media.gray_frames(info, 32)
    assert len(gray) == 120
    assert float(gray[60].mean()) * 219 / 255 / 2 == pytest.approx(60, abs=0.6)


def test_markers_accept_beatsheet_shapes(tmp_path: Path) -> None:
    data = {"beats": [0.5, 1.0], "downbeats": [{"t": 2.0}], "cues": [{"t": 3.0, "label": "stamp"}],
            "markers": [{"t": 4.0, "kind": "word", "label": "kontor"}, {"t": 5.0, "kind": "cue", "name": "brand"}],
            "frames": [6.0]}
    markers = parse_markers(data)
    assert [(m.t, m.kind) for m in markers.ticks] == [(0.5, "beat"), (1.0, "beat"), (2.0, "downbeat"),
                                                       (3.0, "cue"), (4.0, "word"), (5.0, "cue")]
    assert [(m.t, m.label) for m in markers.frames] == [(3.0, "stamp"), (5.0, "brand"), (6.0, "")]
    file = tmp_path / "m.json"
    file.write_text(json.dumps([1.5, {"t": 2.5, "kind": "frame", "label": "x"}]))
    flat = load_markers(file)
    assert [m.t for m in flat.ticks] == [1.5, 2.5] and [m.t for m in flat.frames] == [2.5]
    assert [(m.t, m.label) for m in parse_at(["1.25:intro", "3"])] == [(1.25, "intro"), (3.0, "")]
    with pytest.raises(ValueError):
        parse_markers([{"kind": "cue"}])


def test_frame_count_and_layout_scale() -> None:
    assert auto_frame_count(20) == 40
    assert auto_frame_count(3) == 12
    assert auto_frame_count(600) == 96
    for width, height in ((1920, 1080), (1080, 1920), (1080, 1080)):
        layout = layout_for(width, height)
        assert layout.cols * (layout.tile_w + 6) + 6 <= 2000
        assert layout.capacity >= 40


def test_tiles_are_even_and_cues_merge(clip: Path) -> None:
    info = probe(clip)
    markers = parse_markers({"cues": [{"t": 5.5, "label": "stamp"}, {"t": 0.0, "label": "start"}]})
    tiles = plan_tiles(info, 16, markers.frames)
    frames = [t.frame for t in tiles]
    assert frames[0] == 0 and frames[-1] == 239 and frames == sorted(frames)
    stamp = next(t for t in tiles if t.frame == 165)
    assert stamp.cue == "stamp" and not stamp.regular
    assert next(t for t in tiles if t.frame == 0).cue == "start"
    assert len(tiles) == 17
    assert [len(g) for g in split(list(range(100)), 48)] == [34, 34, 32]


def test_sheet_image_stays_readable(clip: Path, tmp_path: Path) -> None:
    from PIL import Image

    markers = parse_markers({"beats": synth.BEATS, "downbeats": synth.BEATS[::4],
                             "cues": [{"t": synth.STAMP_AT, "label": "stamp"}]})
    result = make_sheets(clip, tmp_path, markers=markers)
    assert len(result.sheets) == 1
    sheet = result.sheets[0]
    with Image.open(sheet.path) as image:
        assert max(image.size) <= 2000
        width, height = image.size
        strip = np.asarray(image.crop((6, height - 168, width - 6, height - 6)).convert("RGB"), dtype=np.int32)
    assert any(t.cue == "stamp" for t in sheet.tiles)
    assert (np.abs(strip - np.array([255, 176, 32])).sum(axis=2) < 60).any()
    many = make_sheets(clip, tmp_path / "many", frames=200)
    assert len(many.sheets) > 1
    assert many.sheets[0].start == 0 and many.sheets[-1].end == pytest.approx(8.0, abs=0.05)
    assert many.sheets[0].end == many.sheets[1].start
    for part in many.sheets:
        with Image.open(part.path) as image:
            assert max(image.size) <= 2000


def test_sheet_without_audio(silent_clip: Path, tmp_path: Path) -> None:
    result = make_sheets(silent_clip, tmp_path, frames=12)
    assert result.sheets[0].path.is_file() and not result.video.has_audio


def _report(clip: Path, checks: list[dict], **extra) -> dict:
    report = run_checks(clip, {"tolerance_frames": 2, "checks": checks, **extra}, plugins=False)
    return {r.index: r for r in report.results} | {"report": report}


def test_cuts_are_found_on_the_exact_frame(clip: Path) -> None:
    results = _report(clip, [{"t": t, "kind": "cut"} for t in synth.CUTS] + [{"t": 3.0, "kind": "cut"}])
    for index, t in enumerate(synth.CUTS):
        assert results[index].status == "pass" and results[index].delta_frames == 0, results[index]
    missing = results[len(synth.CUTS)]
    assert missing.status == "fail" and "nearest cut" in missing.detail
    assert results["report"].detected["cuts"] == synth.CUTS


def test_cut_tolerance_in_frames(clip: Path) -> None:
    results = _report(clip, [{"t": 2.1, "kind": "cut", "tolerance_frames": 2},
                             {"t": 2.1, "kind": "cut", "tolerance_frames": 3}])
    assert results[0].status == "fail" and results[0].delta_frames == -3
    assert results[1].status == "pass"


def test_motion_start_and_stop(clip: Path) -> None:
    results = _report(clip, [{"t": 2.0, "kind": "motion", "params": {"mode": "starts"}},
                             {"t": 4.0, "kind": "motion", "params": {"mode": "stops"}},
                             {"t": 7.0, "kind": "motion", "params": {"mode": "starts"}}])
    assert results[0].status == "pass" and results[0].delta_frames == 0
    assert results[1].status == "pass" and abs(results[1].delta_frames) <= 1
    assert results[2].status == "fail"


def test_visual_colour_region(clip: Path) -> None:
    box = synth.STAMP_BOX
    results = _report(clip, [
        {"t": synth.STAMP_AT, "kind": "visual", "params": {"color": "#d32f2f", "box": box}},
        {"t": 5.0, "kind": "visual", "params": {"color": "#d32f2f", "box": box, "mode": "absent"}},
        {"t": 6.0, "kind": "visual", "params": {"color": "#d32f2f", "box": box, "mode": "present", "until": 6.4}},
        {"t": 5.2, "kind": "visual", "params": {"color": "#d32f2f", "box": box}, "tolerance_frames": 3},
    ])
    assert results[0].status == "pass" and results[0].delta_frames == 0
    assert results[1].status == "pass" and results[2].status == "pass"
    assert results[3].status == "fail" and results[3].delta_frames == 9


def test_visual_template(clip: Path, tmp_path: Path) -> None:
    info = probe(clip)
    frame = media.frame_image(info, info.frame_at(6.0))
    frame.crop((860, 50, 980, 150)).save(tmp_path / "stamp.png")
    frame.crop((100, 100, 160, 160)).save(tmp_path / "flat.png")
    template = str(tmp_path / "stamp.png")
    results = _report(clip, [
        {"t": 6.0, "kind": "visual", "params": {"template": template, "box": [800, 0, 400, 260], "mode": "present"}},
        {"t": 5.0, "kind": "visual", "params": {"template": template, "mode": "present"}},
        {"t": synth.STAMP_AT, "kind": "visual", "params": {"template": "stamp.png", "box": [800, 0, 400, 260]}},
        {"t": 6.0, "kind": "visual", "params": {"template": str(tmp_path / "flat.png"), "mode": "present"}},
    ], )
    assert results[0].status == "pass"
    assert results[1].status == "fail"
    assert results[2].status == "error" and "does not exist" in results[2].detail
    assert results[3].status == "error" and "flat colour" in results[3].detail
    relative = run_checks(clip, parse_expectations([{"t": synth.STAMP_AT, "kind": "visual",
                                                     "params": {"template": "stamp.png", "box": [800, 0, 400, 260]}}],
                                                   base=tmp_path), plugins=False)
    assert relative.results[0].status == "pass" and relative.results[0].delta_frames == 0


def test_match_template_scores() -> None:
    rng = np.random.default_rng(1)
    image = rng.integers(0, 255, (60, 80)).astype(np.float32)
    assert match_template(image, image[10:30, 20:45]) == pytest.approx(1.0, abs=1e-6)
    assert match_template(image, rng.integers(0, 255, (20, 25)).astype(np.float32)) < 0.6
    assert match_template(image[:10, :10], image) == 0.0


def test_pixel_box_fractions_and_pixels() -> None:
    assert pixel_box([0.5, 0.5, 0.25, 0.25], 200, 100, (1280, 720)) == (100, 50, 150, 75)
    assert pixel_box([640, 360, 320, 180], 128, 72, (1280, 720)) == (64, 36, 96, 54)
    with pytest.raises(ExpectationError):
        pixel_box([1, 2, 3], 10, 10, (10, 10))


def test_black_and_frozen_spans(clip: Path) -> None:
    results = _report(clip, [{"kind": "black", "params": {"max_frames": 2}},
                             {"kind": "black", "params": {"max_frames": 15}},
                             {"kind": "black", "params": {"max_frames": 2, "allow": [[3.9, 4.6]]}},
                             {"kind": "frozen", "params": {"max_frames": 30}},
                             {"kind": "frozen", "params": {"max_frames": 90}}])
    assert results[0].status == "fail"
    assert results[0].data["spans"] == [{"start": 4.0, "end": pytest.approx(4.4667, abs=1e-3), "frames": 15,
                                         "startFrame": 120}]
    assert results[1].status == "pass" and results[2].status == "pass"
    assert results[3].status == "fail"
    assert [(s["startFrame"], s["frames"]) for s in results[3].data["spans"]] == [(0, 60), (195, 45)]
    assert results[4].status == "pass"


def test_safe_area(clip: Path) -> None:
    results = _report(clip, [{"kind": "safe_area", "params": {"margins": 0.05, "window": [4.6, 6.4]}},
                             {"kind": "safe_area", "params": {"margins": [0.15, 0.05, 0.05, 0.05],
                                                              "window": [5.6, 6.4]}}])
    assert results[0].status == "pass"
    assert results[1].status == "fail" and results[1].data["violations"]


def test_audio_checks(clip: Path) -> None:
    results = _report(clip, [{"kind": "loudness", "params": {"true_peak_max": 0}},
                             {"kind": "loudness", "params": {"lufs": -14, "lufs_tolerance": 1}},
                             {"t": 0.25, "kind": "onset"},
                             {"t": 0.40, "kind": "onset", "tolerance_frames": 2},
                             {"kind": "beats", "params": {"times": synth.BEATS}},
                             {"kind": "beats", "params": {"times": [b + 0.2 for b in synth.BEATS]}}])
    assert results[0].status == "pass"
    assert results[1].status == "fail" and "LUFS" in results[1].detail
    assert results[2].status == "pass" and results[2].delta_frames == 0
    assert results[3].status == "fail"
    assert results[4].status == "pass" and results[4].data["matched"] == len(synth.BEATS)
    assert results[5].status == "fail"


def test_onset_detector_on_clicks() -> None:
    rate = 16000
    samples = np.zeros(rate * 2, dtype=np.float32)
    for t in (0.3, 0.8, 1.55):
        start = int(t * rate)
        samples[start:start + 160] = np.sin(np.arange(160) * 0.6) * 0.7
    found = detect_onsets(samples, rate)
    assert [round(t, 3) for t in found] == [0.3, 0.8, 1.55]


def test_audio_checks_skip_without_audio(silent_clip: Path) -> None:
    results = _report(silent_clip, [{"kind": "loudness", "params": {"lufs": -14}},
                                    {"kind": "beats", "params": {"times": [1.0], "require_audio": True}}])
    assert results[0].status == "skip" and results[1].status == "fail"
    assert results["report"].ok is False


def _ocr_engines() -> list[str]:
    engines = []
    if shutil.which("tesseract"):
        engines.append("tesseract")
    if sys.platform == "darwin" and shutil.which("swiftc"):
        engines.append("vision")
    return engines


@pytest.mark.parametrize("engine", _ocr_engines() or [pytest.param("none", marks=pytest.mark.skip("no OCR engine"))])
def test_text_appears_and_absent(clip: Path, engine: str) -> None:
    results = _report(clip, [
        {"t": synth.TEXT_AT, "kind": "text", "tolerance_frames": 1,
         "params": {"text": synth.TEXT, "box": [0, 0.6, 1, 0.4], "engine": engine}},
        {"t": 7.0, "kind": "text", "params": {"text": "Kontör", "mode": "absent", "engine": engine}},
        {"t": 4.7, "kind": "text", "params": {"text": "Kontör", "mode": "absent", "engine": engine}},
    ])
    assert results[0].status == "pass", results[0]
    assert results[0].delta_frames == 0
    assert results[1].status == "pass" and results[2].status == "pass"


def test_text_normalization() -> None:
    assert ocr.contains("EKSENGOLD KONTOR", "EksenGOLD Kontör")
    assert ocr.contains("İstanbul  Şube", "istanbul sube")
    assert not ocr.contains("Kontrol", "Kontör")


def test_unknown_kind_and_bad_input(clip: Path) -> None:
    report = run_checks(clip, [{"kind": "nope"}], plugins=False)
    assert report.results[0].status == "error" and "no check named" in report.results[0].detail
    with pytest.raises(ExpectationError):
        parse_expectations({"tolerance_frames": 2})
    with pytest.raises(ExpectationError):
        parse_expectations([{"t": 1}])
    errored = run_checks(clip, [{"kind": "cut"}], plugins=False)
    assert errored.results[0].status == "error" and "needs t" in errored.results[0].detail


def test_registered_check_runs(clip: Path) -> None:
    def always(ctx, item):
        return ctx.result(item, measured=item.t + 1 / ctx.fps)

    class Counter:
        kind = "counter"

        def run(self, ctx, item):
            return CheckResult(index=item.index, kind=item.kind, label=item.label, status="pass",
                               detail=f"{ctx.info.frames} frames")

    register("always", always)
    register("counter", Counter())
    report = run_checks(clip, [{"t": 1.0, "kind": "always"}, {"kind": "counter"}], plugins=False)
    assert report.results[0].status == "pass" and report.results[0].delta_frames == 1
    assert report.results[1].detail == "240 frames"
    text = report.text()
    assert text.startswith("PASS synth.mp4") and "+1f/2f" in text
    assert json.loads(json.dumps(report.as_dict()))["ok"] is True


def test_video_check_is_a_contribution_type() -> None:
    from eks_harness.plugins import CONTRIBUTION_TYPES

    assert "video_check" in CONTRIBUTION_TYPES


def test_repo_plugin_contributes_a_check(clip: Path, tmp_path: Path, paths) -> None:
    import textwrap

    from eks_harness.config import Config
    from eks_harness.plugins.host import PluginHost

    tree = tmp_path / "repo"
    plugin = tree / ".harness" / "plugins" / "checks"
    plugin.mkdir(parents=True)
    (plugin / "harness-plugin.toml").write_text(textwrap.dedent("""
        [plugin]
        id = "acme.checks"
        [[contributes.video_check]]
        id = "duration"
        entry = "acme_checks:Duration"
    """), encoding="utf-8")
    (plugin / "acme_checks.py").write_text(textwrap.dedent("""
        class Duration:
            kind = "duration"

            def __init__(self, context=None):
                self.context = context

            def run(self, ctx, item):
                return ctx.result(item, measured=ctx.info.duration)
    """), encoding="utf-8")
    (tree / ".harness" / "project.toml").write_text(textwrap.dedent("""
        [project]
        id = "acme/ads"
        [plugins]
        trust = ["checks"]
    """), encoding="utf-8")
    from eks_harness.review.checks import load_plugin_checks

    host = PluginHost(paths, Config(paths))
    assert load_plugin_checks(host, tree) == ["duration"]
    report = run_checks(clip, [{"t": 8.0, "kind": "duration"}], plugins=False)
    assert report.results[0].status == "pass" and report.results[0].measured == pytest.approx(8.0, abs=0.05)
