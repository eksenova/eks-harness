from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from eks_harness.capture.steplog import event_times, read_jsonl
from eks_harness.store.encode import (
    FPS,
    EncodeError,
    EncodeResult,
    _ffmpeg,
    _probe,
    _tools,
    encode_recording,
    target_size,
    trim_idle,
    trim_steps,
    verify,
)


def read_timed_frames(listing: Path) -> list[tuple[float, Path]]:
    rows: list[tuple[float, Path]] = []
    for line in listing.read_text(encoding="utf-8").splitlines():
        stamp, _, path = line.partition("\t")
        if path:
            rows.append((float(stamp), Path(path)))
    if not rows:
        raise EncodeError(f"no frames in {listing}")
    rows.sort(key=lambda row: row[0])
    return rows


def resample(rows: list[tuple[float, Path]], target: Path, fps: int = FPS) -> tuple[int, str]:
    start = rows[0][0]
    span = rows[-1][0] - start
    count = int(round(span * fps)) + 1
    index = 0
    extension = rows[0][1].suffix or ".jpg"
    for n in range(count):
        at = start + n / fps
        while index + 1 < len(rows) and rows[index + 1][0] <= at + 1e-6:
            index += 1
        source = rows[index][1]
        link = target / f"{n:07d}{extension}"
        try:
            os.link(source, link)
        except OSError:
            shutil.copyfile(source, link)
    return count, extension


def encode_timed_frames(listing: Path, out: Path, *, trim: bool = False, events: list[float] | None = None,
                        crop: str | None = None) -> EncodeResult:
    ffmpeg, ffprobe = _tools()
    rows = read_timed_frames(listing)
    with tempfile.TemporaryDirectory(prefix="ehx-frames-") as folder:
        sequence = Path(folder)
        count, extension = resample(rows, sequence)
        expected = count / FPS
        if crop:
            parts = crop.split(":")
            width, height = int(parts[0]), int(parts[1])
        else:
            stream = next(s for s in _probe(ffprobe, sequence / f"0000000{extension}").get("streams") or []
                          if s.get("codec_type") == "video")
            width, height = int(stream["width"]), int(stream["height"])
        target_w, target_h = target_size(width, height)
        filters = [f"crop={crop}"] if crop else []
        filters += [f"scale={target_w}:{target_h}:flags=lanczos:out_range=tv", "format=yuv420p", "setsar=1"]
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        encoding = out.with_name(out.stem + ".encoding.mp4")
        _ffmpeg(ffmpeg, ["-framerate", str(FPS), "-i", str(sequence / f"%07d{extension}"),
                         "-vf", ",".join(filters) + f",fps={FPS}"], encoding)
    report: list[str] = []
    try:
        report.append(verify(ffprobe, encoding, expected, "full"))
    except EncodeError:
        os.replace(encoding, out.with_name(out.stem + ".failed-encode.mp4"))
        raise
    os.replace(encoding, out)
    result = EncodeResult(full=out, report=report)
    if trim:
        steps = [at for at in events or [] if 0 <= at <= expected]
        result.trimmed = (trim_steps(ffmpeg, ffprobe, out, steps, report) if steps
                          else trim_idle(ffmpeg, ffprobe, out, report))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eks_harness.capture.encode",
                                     description="Encode a recording into the deliverable MP4 (H.264 High <= 4.1, "
                                                 "yuv420p, 30 fps CFR, faststart, no audio) and verify it.")
    parser.add_argument("input", nargs="?", help="a recorded video (or use --timed-frames)")
    parser.add_argument("--timed-frames", type=Path, help="lines of '<seconds>\\t<image path>'")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--trim", action="store_true", help="also write <name>-trimmed.mp4")
    parser.add_argument("--step-log", type=Path, help="JSONL step log that drives the trim")
    parser.add_argument("--crop", help="W:H:X:Y")
    parser.add_argument("--pad-to", type=float, help="make the clip exactly this many seconds long")
    parser.add_argument("--json", action="store_true", help="print a JSON result instead of paths")
    parser.add_argument("--sid", default=os.environ.get("EHX_LEASE_SID") or None,
                        help="lease of the recording; inside a render that holds this lease the encode uses its slot")
    args = parser.parse_args(argv)
    events = event_times(read_jsonl(args.step_log)) if args.step_log and args.step_log.is_file() else None
    try:
        if args.timed_frames:
            if not args.out:
                parser.error("--timed-frames needs --out")
            from eks_harness.renderq import render_slot

            with render_slot("encode", args.out.name, lease=args.sid, session=args.sid):
                result = encode_timed_frames(args.timed_frames, args.out, trim=args.trim, events=events,
                                             crop=args.crop)
        elif args.input:
            source = Path(args.input)
            out = args.out or source.with_suffix(".mp4")
            if out.resolve() == source.resolve():
                out = source.with_name(source.stem + "-encoded.mp4")
            result = encode_recording(source, out, pad_to=args.pad_to, trim=args.trim, crop=args.crop, events=events,
                                      lease=args.sid)
        else:
            parser.error("pass an input video or --timed-frames")
    except EncodeError as error:
        print(str(error), file=sys.stderr)
        return 3 if error.missing_tool else 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    for line in result.report:
        print(line, file=sys.stderr)
    if args.json:
        print(json.dumps({"full": str(result.full), "trimmed": str(result.trimmed) if result.trimmed else None,
                          "report": result.report}))
    else:
        print(result.full)
        if result.trimmed:
            print(result.trimmed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
