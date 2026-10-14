from __future__ import annotations

import hashlib
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from pydantic import BaseModel

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.values import _resolved_args_model  # pyright: ignore[reportPrivateUsage]
from eks_harness.ir.catalog.transitions import TransitionBase
from eks_harness.ir.ops.edit import Timeline
from eks_harness.ir.registry import default_registry
from eks_harness.ir.time import TimeContext
from eks_harness.ir.types import MediaInfo
from eks_harness.media.edit.compose import RenderJob, encoder_args, render
from eks_harness.media.edit.timing import plan_clips
from eks_harness.media.edit.transitions import TransitionFrame, blend
from eks_harness.media.files import probe

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
FPS = Fraction(30)


def source(tmp: Path, name: str, lavfi: str, seconds: float = 3.0) -> MediaInfo:
    path = tmp / name
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", lavfi, "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", str(path)], check=True)
    return probe(path, f"file:{name}")


def timeline(data: dict[str, Any]) -> Timeline:
    return cast(Timeline, _resolved_args_model(Timeline).model_validate(data))


def media(info: MediaInfo) -> dict[str, Any]:
    return info.model_dump(mode="json", exclude_none=True)


def no_effects(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("no effects expected")


def run(tmp: Path, line: Timeline, name: str) -> Path:
    out = tmp / name
    paths = {path.name: path for path in tmp.iterdir()}

    def resolve(info: MediaInfo) -> Path:
        return paths[info.uri.removeprefix("file:")]

    render(RenderJob(timeline=line, context=TimeContext.static(fps=FPS), media=resolve,
                     media_value=lambda value: resolve(MediaInfo.model_validate(value)), out=out, scratch=tmp,
                     encoder="libx264"), no_effects)
    return out


def frames(path: Path, size: tuple[int, int]) -> np.ndarray:
    done = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt",
                           "rgb24", "pipe:1"], capture_output=True, check=True)
    return np.frombuffer(done.stdout, dtype=np.uint8).reshape(-1, size[1], size[0], 3)


def test_timing_follows_speed_freeze_and_still_images(tmp_path: Path) -> None:
    video = source(tmp_path, "v.mp4", "testsrc2=size=320x240:rate=30", 4.0)
    still = source(tmp_path, "s.png", "color=red:size=64x64", 0.04)
    line = timeline({"fps": 30, "size": [320, 240], "tracks": [
        {"name": "a", "clips": [
            {"id": "a/0", "media": media(video), "src": {"start": "1s", "end": "3s"}, "speed": 2.0},
            {"id": "a/1", "media": media(video), "start": "2s", "src": {"start": "0s", "end": "1s"},
             "effects": [{"kind": "freeze", "at": "500ms", "hold": "1s"}]},
            {"id": "a/2", "media": media(still), "start": "5s"},
        ]}], "duration": "6s"})
    timings, total = plan_clips(line, TimeContext.static(fps=FPS),
                                {clip.id: MediaInfo.model_validate(clip.media.model_dump())
                                 for clip in line.tracks[0].clips}, None)
    fast, frozen, image = timings
    assert total == 180
    assert (fast.first, fast.count) == (0, 30) and fast.source[0] == 1.0 and fast.source[-1] == pytest.approx(2.9333, 1e-3)
    assert frozen.first == 60 and frozen.count == 60
    assert np.all(frozen.source[15:45] == frozen.source[15])
    assert (image.first, image.count) == (150, 30)


