from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure

STEMS_EXTRA = "eks-harness[stems]"
DENOISE_EXTRA = "eks-harness[denoise]"
STEMS_MODULE = "demucs"
DENOISE_MODULE = "df"
DENOISE_MODEL = "deepfilternet3"


def installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def torch_device() -> str:
    torch: Any = importlib.import_module("torch")
    return "cuda" if bool(torch.cuda.is_available()) else "cpu"


def require_stems(what: str) -> None:
    if not installed(STEMS_MODULE):
        raise NodeFailure("resolution_unavailable",
                          f"{what} needs Demucs stem separation, which is not installed on this machine; install "
                          f"{STEMS_EXTRA}", detail={"extra": STEMS_EXTRA})


def require_denoise(what: str) -> None:
    if not installed(DENOISE_MODULE):
        raise NodeFailure("resolution_unavailable",
                          f"{what} needs DeepFilterNet noise reduction, which is not installed on this machine; install "
                          f"{DENOISE_EXTRA}", detail={"extra": DENOISE_EXTRA})


def stems_resolution(model: str) -> dict[str, JsonValue]:
    return {"device": torch_device(), "model": model, "weights": f"demucs:{model}",
            "backend_version": f"demucs {version('demucs')}; torch {version('torch')}"}


def denoise_resolution() -> dict[str, JsonValue]:
    return {"device": torch_device(), "model": DENOISE_MODEL, "weights": f"deepfilternet:{DENOISE_MODEL}",
            "backend_version": f"deepfilternet {version('deepfilternet')}; torch {version('torch')}"}


def separate(path: Path, stems: Sequence[str], model: str, directory: Path) -> Path:
    from eks_harness.video.compile.stem_resolve import resolve_stem_path
    from eks_harness.video.ir.media import AudioFile, SeparatedStem

    source = SeparatedStem.model_validate({"source": AudioFile(path=str(path)), "stems": list(stems), "model": model})
    try:
        return resolve_stem_path(source, cache_dir=directory)
    except (RuntimeError, OSError, ValueError) as error:
        raise NodeFailure("executor_error", f"Demucs could not separate {path.name}: {error}") from error


def denoise(path: Path, attenuation_limit_db: float, directory: Path) -> Path:
    from eks_harness.video.compile.stem_resolve import apply_denoise
    from eks_harness.video.ir.audio_effects import DeepFilterDenoise

    try:
        return apply_denoise(path, DeepFilterDenoise(attenuation_limit_db=attenuation_limit_db), cache_dir=directory)
    except (RuntimeError, OSError, ValueError) as error:
        raise NodeFailure("executor_error", f"DeepFilterNet could not denoise {path.name}: {error}") from error


__all__ = [
    "DENOISE_EXTRA",
    "STEMS_EXTRA",
    "denoise",
    "denoise_resolution",
    "installed",
    "require_denoise",
    "require_stems",
    "separate",
    "stems_resolution",
    "torch_device",
    "version",
]
