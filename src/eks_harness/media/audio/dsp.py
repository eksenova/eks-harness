from __future__ import annotations

import math

import numpy as np
from scipy import signal

from eks_harness.media.audio.loudness import Signal

CONTROL_SECONDS = 0.001
EQ_BLOCK_SECONDS = 0.01
DUCK_WINDOW_SECONDS = 0.02
POWER_FLOOR = 1e-30


def db_gain(db: Signal | float) -> Signal:
    return np.power(10.0, np.asarray(db, dtype=np.float64) / 20.0)


def biquad(kind: str, frequency: float, gain_db: float, q: float, rate: int) -> tuple[Signal, Signal]:
    w0 = 2.0 * math.pi * frequency / rate
    cos = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * q)
    a = 10.0 ** (gain_db / 40.0)
    root = 2.0 * math.sqrt(a) * alpha
    if kind == "peak":
        b = [1.0 + alpha * a, -2.0 * cos, 1.0 - alpha * a]
        den = [1.0 + alpha / a, -2.0 * cos, 1.0 - alpha / a]
    elif kind == "low_shelf":
        b = [a * ((a + 1) - (a - 1) * cos + root), 2 * a * ((a - 1) - (a + 1) * cos), a * ((a + 1) - (a - 1) * cos - root)]
        den = [(a + 1) + (a - 1) * cos + root, -2 * ((a - 1) + (a + 1) * cos), (a + 1) + (a - 1) * cos - root]
    elif kind == "high_shelf":
        b = [a * ((a + 1) + (a - 1) * cos + root), -2 * a * ((a - 1) + (a + 1) * cos), a * ((a + 1) + (a - 1) * cos - root)]
        den = [(a + 1) - (a - 1) * cos + root, 2 * ((a - 1) - (a + 1) * cos), (a + 1) - (a - 1) * cos - root]
    elif kind == "low_pass":
        b = [(1.0 - cos) / 2.0, 1.0 - cos, (1.0 - cos) / 2.0]
        den = [1.0 + alpha, -2.0 * cos, 1.0 - alpha]
    elif kind == "high_pass":
        b = [(1.0 + cos) / 2.0, -(1.0 + cos), (1.0 + cos) / 2.0]
        den = [1.0 + alpha, -2.0 * cos, 1.0 - alpha]
    else:
        raise ValueError(f"unknown equalizer band type {kind!r}")
    return np.array(b, dtype=np.float64) / den[0], np.array(den, dtype=np.float64) / den[0]


def filter_bands(samples: Signal, rate: int, kind: str, frequency: float, q: float, gains: Signal,
                 block: int) -> Signal:
    if samples.shape[0] == 0:
        return samples
    state = np.zeros((2, samples.shape[1]), dtype=np.float64)
    if np.all(gains == gains[0]):
        b, a = biquad(kind, frequency, float(gains[0]), q, rate)
        return np.asarray(signal.lfilter(b, a, samples, axis=0), dtype=np.float64)
    out = np.empty_like(samples)
    for index, start in enumerate(range(0, samples.shape[0], block)):
        stop = min(start + block, samples.shape[0])
        b, a = biquad(kind, frequency, float(gains[index]), q, rate)
        chunk, state = signal.lfilter(b, a, samples[start:stop], axis=0, zi=state)
        out[start:stop] = chunk
    return out


def frame_powers(samples: Signal, hop: int) -> Signal:
    power = (samples * samples).mean(axis=1)
    frames = -(-power.shape[0] // hop)
    padded = np.pad(power, (0, frames * hop - power.shape[0]))
    return padded.reshape(frames, hop).mean(axis=1)


def coefficient(seconds: float, hop: int, rate: int) -> float:
    if seconds <= 0.0:
        return 0.0
    return math.exp(-hop / (seconds * rate))


def ballistics(target: Signal, *, rise: float, fall: float, start: float) -> Signal:
    out = np.empty_like(target)
    level = start
    for index, value in enumerate(target.tolist()):
        coef = rise if value > level else fall
        level = value + coef * (level - value)
        out[index] = level
    return out


def per_sample(values: Signal, hop: int, count: int) -> Signal:
    centers = (np.arange(values.shape[0], dtype=np.float64) + 0.5) * hop
    return np.interp(np.arange(count, dtype=np.float64), centers, values)


def compress(samples: Signal, rate: int, *, threshold_db: float, ratio: float, attack: float, release: float,
             makeup_db: float) -> Signal:
    if samples.shape[0] == 0:
        return samples
    hop = max(1, round(CONTROL_SECONDS * rate))
    power = frame_powers(samples, hop)
    detector = ballistics(power, rise=coefficient(attack, hop, rate), fall=coefficient(release, hop, rate),
                          start=0.0)
    level = 10.0 * np.log10(np.maximum(detector, POWER_FLOOR))
    reduction = np.maximum(level - threshold_db, 0.0) * (1.0 - 1.0 / ratio)
    gain = per_sample(makeup_db - reduction, hop, samples.shape[0])
    return samples * db_gain(gain)[:, None]


def duck_gain(under: Signal, rate: int, *, amount_db: float, threshold_db: float, attack: float,
              release: float) -> Signal:
    count = under.shape[0]
    if count == 0:
        return np.ones(0, dtype=np.float64)
    hop = max(1, round(CONTROL_SECONDS * rate))
    power = frame_powers(under, hop)
    window = max(1, round(DUCK_WINDOW_SECONDS * rate / hop))
    sums = np.concatenate([[0.0], np.cumsum(power)])
    index = np.arange(power.shape[0], dtype=np.int64)
    low = np.maximum(0, index - window + 1)
    rms_db = 10.0 * np.log10(np.maximum((sums[index + 1] - sums[low]) / (index + 1 - low), POWER_FLOOR))
    target = np.where(rms_db > threshold_db, amount_db, 0.0)
    smoothed = ballistics(-target, rise=coefficient(attack, hop, rate), fall=coefficient(release, hop, rate),
                          start=0.0)
    return db_gain(-per_sample(smoothed, hop, count))


def fade(count: int, fade_in: int, fade_out: int) -> Signal:
    envelope = np.ones(count, dtype=np.float64)
    if fade_in > 0:
        length = min(fade_in, count)
        envelope[:length] *= np.arange(length, dtype=np.float64) / fade_in
    if fade_out > 0:
        length = min(fade_out, count)
        envelope[count - length:] *= (np.arange(length, 0, -1, dtype=np.float64) - 1.0) / fade_out
    return envelope


def pan(samples: Signal, positions: Signal) -> Signal:
    if samples.shape[1] != 2:
        return samples
    theta = (np.clip(positions, -1.0, 1.0) + 1.0) * (math.pi / 4.0)
    left = np.cos(theta) * math.sqrt(2.0)
    right = np.sin(theta) * math.sqrt(2.0)
    return samples * np.stack([left, right], axis=1)


__all__ = [
    "CONTROL_SECONDS",
    "EQ_BLOCK_SECONDS",
    "ballistics",
    "biquad",
    "compress",
    "db_gain",
    "duck_gain",
    "fade",
    "filter_bands",
    "frame_powers",
    "pan",
    "per_sample",
]