def test_the_compositor_is_byte_identical_and_draws_transforms(tmp_path: Path) -> None:
    red = source(tmp_path, "red.mp4", "color=red:size=320x240:rate=30")
    blue = source(tmp_path, "blue.mp4", "color=blue:size=320x240:rate=30")
    line = timeline({"fps": 30, "size": [320, 240], "seed": 5, "tracks": [
        {"name": "main", "clips": [
            {"id": "main/0", "media": media(red), "src": {"start": "0s", "end": "1s"}},
            {"id": "main/1", "media": media(blue), "start": "1s", "src": {"start": "0s", "end": "1s"},
             "transition_in": {"kind": "crossfade", "duration": "500ms"}}]},
        {"name": "pip", "z": 1, "clips": [
            {"id": "pip/0", "media": media(red), "start": "1500ms", "src": {"start": "0s", "end": "500ms"},
             "fit": "contain", "scale": 0.25, "x": 100.0, "y": -80.0}]}]})
    first = run(tmp_path, line, "one.mp4")
    second = run(tmp_path, line, "two.mp4")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    pictures = frames(first, (320, 240)).astype(int)
    assert pictures.shape[0] == 60
    assert pictures[10, 120, 160, 0] > 200 and pictures[10, 120, 160, 2] < 60
    middle = pictures[37, 120, 160]
    assert 60 < middle[0] < 200 and 60 < middle[2] < 200
    assert pictures[55, 120, 160, 2] > 200
    assert pictures[50, 40, 260, 0] > 200 and pictures[50, 200, 40, 2] > 200


def test_two_transitions_on_one_boundary_are_refused(tmp_path: Path) -> None:
    red = source(tmp_path, "red.mp4", "color=red:size=64x64:rate=30", 1.0)
    line = timeline({"fps": 30, "size": [64, 64], "tracks": [{"name": "m", "clips": [
        {"id": "m/0", "media": media(red), "src": {"start": "0s", "end": "1s"},
         "transition_out": {"kind": "crossfade", "duration": "4f"}},
        {"id": "m/1", "media": media(red), "start": "1s", "src": {"start": "0s", "end": "1s"},
         "transition_in": {"kind": "wipe", "duration": "4f"}}]}]})
    with pytest.raises(NodeFailure, match="both set a transition"):
        run(tmp_path, line, "x.mp4")


@pytest.mark.parametrize("kind", sorted(spec.kind for spec in default_registry().catalogs("transition")))
def test_every_transition_starts_on_a_and_ends_on_b(kind: str) -> None:
    spec = default_registry().catalog("transition", kind)
    fields: dict[str, Any] = {"duration": "10f"} if "duration" in spec.model.model_fields else {}
    model = cast(TransitionBase, cast(type[BaseModel], spec.model).model_validate(fields))
    a = np.zeros((24, 32, 4))
    a[..., 0], a[..., 3] = 1.0, 1.0
    b = np.zeros((24, 32, 4))
    b[..., 2], b[..., 3] = 1.0, 1.0
    start = blend(model, a, b, TransitionFrame(progress=0.0, index=0, seconds=0.0, seed=1))
    end = blend(model, a, b, TransitionFrame(progress=1.0, index=9, seconds=0.3, seed=1))
    assert start.shape == a.shape and end.shape == a.shape
    assert np.isfinite(start).all() and np.isfinite(end).all()
    assert float(np.abs(start - a).mean()) < 0.35
    assert float(np.abs(end - b).mean()) < 0.35
    again = blend(model, a, b, TransitionFrame(progress=0.5, index=4, seconds=0.15, seed=1))
    assert np.array_equal(again, blend(model, a, b, TransitionFrame(progress=0.5, index=4, seconds=0.15, seed=1)))


def test_encoder_arguments_apply_every_render_setting() -> None:
    from eks_harness.ir.ops.edit import RenderSettings

    args = encoder_args(RenderSettings(threads=3, quality="draft", crf=21, colorspace="bt601"), "libx264")
    joined = " ".join(args)
    assert "-threads 3" in joined and "threads=3" in joined and "-crf 21" in joined and "-preset veryfast" in joined
    assert "-colorspace bt601" in joined and "-pix_fmt yuv420p" in joined
    assert "-b:v 900k" in " ".join(encoder_args(RenderSettings(bitrate_kbps=900), "libsvtav1"))
