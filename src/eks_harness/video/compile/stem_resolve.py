"""Demucs HTDemucs stem separation + DeepFilterNet denoise resolution.

Both transforms are heavy ML pre-passes that the audio renderer needs to
apply *before* the per-segment ffmpeg chain runs (they replace the input
file path). Each is cached on disk by a content-derived key so repeated
renders of the same project skip the expensive inference.

The imports of :mod:`demucs` and :mod:`df` are deferred to the point of
use so the base base install stays lightweight; missing extras
surface as ``RuntimeError`` with the exact ``pip install`` hint.

Cache layout under ``<workspace>``::

    workspace/
      stems/<sha1(source, mtime, model)>/{vocals,drums,bass,other}.wav
      stems/<sha1(...)>/mix_<dash-joined-stems>.wav   # multi-stem mix
      denoise/<sha1(source, mtime, params)>.wav
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eks_harness.video.ir.audio_effects import DeepFilterDenoise
    from eks_harness.video.ir.media import SeparatedStem

__all__ = ["apply_denoise", "resolve_stem_path"]

_LOG = logging.getLogger(__name__)

_STEM_NAMES = ("vocals", "drums", "bass", "other")


def _hash_source(path: Path, *parts: str) -> str:
    """Compute a 16-hex-char digest over the source file identity + extra parts.

    The mtime nanoseconds are included so a re-encoded source invalidates
    the cache without requiring a content hash on potentially huge files.
    """

    h = hashlib.sha1()
    h.update(str(path.resolve()).encode("utf-8"))
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    h.update(str(mtime_ns).encode("ascii"))
    for part in parts:
        h.update(b"\x00")
        h.update(part.encode("utf-8"))
    return h.hexdigest()[:16]


def resolve_stem_path(source: SeparatedStem, *, cache_dir: Path) -> Path:
    """Resolve a :class:`SeparatedStem` to a WAV path on disk.

    Runs Demucs once per ``(source path, model)`` pair, caches the four
    stems under ``cache_dir/stems/<key>/``, and returns either the single
    cached stem (when ``len(source.stems) == 1``) or a freshly-mixed WAV
    combining the requested stems via ffmpeg ``amix``.
    """

    src_path = Path(source.source.path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(
            f"SeparatedStem source not found: {src_path}"
        )

    key = _hash_source(src_path, source.model)
    stem_dir = cache_dir / "stems" / key
    stem_paths = {name: stem_dir / f"{name}.wav" for name in _STEM_NAMES}

    if not all(p.exists() for p in stem_paths.values()):
        stem_dir.mkdir(parents=True, exist_ok=True)
        _run_demucs(src_path, source.model, stem_dir)
        missing = [name for name, p in stem_paths.items() if not p.exists()]
        if missing:
            raise RuntimeError(
                f"Demucs run did not produce expected stems {missing} "
                f"under {stem_dir}"
            )

    requested = list(source.stems)
    for s in requested:
        if s not in _STEM_NAMES:
            raise ValueError(f"unknown stem {s!r}; expected one of {_STEM_NAMES}")

    if len(requested) == 1:
        return stem_paths[requested[0]]

    mix_name = "mix_" + "-".join(sorted(requested)) + ".wav"
    mix_path = stem_dir / mix_name
    if mix_path.exists():
        return mix_path

    _mix_stems_with_ffmpeg([stem_paths[s] for s in requested], mix_path)
    return mix_path


def apply_denoise(
    media_path: Path,
    denoise: DeepFilterDenoise,
    *,
    cache_dir: Path,
) -> Path:
    """Pre-process ``media_path`` through DeepFilterNet, return cached WAV.

    Cached under ``cache_dir/denoise/<sha1(source, mtime, params)>.wav``.
    """

    src_path = Path(media_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"denoise input not found: {src_path}")

    params = f"atten={denoise.attenuation_limit_db:.3f}"
    key = _hash_source(src_path, "deepfilternet3", params)
    out_dir = cache_dir / "denoise"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{key}.wav"
    if out_path.exists():
        return out_path

    _run_deepfilternet(src_path, out_path, denoise)
    if not out_path.exists():
        raise RuntimeError(
            f"DeepFilterNet did not produce expected output at {out_path}"
        )
    return out_path


def _run_demucs(source: Path, model: str, out_dir: Path) -> None:
    """Invoke Demucs and copy the four stems to ``out_dir``.

    Uses ``demucs.api.Separator`` when available; falls back to invoking
    the ``demucs`` CLI as a subprocess.
    """

    try:
        from demucs.api import Separator, save_audio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on extra
        # Try CLI fallback
        if shutil.which("demucs") is None and shutil.which("python") is None:
            raise RuntimeError(
                "SeparatedStem requires `pip install eks-harness[stems]`"
            ) from exc
        _run_demucs_cli(source, model, out_dir)
        return

    _LOG.info(
        "running demucs (%s) on %s -> %s", model, source.name, out_dir
    )
    separator = Separator(model=model)
    _origin, stems = separator.separate_audio_file(str(source))
    # stems is a dict-like mapping {name: Tensor} for the four stems.
    for name, tensor in stems.items():
        if name not in _STEM_NAMES:
            continue
        save_audio(tensor, str(out_dir / f"{name}.wav"), separator.samplerate)


def _run_demucs_cli(source: Path, model: str, out_dir: Path) -> None:
    """Fallback: invoke the demucs CLI and move results into ``out_dir``."""

    with tempfile.TemporaryDirectory(prefix="demucs_") as tmp_root:
        cmd = [
            "demucs",
            "-n", model,
            "-o", tmp_root,
            "--filename", "{stem}.{ext}",
            str(source),
        ]
        _LOG.info("running demucs cli: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
        # Demucs writes to <tmp_root>/<model>/<source_stem>/<stem>.wav
        produced_root = Path(tmp_root) / model
        if not produced_root.exists():
            raise RuntimeError(
                f"demucs cli did not produce output under {produced_root}"
            )
        # Find the subdir matching this source.
        candidates = list(produced_root.iterdir())
        if not candidates:
            raise RuntimeError(
                f"demucs cli produced no output dirs under {produced_root}"
            )
        src_subdir = candidates[0]
        for name in _STEM_NAMES:
            produced = src_subdir / f"{name}.wav"
            if produced.exists():
                shutil.copy2(produced, out_dir / f"{name}.wav")


def _mix_stems_with_ffmpeg(stem_paths: list[Path], out_path: Path) -> None:
    """Combine multiple stem WAVs via ffmpeg ``amix=duration=longest``."""

    if len(stem_paths) < 2:
        raise ValueError("_mix_stems_with_ffmpeg requires at least 2 inputs")

    args: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in stem_paths:
        args.extend(["-i", str(p)])
    filter_complex = (
        "".join(f"[{i}:a]" for i in range(len(stem_paths)))
        + f"amix=inputs={len(stem_paths)}:duration=longest:normalize=0[aout]"
    )
    args.extend([
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "pcm_s16le",
        str(out_path),
    ])
    _LOG.info("mixing %d stems -> %s", len(stem_paths), out_path)
    subprocess.run(args, check=True)


def _run_deepfilternet(
    source: Path, out_path: Path, denoise: DeepFilterDenoise
) -> None:
    """Run DeepFilterNet 3 over ``source`` and write a WAV to ``out_path``."""

    try:
        from df.enhance import enhance, init_df, load_audio, save_audio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on extra
        raise RuntimeError(
            "DeepFilterDenoise requires `pip install eks-harness[denoise]`"
        ) from exc

    _LOG.info("running deepfilternet on %s -> %s", source.name, out_path)
    model, df_state, _ = init_df()
    audio, _ = load_audio(str(source), sr=df_state.sr())
    enhanced = enhance(
        model,
        df_state,
        audio,
        atten_lim_db=float(denoise.attenuation_limit_db),
    )
    save_audio(str(out_path), enhanced, df_state.sr())
