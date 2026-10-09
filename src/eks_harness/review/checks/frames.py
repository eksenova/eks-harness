from __future__ import annotations

import base64
import io
from typing import Any, Callable

import numpy as np

from eks_harness.review.checks import CheckResult, Context, Expectation, ExpectationError, check

PRESENCE_MODES = ("appears", "disappears", "present", "absent")


def pixel_box(box: Any, width: int, height: int, source: tuple[int, int]) -> tuple[int, int, int, int]:
    if box is None:
        return 0, 0, width, height
    if len(box) != 4:
        raise ExpectationError("box is [x, y, w, h]")
    x, y, w, h = (float(v) for v in box)
    if max(x, y, w, h) <= 1.0:
        sx, sy = width, height
    else:
        sx, sy = width / max(1, source[0]), height / max(1, source[1])
    left, top = int(round(x * sx)), int(round(y * sy))
    right, bottom = int(round((x + w) * sx)), int(round((y + h) * sy))
    left, top = max(0, min(left, width - 1)), max(0, min(top, height - 1))
    right, bottom = max(left + 1, min(right, width)), max(top + 1, min(bottom, height))
    return left, top, right, bottom


def parse_color(value: Any) -> tuple[int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return int(value[0]), int(value[1]), int(value[2])
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise ExpectationError(f"not a colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _frame_range(ctx: Context, a: float, b: float) -> tuple[int, int]:
    return ctx.info.frame_at(a), ctx.info.frame_at(b)


def search_presence(ctx: Context, item: Expectation, present: Callable[[int], bool],
                    default_seconds: float = 1.5) -> CheckResult:
    mode = str(item.params.get("mode") or ("present" if item.t is not None and item.params.get("at") else "appears"))
    if mode not in PRESENCE_MODES:
        raise ExpectationError(f"mode is one of {', '.join(PRESENCE_MODES)}")
    if mode in ("present", "absent"):
        if item.t is None:
            raise ExpectationError(f"mode {mode} needs t")
        until = item.params.get("until")
        first = ctx.info.frame_at(item.t)
        last = ctx.info.frame_at(float(until)) if until is not None else first
        step = max(1, (last - first) // 12)
        frames = list(range(first, last + 1, step))
        if frames[-1] != last:
            frames.append(last)
        wanted = mode == "present"
        misses = [n for n in frames if present(n) != wanted]
        detail = "" if not misses else f"{'missing' if wanted else 'visible'} at " + ", ".join(
            f"{ctx.info.time_of(n):.3f}s" for n in misses[:6])
        return ctx.result(item, ok=not misses, detail=detail, data={"frames": frames, "misses": misses})
    if item.t is None and item.params.get("window") is None:
        raise ExpectationError(f"mode {mode} needs t or a window")
    a, b = ctx.window(item, default_seconds)
    first, last = _frame_range(ctx, a, b)
    wanted = mode == "appears"

    def hit(n: int) -> bool:
        return present(n) == wanted

    step = max(1, (last - first) // 24)
    samples = list(range(first, last + 1, step))
    if samples[-1] != last:
        samples.append(last)
    previous = None
    found = None
    for n in samples:
        if hit(n):
            found = n
            break
        previous = n
    if found is None:
        state = "never appears" if wanted else "never disappears"
        return ctx.result(item, ok=False, detail=f"{state} in {a:.2f}-{b:.2f}s", data={"window": [a, b]})
    if previous is None:
        state = "already visible" if wanted else "already gone"
        return ctx.result(item, measured=ctx.info.time_of(found),
                          detail=f"{state} at window start {a:.2f}s", data={"window": [a, b], "frame": found})
    low, high = previous, found
    while high - low > 1:
        middle = (low + high) // 2
        if hit(middle):
            high = middle
        else:
            low = middle
    return ctx.result(item, measured=ctx.info.time_of(high), data={"window": [a, b], "frame": high})


def detect_cuts(ctx: Context, threshold: float = 0.1, ratio: float = 3.0) -> list[int]:
    d = ctx.diffs()
    found = []
    for i in range(1, len(d)):
        value = float(d[i])
        if value < threshold:
            continue
        lo, hi = max(1, i - 8), min(len(d), i + 9)
        neighbours = np.concatenate([d[lo:i], d[i + 1:hi]])
        baseline = max(float(np.median(neighbours)) if neighbours.size else 0.0, 0.002)
        if value < ratio * baseline:
            continue
        if value < float(d[max(1, i - 2): i + 3].max()):
            continue
        found.append(i)
    return found


@check("cut")
def cut(ctx: Context, item: Expectation) -> CheckResult:
    if item.t is None:
        raise ExpectationError("cut needs t")
    threshold = float(item.params.get("threshold", 0.1))
    ratio = float(item.params.get("ratio", 3.0))
    frames = ctx.cached(f"cutframes:{threshold}:{ratio}", lambda: detect_cuts(ctx, threshold, ratio))
    ctx.cached("cuts", lambda: [ctx.info.time_of(n) for n in frames])
    a, b = ctx.window(item, 1.0)
    near = [n for n in frames if a - 1e-6 <= ctx.info.time_of(n) <= b + 1e-6]
    if not near:
        return ctx.result(item, ok=False, detail=f"no cut in {a:.2f}-{b:.2f}s", data={"window": [a, b]})
    best = min(near, key=lambda n: abs(ctx.info.time_of(n) - item.t))
    score = float(ctx.diffs()[best])
    measured = ctx.info.time_of(best)
    result = ctx.result(item, measured=measured, data={"frame": best, "score": round(score, 4)})
    if result.status == "fail":
        result.detail = f"nearest cut {measured:.3f}s; cuts in window: " + ", ".join(
            f"{ctx.info.time_of(n):.3f}s" for n in near[:6])
    return result


@check("motion")
def motion(ctx: Context, item: Expectation) -> CheckResult:
    mode = str(item.params.get("mode") or "starts")
    if mode not in ("starts", "stops"):
        raise ExpectationError("motion mode is starts or stops")
    threshold = float(item.params.get("threshold", 0.004))
    hold = max(1, int(item.params.get("min_frames", 3)))
    d = ctx.diffs()
    a, b = ctx.window(item, 1.0)
    first, last = _frame_range(ctx, a, b)
    moving = d > threshold
    for n in range(max(1, first), min(last, len(d) - hold) + 1):
        run = moving[n: n + hold]
        if mode == "starts" and run.all() and not moving[n - 1]:
            return ctx.result(item, measured=ctx.info.time_of(n), data={"frame": n})
        if mode == "stops" and not run.any() and moving[n - 1]:
            return ctx.result(item, measured=ctx.info.time_of(n), data={"frame": n})
    return ctx.result(item, ok=False, detail=f"motion never {mode[:-1]}s in {a:.2f}-{b:.2f}s",
                      data={"window": [a, b]})


def _spans(mask: np.ndarray) -> list[tuple[int, int]]:
    spans = []
    start = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            spans.append((start, i - 1))
            start = None
    if start is not None:
        spans.append((start, len(mask) - 1))
    return spans


def _allowed(ctx: Context, item: Expectation) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in (item.params.get("allow") or [])]


def _span_result(ctx: Context, item: Expectation, spans: list[tuple[int, int]], limit: int, what: str) -> CheckResult:
    allowed = _allowed(ctx, item)
    rows = []
    for start, end in spans:
        frames = end - start + 1
        t0, t1 = ctx.info.time_of(start), ctx.info.time_of(end)
        if frames <= limit or any(a - 1e-6 <= t0 and t1 <= b + 1e-6 for a, b in allowed):
            continue
        rows.append({"start": round(t0, 4), "end": round(t1, 4), "frames": frames, "startFrame": start})
    detail = "" if not rows else f"{len(rows)} {what} span(s) over {limit} frames: " + ", ".join(
        f"{r['start']:.2f}-{r['end']:.2f}s ({r['frames']}f)" for r in rows[:5])
    return ctx.result(item, ok=not rows, detail=detail, data={"spans": rows, "limit": limit})


@check("black")
def black(ctx: Context, item: Expectation) -> CheckResult:
    pixel = float(item.params.get("pixel_threshold", 0.1)) * 255
    ratio = float(item.params.get("picture_ratio", 0.98))
    limit = int(item.params.get("max_frames", 2))
    gray = ctx.gray()
    mask = (gray <= pixel).mean(axis=(1, 2)) >= ratio
    return _span_result(ctx, item, _spans(mask), limit, "black")


@check("frozen")
def frozen(ctx: Context, item: Expectation) -> CheckResult:
    noise = float(item.params.get("noise", 0.0008))
    limit = int(item.params.get("max_frames", max(1, round(ctx.fps))))
    d = ctx.diffs()
    still = d <= noise
    if len(still):
        still[0] = False
    spans = [(max(0, s - 1), e) for s, e in _spans(still)]
    return _span_result(ctx, item, spans, limit, "frozen")


def _template(ctx: Context, item: Expectation) -> Any:
    from PIL import Image

    encoded = item.params.get("template_b64")
    if encoded:
        return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("L")
    path = item.params.get("template")
    if not path:
        raise ExpectationError("visual needs template (path) or color")
    file = ctx.resolve(str(path))
    if not file.is_file():
        raise ExpectationError(f"template {file} does not exist")
    return Image.open(file).convert("L")


def match_template(region: np.ndarray, template: np.ndarray) -> float:
    th, tw = template.shape
    rh, rw = region.shape
    if th > rh or tw > rw:
        return 0.0
    t = template.astype(np.float64)
    t = t - t.mean()
    t_norm = np.sqrt((t ** 2).sum())
    if t_norm < 1e-6:
        return 0.0
    r = region.astype(np.float64)
    shape = (rh + th - 1, rw + tw - 1)
    corr = np.fft.irfft2(np.fft.rfft2(r, shape) * np.conj(np.fft.rfft2(t, shape)), shape)[: rh - th + 1, : rw - tw + 1]
    integral = np.pad(r, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    integral2 = np.pad(r ** 2, ((1, 0), (1, 0))).cumsum(0).cumsum(1)

    def window_sum(table: np.ndarray) -> np.ndarray:
        return table[th:, tw:] - table[:-th, tw:] - table[th:, :-tw] + table[:-th, :-tw]

    n = th * tw
    sums = window_sum(integral)
    variance = window_sum(integral2) - sums ** 2 / n
    denominator = np.sqrt(np.maximum(variance, 1e-9)) * t_norm
    scores = corr / denominator
    scores[variance < 1e-3] = 0.0
    return float(np.clip(scores.max(), -1.0, 1.0)) if scores.size else 0.0


@check("visual")
def visual(ctx: Context, item: Expectation) -> CheckResult:
    source = (ctx.info.width, ctx.info.height)
    if item.params.get("color") is not None:
        color = np.array(parse_color(item.params["color"]), dtype=np.float32)
        tolerance = float(item.params.get("color_tolerance", 48))
        minimum = float(item.params.get("min_fraction", 0.25))
        width = int(item.params.get("analysis_width", 320))
        scores: dict[int, float] = {}

        def present(n: int) -> bool:
            if n not in scores:
                image = np.asarray(ctx.frame(n, width), dtype=np.float32)
                x0, y0, x1, y1 = pixel_box(item.params.get("box"), image.shape[1], image.shape[0], source)
                region = image[y0:y1, x0:x1].reshape(-1, 3)
                scores[n] = float((np.linalg.norm(region - color, axis=1) <= tolerance).mean())
            return scores[n] >= minimum

        result = search_presence(ctx, item, present)
        result.data["scores"] = {str(k): round(v, 3) for k, v in sorted(scores.items())[:40]}
        return result
    template_image = _template(ctx, item)
    width = int(item.params.get("analysis_width", min(ctx.info.width, 640)))
    factor = width / max(1, ctx.info.width)
    scale = float(item.params.get("template_scale", 1.0)) * factor
    tw, th = max(4, round(template_image.width * scale)), max(4, round(template_image.height * scale))
    template = np.asarray(template_image.resize((tw, th)), dtype=np.float32)
    if float(template.std()) < 2.0:
        raise ExpectationError("the template is one flat colour; use params.color for a colour region")
    threshold = float(item.params.get("threshold", 0.8))
    scores = {}

    def present(n: int) -> bool:
        if n not in scores:
            image = np.asarray(ctx.frame(n, width).convert("L"), dtype=np.float32)
            x0, y0, x1, y1 = pixel_box(item.params.get("box"), image.shape[1], image.shape[0], source)
            scores[n] = match_template(image[y0:y1, x0:x1], template)
        return scores[n] >= threshold

    result = search_presence(ctx, item, present)
    result.data["scores"] = {str(k): round(v, 3) for k, v in sorted(scores.items())[:40]}
    return result


def _margins(value: Any, width: int, height: int, source: tuple[int, int]) -> tuple[int, int, int, int]:
    values = [value] * 4 if isinstance(value, (int, float)) else list(value)
    if len(values) == 2:
        values = [values[0], values[1], values[0], values[1]]
    if len(values) != 4:
        raise ExpectationError("margins is a number, [vertical, horizontal] or [top, right, bottom, left]")
    top, right, bottom, left = (float(v) for v in values)
    if max(top, right, bottom, left) <= 0.5:
        return (round(top * height), round(right * width), round(bottom * height), round(left * width))
    sx, sy = width / max(1, source[0]), height / max(1, source[1])
    return round(top * sy), round(right * sx), round(bottom * sy), round(left * sx)


@check("safe_area")
def safe_area(ctx: Context, item: Expectation) -> CheckResult:
    width = int(item.params.get("analysis_width", 320))
    rate = float(item.params.get("sample_fps", 4))
    tolerance = float(item.params.get("tolerance", 40))
    limit = float(item.params.get("max_fraction", 0.01))
    background = item.params.get("background", "auto")
    source = (ctx.info.width, ctx.info.height)
    step = max(1, round(ctx.fps / max(rate, 0.1)))
    frames = list(range(0, ctx.info.frames, step))
    if item.params.get("window"):
        a, b = ctx.window(item)
        frames = [n for n in frames if a <= ctx.info.time_of(n) <= b]
    from eks_harness.review.media import select_frames, scaled_size

    w, h = scaled_size(ctx.info, width)
    images = select_frames(ctx.info, frames, w, h)
    violations = []
    for n in frames:
        image = images.get(n)
        if image is None:
            continue
        pixels = np.asarray(image, dtype=np.float32)
        top, right, bottom, left = _margins(item.params.get("margins", 0.05), w, h, source)
        mask = np.zeros((h, w), dtype=bool)
        mask[:top, :] = True
        if bottom:
            mask[h - bottom:, :] = True
        mask[:, :left] = True
        if right:
            mask[:, w - right:] = True
        band = pixels[mask]
        if band.size == 0:
            continue
        if background == "auto":
            quantized = (band // 16).astype(np.int32)
            keys = quantized[:, 0] * 256 + quantized[:, 1] * 16 + quantized[:, 2]
            mode = np.bincount(keys).argmax()
            reference = np.median(band[keys == mode], axis=0)
        else:
            reference = np.array(parse_color(background), dtype=np.float32)
        fraction = float((np.linalg.norm(band - reference, axis=1) > tolerance).mean())
        if fraction > limit:
            violations.append({"t": round(ctx.info.time_of(n), 4), "frame": n, "fraction": round(fraction, 4)})
    detail = "" if not violations else f"content in margins at {len(violations)} sampled frame(s): " + ", ".join(
        f"{v['t']:.2f}s ({v['fraction'] * 100:.1f}%)" for v in violations[:5])
    return ctx.result(item, ok=not violations, detail=detail,
                      data={"violations": violations[:50], "sampled": len(frames)})
