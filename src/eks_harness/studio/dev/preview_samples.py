"""ffmpeg-generated sample clips backing the effect/transition preview cache.

Sample assets live under ``<cache dir>/studio/preview_cache/_samples/``. Each
helper is idempotent: if the target file already exists on disk we return
the cached path without re-invoking ffmpeg.

The previous incarnation used a static ``smptebars`` source. The colour
bars are pretty, but they are also perfectly still - which means motion
effects like Shake, Glitch and RGBSplit look identical on hover regardless
of intensity. The current recipe builds a 320x180 @ 30fps clip with two
animated coloured boxes orbiting on saturated paths plus a slow hue
rotation. Visible motion makes Shake jitter, Glitch pop, and RGBSplit
fringe in obvious ways. The transition pair uses different colours,
shapes and speeds so a Crossfade is visibly a blend of two distinct clips.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .file_routes import samples_root

__all__ = [
    "portrait_sample_path",
    "sample_audio_path",
    "sample_video_pair",
    "sample_video_path",
    "samples_dir",
]


# Bundled with the wheel under `preview_assets/portrait.mp4`. Effects that
# need a face to do anything visible (AutoReframe, PunchIn) read this
# instead of the abstract colour-box sample.
_BUNDLED_PORTRAIT = Path(__file__).parent / "preview_assets" / "portrait.mp4"


_SAMPLE_DURATION_SECONDS = 2
_SAMPLE_SIZE = "320x180"
_SAMPLE_FPS = 30


def samples_dir() -> Path:
    """Return the on-disk directory that owns the sample assets."""

    target = samples_root()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_ffmpeg() -> str:
    """Locate the ``ffmpeg`` binary or raise with a clear message."""

    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg not found on PATH")
    return binary


# Recipe A: red box orbiting on a slow ellipse, cyan box on a faster
# counter-orbit, slow hue rotation on top. Resolution / framerate /
# duration are baked into the lavfi chain to keep callers small.
_LAVFI_PRIMARY = (
    f"color=c=black:s={_SAMPLE_SIZE}:r={_SAMPLE_FPS}:d={_SAMPLE_DURATION_SECONDS},"
    "drawbox=x='40+80*sin(2*PI*t/2)':y='40+50*cos(2*PI*t/2)':w=80:h=80"
    ":color=red@1.0:t=fill,"
    "drawbox=x='160+60*cos(2*PI*t/1.3)':y='90+40*sin(2*PI*t/1.3)':w=60:h=60"
    ":color=cyan@1.0:t=fill,"
    "hue=h='90*sin(2*PI*t/2)':s=1.4,"
    "format=yuv420p"
)

# Recipe B: lime + magenta squares on different orbits, faster rotation,
# negative-direction hue sweep so a Crossfade midpoint visibly blends two
# distinct colour palettes.
_LAVFI_SECONDARY = (
    f"color=c=black:s={_SAMPLE_SIZE}:r={_SAMPLE_FPS}:d={_SAMPLE_DURATION_SECONDS},"
    "drawbox=x='30+70*cos(2*PI*t/1.6)':y='30+45*sin(2*PI*t/1.6)':w=70:h=70"
    ":color=lime@1.0:t=fill,"
    "drawbox=x='180+50*sin(2*PI*t/0.9)':y='80+35*cos(2*PI*t/0.9)':w=55:h=55"
    ":color=magenta@1.0:t=fill,"
    "hue=h='-120*cos(2*PI*t/2)':s=1.5,"
    "format=yuv420p"
)


def _generate_video(out_path: Path, *, lavfi: str) -> None:
    """Synthesize ``lavfi`` into ``out_path`` via ffmpeg.

    Always libx264 + yuv420p so the output plays in every browser, with
    ``+faststart`` so the moov atom sits at the head of the file.
    """

    binary = _resolve_ffmpeg()
    args = [
        binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(out_path),
    ]
    subprocess.run(args, check=True, capture_output=True)


def _generate_audio(out_path: Path) -> None:
    binary = _resolve_ffmpeg()
    lavfi = f"sine=frequency=440:duration={_SAMPLE_DURATION_SECONDS}"
    args = [
        binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        str(out_path),
    ]
    subprocess.run(args, check=True, capture_output=True)


def sample_video_path() -> Path:
    """Lazily produce the primary animated colour clip, return its path."""

    target = samples_dir() / "sample.mp4"
    if target.is_file() and target.stat().st_size > 0:
        return target
    _generate_video(target, lavfi=_LAVFI_PRIMARY)
    return target


def sample_video_pair() -> tuple[Path, Path]:
    """Return ``(sample.mp4, sample_b.mp4)`` for transition previews."""

    primary = sample_video_path()
    secondary = samples_dir() / "sample_b.mp4"
    if not (secondary.is_file() and secondary.stat().st_size > 0):
        _generate_video(secondary, lavfi=_LAVFI_SECONDARY)
    return primary, secondary


def portrait_sample_path() -> Path:
    """Return the bundled portrait clip used by face-tracking previews.

    The asset is shipped inside the wheel (``preview_assets/portrait.mp4``)
    and depicts a centred face that mediapipe / cv2 trackers can lock onto
    while gently oscillating horizontally so AutoReframe has motion to
    react to. No generation step required.
    """

    return _BUNDLED_PORTRAIT


def sample_audio_path() -> Path:
    """Lazily produce a 440Hz sine ``sample.wav``, return its path."""

    target = samples_dir() / "sample.wav"
    if target.is_file() and target.stat().st_size > 0:
        return target
    _generate_audio(target)
    return target
