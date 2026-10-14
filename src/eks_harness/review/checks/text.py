from __future__ import annotations

from eks_harness.review import ocr
from eks_harness.review.checks import CheckResult, Context, Expectation, ExpectationError, check
from eks_harness.review.checks.frames import pixel_box, search_presence


@check("text")
def text(ctx: Context, item: Expectation) -> CheckResult:
    wanted = str(item.params.get("text") or "")
    if not wanted:
        raise ExpectationError("text needs params.text")
    box = item.params.get("box")
    languages = item.params.get("languages") or item.params.get("lang")
    if isinstance(languages, str):
        languages = [languages]
    engine = str(item.params.get("engine") or "auto")
    min_height = int(item.params.get("min_height", 240))
    seen: dict[int, str] = {}

    def present(n: int) -> bool:
        if n not in seen:
            image = ctx.frame(n)
            x0, y0, x1, y1 = pixel_box(box, image.width, image.height, (ctx.info.width, ctx.info.height))
            crop = image.crop((x0, y0, x1, y1))
            if crop.height < min_height:
                factor = min(4.0, min_height / max(1, crop.height))
                crop = crop.resize((max(1, round(crop.width * factor)), max(1, round(crop.height * factor))))
            lines = ocr.read_images([crop], languages=languages, name=engine)[0]
            seen[n] = " ".join(line.text for line in lines)
        return ocr.contains(seen[n], wanted)

    result = search_presence(ctx, item, present)
    frame = result.data.get("frame")
    if frame is not None and frame in seen:
        result.data["read"] = seen[frame][:300]
    elif seen:
        last = max(seen)
        result.data["read"] = seen[last][:300]
        if result.status == "fail" and not result.detail.endswith(")"):
            result.detail += f" (last read {ctx.info.time_of(last):.2f}s: {seen[last][:80]!r})"
    result.data["ocrFrames"] = len(seen)
    return result


SAFE_EPSILON = 0.005


@check("text_safe_area")
def text_safe_area(ctx: Context, item: Expectation) -> CheckResult:
    from eks_harness.review.media import select_frames, scaled_size

    margins = item.params.get("margins", (0.1, 0.05, 0.2, 0.05))
    if len(margins) != 4:
        raise ExpectationError("margins is [top, right, bottom, left] as fractions of the frame")
    top, right, bottom, left = (float(value) for value in margins)
    languages = item.params.get("languages") or None
    engine = str(item.params.get("engine") or "auto")
    rate = float(item.params.get("sample_fps", 2))
    limit = max(1, int(item.params.get("max_frames", 24)))
    shortest = int(item.params.get("min_chars", 2))
    if item.params.get("window") is not None:
        a, b = ctx.window(item)
    else:
        a, b = 0.0, ctx.info.duration
    first, last = ctx.info.frame_at(a), ctx.info.frame_at(b)
    step = max(1, round(ctx.fps / max(rate, 0.01)), -(-(last - first + 1) // limit))
    frames = list(range(first, last + 1, step))
    width, height = scaled_size(ctx.info, min(ctx.info.width, 1280))
    images = select_frames(ctx.info, frames, width, height)
    picked = [n for n in frames if n in images]
    read = ocr.read_images([images[n] for n in picked], languages=languages, name=engine, boxes=True)
    violations = []
    found = 0
    for n, lines in zip(picked, read):
        for line in lines:
            if line.box is None or len(ocr.normalize(line.text).replace(" ", "")) < shortest:
                continue
            found += 1
            x, y, w, h = line.box
            outside = (x < left - SAFE_EPSILON or y < top - SAFE_EPSILON or x + w > 1 - right + SAFE_EPSILON
                       or y + h > 1 - bottom + SAFE_EPSILON)
            if outside:
                violations.append({"t": round(ctx.info.time_of(n), 4), "frame": n, "text": line.text[:80],
                                   "box": [round(v, 4) for v in line.box]})
    detail = (f"{found} text line(s) inside the safe area" if not violations else
              f"{len(violations)} text line(s) outside the safe area: " + ", ".join(
                  f"{v['t']:.2f}s {v['text']!r}" for v in violations[:5]))
    return ctx.result(item, ok=not violations, detail=detail,
                      data={"violations": violations[:50], "sampled": len(picked), "lines": found})
