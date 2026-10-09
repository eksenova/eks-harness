from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from eks_harness.paths import resolve_paths


class AnalysisError(RuntimeError):
    pass


def _cache_dir() -> Path:
    path = resolve_paths().cache_dir / "score"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AnalysisError("ffprobe is not on PATH")
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise AnalysisError(f"ffprobe failed on {path}: {out.stderr.strip()}")
    return float(json.loads(out.stdout)["format"]["duration"])


def _window_clip(path: Path, window: tuple[float, float]) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AnalysisError("ffmpeg is not on PATH")
    stat = path.stat()
    key = hashlib.sha256(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{window}".encode()).hexdigest()[:20]
    target = _cache_dir() / f"window-{key}.wav"
    if not target.exists():
        start, end = window
        out = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
                              "-i", str(path), "-ac", "1", "-ar", "44100", str(target)],
                             capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise AnalysisError(f"ffmpeg could not cut {path}: {out.stderr.strip()}")
    return target


def beat_analyzer(path: Path, window: tuple[float, float] | None = None, *,
                  backend: str = "auto") -> dict[str, list[float]]:
    try:
        from eks_harness.video.ir.markers import BeatTracker
        from eks_harness.video.plugins.builtin.markers._cache import MarkerCache, compute_audio_digest
        from eks_harness.video.plugins.builtin.markers.beat_tracker import BeatTrackerExtractor
    except ImportError as error:
        raise AnalysisError(f"beat analysis needs eks-harness[video]: {error}") from error
    if not path.is_file():
        raise AnalysisError(f"no audio file {path}")
    audio = _window_clip(path, window) if window else path
    source = BeatTracker(name="score", source=str(audio), backend=backend)
    extractor = BeatTrackerExtractor()
    digest = compute_audio_digest(audio)
    cache = MarkerCache(_cache_dir())
    payload = None
    try:
        for name in extractor._chain_for(source.backend):
            key = f"beat-{digest}-auto-{name}"
            payload = cache.lookup(key)
            if payload is not None:
                break
            try:
                payload = extractor._run_backend(name, audio, source)
            except Exception:
                payload = None
                continue
            if payload is not None:
                cache.store(key, payload)
                break
    finally:
        cache.close()
    if payload is None:
        raise AnalysisError(f"no beat tracking backend could analyze {path}")
    beats = sorted(float(t) for t in payload.get("beat") or payload.get("beats") or [])
    downbeats = sorted(float(t) for t in payload.get("downbeat") or payload.get("downbeats") or [])
    return {"beats": beats, "downbeats": downbeats, "duration": [_probe_duration(audio)]}
