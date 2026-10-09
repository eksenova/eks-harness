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
