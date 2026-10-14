from __future__ import annotations

from typing import cast

import numpy as np
from scipy.ndimage import minimum_filter1d

from eks_harness.media.audio.loudness import OVERSAMPLE, Signal, oversampled, true_peak

LOOKAHEAD_SECONDS = 0.005
HOLD_SECONDS = 0.04
MARGIN_DB = 0.05
PASSES = 6


def _sample_peaks(samples: Signal) -> Signal:
    count = samples.shape[0]
    dense = np.abs(oversampled(samples)).max(axis=1)[: count * OVERSAMPLE]
    dense = np.pad(dense, (0, count * OVERSAMPLE - dense.shape[0]))
    peaks = np.maximum(dense.reshape(count, OVERSAMPLE).max(axis=1), np.abs(samples).max(axis=1))
    widened = peaks.copy()
    widened[1:] = np.maximum(widened[1:], peaks[:-1])
    widened[:-1] = np.maximum(widened[:-1], peaks[1:])
    return widened


def _gain(peaks: Signal, ceiling: float, lookahead: int, hold: int) -> Signal:
    with np.errstate(divide="ignore"):
        wanted = np.where(peaks > ceiling, ceiling / np.maximum(peaks, 1e-300), 1.0)
    size = lookahead + hold
    padded = np.concatenate([np.ones(hold), wanted, np.ones(lookahead)])
    window = minimum_filter1d(padded, size=size, mode="constant", cval=1.0, origin=-(size // 2))
    floor = np.asarray(cast(Signal, window)[: wanted.shape[0]], dtype=np.float64)
    sums = np.concatenate([[0.0], np.cumsum(floor)])
    index = np.arange(wanted.shape[0], dtype=np.int64)
    low = np.maximum(0, index - lookahead + 1)
    return (sums[index + 1] - sums[low]) / (index + 1 - low)


def limit(samples: Signal, rate: int, ceiling_db: float) -> Signal:
    if samples.shape[0] == 0:
        return samples
    allowed = 10.0 ** (ceiling_db / 20.0)
    target = 10.0 ** ((ceiling_db - MARGIN_DB) / 20.0)
    lookahead = max(1, round(LOOKAHEAD_SECONDS * rate))
    hold = max(1, round(HOLD_SECONDS * rate))
    out = samples
    for _ in range(PASSES):
        if true_peak(out) <= allowed:
            return out
        out = out * _gain(_sample_peaks(out), target, lookahead, hold)[:, None]
    peak = true_peak(out)
    return out * (target / peak) if peak > allowed else out


__all__ = ["limit"]
