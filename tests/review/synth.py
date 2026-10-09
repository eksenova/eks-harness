from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FONTS = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FPS = 30
SIZE = "1280x720"
BEATS = [0.25 + 0.5 * i for i in range(16)]
CUTS = [2.0, 4.0, 4.5, 6.5]
TEXT_AT = 5.0
STAMP_AT = 5.5
STAMP_BOX = [900, 80, 220, 120]
TEXT = "EksenGOLD Kontör"


def font() -> str | None:
    return next((f for f in FONTS if Path(f).is_file()), None)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def build(out: Path, *, audio: bool = True) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    card = out.with_suffix(".text.png")
    image = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    face = font()
    typeface = ImageFont.truetype(face, 72) if face else ImageFont.load_default()
    ImageDraw.Draw(image).text((200, 520), TEXT, fill=(255, 255, 255, 255), font=typeface)
    image.save(card)
    x, y, w, h = STAMP_BOX
    graph = (
        f"color=c=0x1e3a8a:s={SIZE}:r={FPS}:d=2[a];"
        f"testsrc2=s={SIZE}:r={FPS}:d=2[b];"
        f"color=c=black:s={SIZE}:r={FPS}:d=0.5[c];"
        f"color=c=0x303030:s={SIZE}:r={FPS}:d=2[bg];"
        f"[bg][0:v]overlay=enable='gte(t,{TEXT_AT - 4.5})',"
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=0xd32f2f:t=fill:enable='gte(t,{STAMP_AT - 4.5})'[d];"
        f"color=c=white:s={SIZE}:r={FPS}:d=1.5[e];"
        "[a][b][c][d][e]concat=n=5:v=1:a=0,format=yuv420p[v]"
    )
    args = ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-t", "2", "-i", str(card),
            "-filter_complex", graph]
    if audio:
        clicks = "+".join(f"between(t,{b},{b + 0.012})" for b in BEATS)
        args += ["-f", "lavfi", "-i", f"aevalsrc='({clicks})*0.8*sin(2*PI*1500*t)':s=48000:d=8"]
    args += ["-map", "[v]"]
    if audio:
        args += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", "30", str(out)]
    subprocess.run(args, check=True)
    return out
