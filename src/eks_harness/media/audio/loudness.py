from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray
from scipy import signal

Signal = NDArray[np.float64]

BLOCK_SECONDS = 0.4
STEP_SECONDS = 0.1
ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0
LOUDNESS_OFFSET = -0.691
OVERSAMPLE = 4
SHELF_HZ = 1681.974450955533
SHELF_GAIN_DB = 3.999843853973347
SHELF_Q = 0.7071752369554196
HIGHPASS_HZ = 38.13547087602444
HIGHPASS_Q = 0.5003270373238773


def k_weighting(rate: int) -> Signal:
    k = math.tan(math.pi * SHELF_HZ / rate)
    vh = 10.0 ** (SHELF_GAIN_DB / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / SHELF_Q + k * k
    shelf = [(vh + vb * k / SHELF_Q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / SHELF_Q + k * k) / a0,
             1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / SHELF_Q + k * k) / a0]
    k = math.tan(math.pi * HIGHPASS_HZ / rate)
    a0 = 1.0 + k / HIGHPASS_Q + k * k
    highpass = [1.0, -2.0, 1.0, 1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / HIGHPASS_Q + k * k) / a0]
    return np.array([shelf, highpass], dtype=np.float64)


def block_powers(samples: Signal, rate: int) -> Signal:
    weighted = np.asarray(signal.sosfilt(k_weighting(rate), samples, axis=0), dtype=np.float64)
    block = round(BLOCK_SECONDS * rate)
    step = round(STEP_SECONDS * rate)
    count = (weighted.shape[0] - block) // step + 1 if weighted.shape[0] >= block else 0
    if count <= 0:
        return np.zeros((0, samples.shape[1]), dtype=np.float64)
    sums = np.concatenate([np.zeros((1, samples.shape[1])), np.cumsum(weighted * weighted, axis=0)])
    starts = np.arange(count) * step
    return (sums[starts + block] - sums[starts]) / block


def _loudness(power: Signal) -> Signal:
    with np.errstate(divide="ignore"):
        return LOUDNESS_OFFSET + 10.0 * np.log10(power)


def integrated_loudness(samples: Signal, rate: int) -> float:
    powers = block_powers(samples, rate)
    if powers.shape[0] == 0:
        return -math.inf
    levels = _loudness(powers.sum(axis=1))
    kept = powers[levels > ABSOLUTE_GATE]
    if kept.shape[0] == 0:
        return -math.inf
    relative = float(_loudness(np.asarray(kept.mean(axis=0).sum()))) + RELATIVE_GATE
    gated = powers[(levels > ABSOLUTE_GATE) & (levels > relative)]
    if gated.shape[0] == 0:
        return -math.inf
    return float(_loudness(np.asarray(gated.mean(axis=0).sum())))


def oversampled(samples: Signal) -> Signal:
    return np.asarray(signal.resample_poly(samples, OVERSAMPLE, 1, axis=0), dtype=np.float64)


def true_peak(samples: Signal) -> float:
    if samples.shape[0] == 0:
        return 0.0
    return float(max(np.max(np.abs(oversampled(samples))), np.max(np.abs(samples))))


def to_db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0.0 else -math.inf


def true_peak_db(samples: Signal) -> float:
    return to_db(true_peak(samples))


__all__ = [
    "ABSOLUTE_GATE",
    "RELATIVE_GATE",
    "Signal",
    "block_powers",
    "integrated_loudness",
    "k_weighting",
    "oversampled",
    "to_db",
    "true_peak",
    "true_peak_db",
]
