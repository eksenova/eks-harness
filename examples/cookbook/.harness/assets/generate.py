from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RATE = 48000
FLAGS = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-map_metadata", "-1"]
VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "1",
         "-x264-params", "threads=1"]
SOURCES = {
    "clip_0": "testsrc2=size=1280x720:rate=30",
    "clip_1": "smptehdbars=size=1280x720:rate=30",
    "clip_2": "mandelbrot=size=1280x720:rate=30:maxiter=64",
    "clip_3": "cellauto=size=1280x720:rate=30:rule=110:seed=7",
    "before": "testsrc=size=1280x720:rate=30",
    "after": "testsrc=size=1280x720:rate=30,hue=h=120:s=2",
    "main_clip": "testsrc2=size=1080x1920:rate=30",
    "reaction_clip": "life=size=640x640:rate=30:seed=11:mold=10:ratio=0.2,scale=640:640",
    "parkour": "rgbtestsrc=size=1280x720:rate=30",
    "broll_0": "testsrc2=size=1280x720:rate=30,hue=H=2*PI*t/6",
    "broll_1": "mandelbrot=size=1280x720:rate=30:maxiter=48:start_scale=2,hue=s=0.4",
}


def ffmpeg(*args: str) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SystemExit("ffmpeg is required to generate the cookbook assets")
    subprocess.run([binary, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def video(name: str, source: str, seconds: float) -> None:
    ffmpeg("-f", "lavfi", "-i", source, "-t", f"{seconds}", *FLAGS, *VIDEO, str(HERE / f"{name}.mp4"))


def write_wav(path: Path, samples: np.ndarray) -> None:
    data = np.clip(samples, -1.0, 1.0)
    pcm = np.round(data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(pcm.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())


def music(seconds: float, bpm: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = round(seconds * RATE)
    time = np.arange(count) / RATE
    out = np.zeros(count)
    beat = 60.0 / bpm
    for index in range(int(seconds / beat) + 1):
        start = round(index * beat * RATE)
        length = min(round(0.12 * RATE), count - start)
        if length <= 0:
            continue
        local = np.arange(length) / RATE
        accent = 1.0 if index % 4 == 0 else 0.8
        out[start:start + length] += accent * np.sin(2 * math.pi * (150 - 90 * local / 0.12) * local) * np.exp(-local / 0.04)
        hat_start = round((index + 0.5) * beat * RATE)
        hat_length = min(round(0.03 * RATE), count - hat_start)
        if hat_length > 0:
            hat_time = np.arange(hat_length) / RATE
            out[hat_start:hat_start + hat_length] += rng.uniform(-1.0, 1.0, hat_length) * 0.3 * np.exp(-hat_time / 0.01)
    pad = sum(np.sin(2 * math.pi * f * time) for f in (220.0, 277.18, 329.63)) * 0.02
    return np.stack([out + pad, out * 0.95 + pad], axis=1)


def voice(seconds: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = round(seconds * RATE)
    time = np.arange(count) / RATE
    syllables = (np.sin(2 * math.pi * 4.0 * time) > 0.2).astype(float)
    pauses = ((time % 3.0) < 2.2).astype(float)
    pitch = 160 + 30 * np.sin(2 * math.pi * 0.7 * time)
    tone = np.sin(2 * math.pi * np.cumsum(pitch) / RATE) + 0.4 * np.sin(4 * math.pi * np.cumsum(pitch) / RATE)
    breath = rng.normal(0.0, 0.02, count)
    mono = (tone * 0.35 + breath) * syllables * pauses
    return np.stack([mono, mono], axis=1)


def image(name: str, width: int, height: int, paint: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> None:
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    rgba = paint(xs / width, ys / height)
    raw = HERE / f"{name}.rgba"
    np.clip(np.round(rgba * 255.0), 0, 255).astype(np.uint8).tofile(raw)
    ffmpeg("-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-i", str(raw), *FLAGS,
           "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgba", str(HERE / f"{name}.png"))
    raw.unlink()


def photo(index: int) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    def paint(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        phase = index * 1.3
        red = 0.5 + 0.5 * np.sin(6 * x + phase)
        green = 0.5 + 0.5 * np.sin(5 * y + phase * 0.7)
        blue = 0.5 + 0.5 * np.sin(4 * (x + y) + phase * 1.9)
        rings = 0.5 + 0.5 * np.cos(40 * np.hypot(x - 0.5, y - 0.5))
        return np.stack([red * rings, green, blue, np.ones_like(x)], axis=-1)

    return paint


def sticker(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    distance = np.hypot(x - 0.5, y - 0.5)
    alpha = np.clip((0.45 - distance) * 40, 0.0, 1.0)
    return np.stack([np.full_like(x, 0.96), np.full_like(x, 0.77), np.full_like(x, 0.26), alpha], axis=-1)


def warm_lut(path: Path, size: int = 17) -> None:
    lines = ["TITLE \"warm filmic\"", f"LUT_3D_SIZE {size}"]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                r, g, b = red / (size - 1), green / (size - 1), blue / (size - 1)
                lines.append(f"{min(1.0, r * 1.08 + 0.02):.6f} {g * 1.0:.6f} {max(0.0, b * 0.88):.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


LOGO = """<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240">
<rect x="10" y="10" width="220" height="220" rx="40" fill="#0d1b2a"/>
<circle cx="120" cy="120" r="70" fill="none" stroke="#f5c542" stroke-width="18"/>
<path d="M85 120 L112 147 L160 95" fill="none" stroke="#f5c542" stroke-width="18" stroke-linecap="round"/>
</svg>
"""


def generate() -> dict[str, str]:
    for name, source in SOURCES.items():
        video(name, source, 6.0)
    write_wav(HERE / "music.wav", music(16.0, 120.0, 7))
    write_wav(HERE / "drop.wav", music(8.0, 140.0, 9))
    write_wav(HERE / "vo.wav", voice(12.0, 5))
    for index in range(4):
        image(f"photo_{index}", 1600, 1200, photo(index))
    image("sticker", 256, 256, sticker)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-i", str(HERE / "vo.wav"), "-t", "8", *FLAGS,
           *VIDEO, "-c:a", "aac", "-b:a", "128k", "-shortest", str(HERE / "interview.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30,hue=H=floor(t/2)*1.5", "-i", str(HERE / "vo.wav"),
           "-t", "12", *FLAGS, *VIDEO, "-c:a", "aac", "-b:a", "128k", "-shortest", str(HERE / "talk.mp4"))
    warm_lut(HERE / "warm_filmic.cube")
    (HERE / "logo.svg").write_text(LOGO, encoding="utf-8")
    return digests()


def digests() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(HERE.iterdir()):
        if path.is_file() and path.suffix in (".mp4", ".wav", ".png", ".cube", ".svg"):
            found[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


if __name__ == "__main__":
    result = generate()
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
