from __future__ import annotations

import math

import numpy as np

from eks_harness.review.checks import CheckResult, Context, Expectation, ExpectationError, check

RATE = 16000
WINDOW = 512
HOP = 80


def detect_onsets(samples: np.ndarray, rate: int = RATE, *, sensitivity: float = 1.0,
                  min_gap: float = 0.05) -> list[float]:
    if samples.size < WINDOW * 2:
        return []
    padded = np.pad(samples.astype(np.float32), (WINDOW // 2, WINDOW // 2))
    count = 1 + (padded.size - WINDOW) // HOP
    index = np.arange(WINDOW)[None, :] + HOP * np.arange(count)[:, None]
    frames = padded[index] * np.hanning(WINDOW).astype(np.float32)
    magnitude = np.log1p(100.0 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0.0, np.diff(magnitude, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    if flux.max() <= 1e-9:
        return []
    flux = flux / flux.max()
    half = 12
    padded_flux = np.pad(flux, (half, half), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded_flux, 2 * half + 1)
    baseline = np.median(windows, axis=1)
    threshold = baseline + 0.12 / max(sensitivity, 1e-3)
    gap = max(1, round(min_gap * rate / HOP))
    found: list[int] = []
    for i in range(1, len(flux) - 1):
        if flux[i] < threshold[i] or flux[i] < flux[i - 1] or flux[i] < flux[i + 1]:
            continue
        lo, hi = max(0, i - 3), min(len(flux), i + 4)
        if flux[i] < flux[lo:hi].max():
            continue
        if found and i - found[-1] < gap:
            if flux[i] > flux[found[-1]]:
                found[-1] = i
            continue
        found.append(i)
    return [refine_onset(samples, rate, i * HOP / rate) for i in found]


def refine_onset(samples: np.ndarray, rate: int, t: float, before: float = 0.03, after: float = 0.04) -> float:
    a = max(0, int((t - before) * rate))
    b = min(samples.size, int((t + after) * rate))
    if b - a < 8:
        return t
    width = max(1, rate // 1000)
    envelope = np.convolve(np.abs(samples[a:b]), np.ones(width) / width, mode="same")
    peak = float(envelope.max())
    if peak <= 1e-6:
        return t
    floor = float(np.percentile(envelope, 10))
    rising = np.nonzero(envelope >= floor + 0.3 * (peak - floor))[0]
    return (a + int(rising[0])) / rate if rising.size else t


def _onsets(ctx: Context, sensitivity: float) -> list[float]:
    return ctx.cached(f"onsets:{sensitivity}", lambda: detect_onsets(ctx.audio(RATE), RATE, sensitivity=sensitivity))


def _no_audio(ctx: Context, item: Expectation) -> CheckResult | None:
    if ctx.info.has_audio:
        return None
    if item.params.get("require_audio"):
        return ctx.result(item, ok=False, detail="no audio stream")
    return ctx.result(item, status="skip", detail="no audio stream")


@check("loudness")
def loudness(ctx: Context, item: Expectation) -> CheckResult:
    missing = _no_audio(ctx, item)
    if missing:
        return missing
    values = ctx.loudness()
    problems = []
    integrated, peak = values.get("integrated"), values.get("truePeak")
    target = item.params.get("lufs")
    if target is not None:
        tolerance = float(item.params.get("lufs_tolerance", 1.0))
        if integrated is None or not math.isfinite(integrated) or abs(integrated - float(target)) > tolerance:
            problems.append(f"integrated {integrated} LUFS, want {float(target):g} +/- {tolerance:g}")
    ceiling = item.params.get("true_peak_max", -1.0)
    if ceiling is not None and peak is not None and peak > float(ceiling):
        problems.append(f"true peak {peak} dBTP over {float(ceiling):g}")
    detail = "; ".join(problems) or f"{integrated} LUFS, true peak {peak} dBTP, LRA {values.get('range')} LU"
    return ctx.result(item, ok=not problems, detail=detail, data=values)


@check("onset")
def onset(ctx: Context, item: Expectation) -> CheckResult:
    if item.t is None:
        raise ExpectationError("onset needs t")
    missing = _no_audio(ctx, item)
    if missing:
        return missing
    found = _onsets(ctx, float(item.params.get("sensitivity", 1.0)))
    ctx.cached("onsets", lambda: found)
    a, b = ctx.window(item, 0.25)
    near = [t for t in found if a <= t <= b]
    if not near:
        return ctx.result(item, ok=False, detail=f"no onset in {a:.2f}-{b:.2f}s")
    best = min(near, key=lambda t: abs(t - item.t))
    return ctx.result(item, measured=best)


@check("beats")
def beats(ctx: Context, item: Expectation) -> CheckResult:
    missing = _no_audio(ctx, item)
    if missing:
        return missing
    times = item.params.get("times") or item.params.get("beats")
    if not times:
        raise ExpectationError("beats needs params.times (a list of seconds)")
    found = np.array(_onsets(ctx, float(item.params.get("sensitivity", 1.0))))
    ctx.cached("onsets", lambda: found.tolist())
    tolerance = item.tolerance_frames
    rows = []
    for t in (float(x) for x in times):
        if found.size == 0:
            rows.append({"t": t, "measured": None, "deltaFrames": None, "ok": False})
            continue
        nearest = float(found[np.abs(found - t).argmin()])
        delta = round((nearest - t) * ctx.fps)
        rows.append({"t": round(t, 4), "measured": round(nearest, 4), "deltaFrames": delta,
                     "ok": abs(delta) <= tolerance})
    matched = sum(1 for r in rows if r["ok"])
    ratio = matched / len(rows)
    minimum = float(item.params.get("min_ratio", 0.9))
    deltas = [abs(r["deltaFrames"]) for r in rows if r["deltaFrames"] is not None]
    worst = max(deltas) if deltas else None
    detail = (f"{matched}/{len(rows)} beats within {tolerance}f (need {minimum:.0%}), "
              f"median {float(np.median(deltas)) if deltas else 0:.1f}f, worst {worst}f")
    misses = [r for r in rows if not r["ok"]]
    return ctx.result(item, ok=ratio >= minimum, detail=detail,
                      data={"matched": matched, "total": len(rows), "misses": misses[:40]})
