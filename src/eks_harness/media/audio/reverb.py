from __future__ import annotations

import math

import numpy as np
from scipy import signal

from eks_harness.media.audio.loudness import Signal

COMB_TUNING = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
ALLPASS_TUNING = (556, 441, 341, 225)
STEREO_SPREAD = 23
TUNING_RATE = 44100
INPUT_GAIN = 0.015
WET_SCALE = 3.0
ROOM_SCALE = 0.28
ROOM_OFFSET = 0.7
DAMP_SCALE = 0.4
ALLPASS_FEEDBACK = 0.5
JITTER = 8
DECAY_DB = 60.0
MAX_TAIL_SECONDS = 12.0


def _comb(data: Signal, delay: int, feedback: float, damp: float) -> Signal:
    count = data.shape[0]
    out = np.zeros(count, dtype=np.float64)
    written = np.zeros(delay, dtype=np.float64)
    state = np.zeros(1, dtype=np.float64)
    for start in range(0, count, delay):
        stop = min(start + delay, count)
        read = written[: stop - start]
        out[start:stop] = read
        filtered, state = signal.lfilter([1.0 - damp], [1.0, -damp], read, zi=state)
        written = data[start:stop] + feedback * np.asarray(filtered, dtype=np.float64)
    return out


def _allpass(data: Signal, delay: int) -> Signal:
    count = data.shape[0]
    out = np.empty(count, dtype=np.float64)
    written = np.zeros(delay, dtype=np.float64)
    for start in range(0, count, delay):
        stop = min(start + delay, count)
        read = written[: stop - start]
        chunk = data[start:stop]
        out[start:stop] = read - chunk
        written = chunk + read * ALLPASS_FEEDBACK
    return out


def _tunings(rate: int, seed: int, offset: int) -> tuple[list[int], list[int]]:
    rng = np.random.default_rng(seed)
    scale = rate / TUNING_RATE
    combs = [max(1, round((base + offset) * scale) + int(rng.integers(0, JITTER))) for base in COMB_TUNING]
    allpasses = [max(1, round((base + offset) * scale) + int(rng.integers(0, JITTER))) for base in ALLPASS_TUNING]
    return combs, allpasses


def _feedback(room_size: float) -> float:
    return room_size * ROOM_SCALE + ROOM_OFFSET


def tail_samples(rate: int, room_size: float) -> int:
    feedback = _feedback(room_size)
    longest = (max(COMB_TUNING) + STEREO_SPREAD + JITTER) * rate / TUNING_RATE
    loops = DECAY_DB / (-20.0 * math.log10(feedback))
    return min(round(loops * longest), round(MAX_TAIL_SECONDS * rate))


def freeverb(samples: Signal, rate: int, *, room_size: float, damping: float, seed: int) -> Signal:
    feedback = _feedback(room_size)
    damp = damping * DAMP_SCALE
    mono = samples.mean(axis=1) * INPUT_GAIN
    channels: list[Signal] = []
    for channel in range(samples.shape[1]):
        combs, allpasses = _tunings(rate, seed + channel, STEREO_SPREAD * channel)
        wet = np.zeros(samples.shape[0], dtype=np.float64)
        for delay in combs:
            wet += _comb(mono, delay, feedback, damp)
        for delay in allpasses:
            wet = _allpass(wet, delay)
        channels.append(wet * WET_SCALE)
    return np.stack(channels, axis=1)


__all__ = ["freeverb", "tail_samples"]
