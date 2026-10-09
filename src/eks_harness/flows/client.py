from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FONT_CANDIDATES = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "C:/Windows/Fonts/arial.ttf")


class StepError(RuntimeError):
    def __init__(self, step: str, message: str, detail: dict | None = None):
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message
        self.detail = detail or {}


@dataclass
class Step:
    name: str
    args: str
    ms: int
    ok: bool
    error: str | None = None


@dataclass
class Artifact:
    kind: str
    name: str
    id: str | None
    url: str | None
    raw: str | None
    session: str | None
    local: str | None = None
    sheet: str | None = None
    extra: dict = field(default_factory=dict)
    marks: list = field(default_factory=list)


def artifact_of(kind: str, name: str, value: dict | None) -> Artifact | None:
    if not isinstance(value, dict):
        return None
    store: dict = value
    if isinstance(value.get("store"), dict):
        store = value["store"]
    if isinstance(store.get("video"), dict):
        store = store["video"]
    return Artifact(kind=kind, name=name, id=store.get("id"), url=store.get("url"), raw=store.get("rawUrl"),
                    session=store.get("sessionUrl"))


def download(client: Any, ids: list[str], out: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    out.mkdir(parents=True, exist_ok=True)
    for artifact_id in [i for i in ids if i]:
        try:
            info = client.get(f"/api/artifacts/{artifact_id}")
            raw = str(info.get("rawUrl") or "")
            path = raw.split("://", 1)[-1].split("/", 1)[-1] if "://" in raw else raw.lstrip("/")
            target = out / f"{artifact_id}-{info.get('filename') or 'artifact'}"
            found[artifact_id] = client.download("/" + path, target, params={"download": "1"})
        except Exception:
            continue
    return found


def _font(size: int, image_font: Any) -> Any:
    for candidate in FONT_CANDIDATES:
        try:
            return image_font.truetype(candidate, size)
        except OSError:
            continue
    return image_font.load_default()


def probe_video(video: Path) -> tuple[float, int, int] | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(video)], capture_output=True, text=True)
    try:
        info = json.loads(out.stdout)
        stream = info["streams"][0]
        return float(info["format"]["duration"]), int(stream["width"]), int(stream["height"])
    except (json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None


PRIORITY = {"callout": 0, "spotlight": 0, "highlight": 1, "title": 1, "caption": 2}


def sheet_times(duration: float, marks: list[dict], count: int) -> list[tuple[float, str]]:
    picked: list[tuple[float, str]] = []
    for mark in sorted(marks, key=lambda m: PRIORITY.get(str(m.get("kind")), 3)):
        at = float(mark.get("at") or 0) + 0.9
        if 0 <= at < duration and len(picked) < count - 1 and all(abs(at - t) > 0.8 for t, _ in picked):
            label = mark.get("text") or mark.get("kind") or ""
            picked.append((at, f"{mark.get('kind', '')}: {label}"[:60]))
    end = max(0.0, duration - 0.3)
    if all(abs(end - t) > 0.8 for t, _ in picked):
        picked.append((end, "end"))
    while len(picked) < count:
        points = sorted([0.0] + [t for t, _ in picked] + [duration])
        gap, at = max((b - a, (a + b) / 2) for a, b in zip(points, points[1:]))
        if gap < 1.6:
            break
        picked.append((at, ""))
    return sorted(picked)


def contact_sheet(video: Path, out: Path, marks: list[dict] | None = None, count: int = 12) -> Path | None:
    meta = probe_video(video)
    if not meta:
        return None
    duration, width, height = meta
    portrait = height > width
    cols = 6 if portrait else 3
    tile_w = 300 if portrait else 640
    tile_h = int(tile_w * height / width)
    times = sheet_times(duration, marks or [], count)
    frames_dir = out.parent / f"{out.stem}-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    tiles: list[tuple[Path, float, str]] = []
    for n, (t, label) in enumerate(times):
        frame = frames_dir / f"{n:02d}.png"
        res = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                              "-vf", f"scale={tile_w}:{tile_h}", str(frame)], capture_output=True)
        if res.returncode == 0 and frame.exists():
            tiles.append((frame, t, label))
    if not tiles:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    rows = (len(tiles) + cols - 1) // cols
    band = 30
    pad = 8
    sheet = Image.new("RGB", (cols * (tile_w + pad) + pad, rows * (tile_h + band + pad) + pad), "white")
    draw = ImageDraw.Draw(sheet)
    font = _font(17, ImageFont)
    for n, (frame, t, label) in enumerate(tiles):
        x = pad + (n % cols) * (tile_w + pad)
        y = pad + (n // cols) * (tile_h + band + pad)
        sheet.paste(Image.open(frame).convert("RGB"), (x, y + band))
        text = f"{int(t // 60)}:{t % 60:04.1f}  {label}".strip()
        draw.rectangle([x, y, x + tile_w, y + band - 2], fill=(20, 21, 24))
        draw.text((x + 8, y + 5), text[: 70 if not portrait else 32], fill=(255, 255, 255), font=font)
    sheet.save(out)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return out


def marks_to_markers(marks: list[dict] | None) -> dict:
    ticks, frames = [], []
    for mark in marks or []:
        try:
            at = float(mark.get("at") or 0)
        except (TypeError, ValueError):
            continue
        kind = str(mark.get("kind") or "mark")
        label = f"{kind}: {mark.get('text') or ''}".strip(": ")[:40]
        ticks.append({"t": at, "kind": "cue", "label": label})
        frames.append({"t": at + 0.5, "label": label})
    return {"markers": ticks, "frames": frames}


def stored_sheet(client: Any, art: Artifact, out: Path) -> bool:
    from eks_harness.cli.client import NotFound

    try:
        data = client.post(f"/api/artifacts/{art.id}/sheet", json={"markers": marks_to_markers(art.marks)},
                           timeout=900)
    except NotFound:
        return False
    sheets = data.get("sheets") or []
    found = download(client, [s["id"] for s in sheets], out)
    rows = [{"page": s.get("url"), "local": str(found[s["id"]]) if s["id"] in found else None,
             "range": s.get("range")} for s in sheets]
    if rows:
        art.sheet = rows[0]["local"] or rows[0]["page"]
        art.extra["sheetPage"] = rows[0]["page"]
        if len(rows) > 1:
            art.extra["sheets"] = rows
    return bool(rows)


class Timer:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)
