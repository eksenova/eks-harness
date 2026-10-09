"""``eks-harness video doctor`` - environment & dependency diagnostic report.

Probes ffmpeg / ffprobe, the encoders that ffmpeg actually exposes,
required Python dependencies, and the (lazy) optional ML/media stacks
that power markers, captioners, and media providers.

Exit code is ``0`` iff the always-required render path works
(``ffmpeg`` + the ``libx264`` software encoder); anything else is ``1``.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["run_doctor"]


_REQUIRED_ENCODERS: tuple[str, ...] = ("libx264",)
_PROBED_ENCODERS: tuple[str, ...] = (
    "libx264",
    "h264_nvenc",
    "h264_qsv",
    "h264_videotoolbox",
    "libsvtav1",
)
_REQUIRED_DEPS: tuple[str, ...] = ("numpy", "pydantic", "pluggy", "asteval", "av")
_OPTIONAL_DEPS: tuple[str, ...] = (
    "librosa",
    "madmom",
    "BeatNet",
    "faster_whisper",
    "whisperx",
    "mlx_whisper",
    "mediapipe",
    "cv2",
    "rembg",
    "scipy",
    "PIL",
    "yt_dlp",
)


def _blender_row() -> _Row:
    from eks_harness.video.plugins.builtin.media.blender import runner

    try:
        binary = runner.find_blender()
        return _Row("blender", "OK", f"{runner.blender_version(binary)} ({binary})")
    except RuntimeError:
        return _Row("blender", "OPTIONAL", "not installed; only needed by projects that enable the blender plugin")


@dataclass(frozen=True)
class _Row:
    name: str
    status: str
    detail: str


def run_doctor(ffmpeg_binary: str = "ffmpeg") -> int:
    rows: list[tuple[str, list[_Row]]] = []

    ffmpeg_info = _probe_binary(ffmpeg_binary)
    ffprobe_info = _probe_binary(_sibling_binary(ffmpeg_binary, "ffprobe"))
    rows.append(
        (
            "Binaries",
            [
                _binary_row("ffmpeg", ffmpeg_info),
                _binary_row("ffprobe", ffprobe_info),
            ],
        )
    )

    encoder_rows, encoders_ok = _check_encoders(ffmpeg_info.path)
    rows.append(("Encoders", encoder_rows))

    rows.append(("Required Python deps", [_dep_row(name) for name in _REQUIRED_DEPS]))
    rows.append(("Optional deps", [_dep_row(name) for name in _OPTIONAL_DEPS]))
    rows.append(("Optional binaries", [_blender_row()]))

    _render(rows)

    if ffmpeg_info.path is None:
        return 1
    return 0 if encoders_ok else 1


@dataclass(frozen=True)
class _BinaryInfo:
    path: str | None
    version_line: str | None


def _probe_binary(binary: str) -> _BinaryInfo:
    resolved = shutil.which(binary)
    if resolved is None:
        return _BinaryInfo(path=None, version_line=None)
    try:
        completed = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _BinaryInfo(path=resolved, version_line=None)
    first_line = (completed.stdout or completed.stderr or "").splitlines()
    return _BinaryInfo(path=resolved, version_line=first_line[0] if first_line else None)


def _sibling_binary(reference: str, sibling: str) -> str:
    """If user passed an explicit ffmpeg path, look for ffprobe next to it."""
    if reference in ("ffmpeg", ""):
        return sibling
    from pathlib import Path

    parent = Path(reference).parent
    candidate = parent / sibling
    if candidate.exists():
        return str(candidate)
    return sibling


def _binary_row(label: str, info: _BinaryInfo) -> _Row:
    if info.path is None:
        return _Row(name=label, status="MISSING", detail="not found on PATH")
    detail = info.path
    if info.version_line:
        detail = f"{info.path}  ({info.version_line})"
    return _Row(name=label, status="OK", detail=detail)


def _check_encoders(ffmpeg_path: str | None) -> tuple[list[_Row], bool]:
    if ffmpeg_path is None:
        rows = [
            _Row(name=enc, status="UNKNOWN", detail="ffmpeg not available")
            for enc in _PROBED_ENCODERS
        ]
        return rows, False

    available = _list_ffmpeg_encoders(ffmpeg_path)
    rows: list[_Row] = []
    required_ok = True
    for enc in _PROBED_ENCODERS:
        present = enc in available
        status = "OK" if present else "MISSING"
        detail = "available" if present else "not built into this ffmpeg"
        rows.append(_Row(name=enc, status=status, detail=detail))
        if enc in _REQUIRED_ENCODERS and not present:
            required_ok = False
    return rows, required_ok


def _list_ffmpeg_encoders(ffmpeg_path: str) -> set[str]:
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    encoders: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        if flags and flags[0] in {"V", "A", "S", "."}:
            encoders.add(name)
    return encoders


def _dep_row(module_name: str) -> _Row:
    version, error = _probe_module(module_name)
    if error is not None:
        return _Row(name=module_name, status="MISSING", detail=error)
    return _Row(name=module_name, status="OK", detail=version or "(version unknown)")


def _probe_module(module_name: str) -> tuple[str | None, str | None]:
    importer: Callable[[str], object] = importlib.import_module
    try:
        module = importer(module_name)
    except ImportError as exc:
        return None, f"not installed ({exc.__class__.__name__})"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"failed to import: {exc.__class__.__name__}: {exc}"
    version = getattr(module, "__version__", None)
    if version is None and module_name == "PIL":
        try:
            version = importlib.import_module("PIL").__version__  # type: ignore[attr-defined]
        except Exception:
            version = None
    if version is None:
        version = _package_version(module_name)
    return (str(version) if version else None), None


def _package_version(module_name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        return pkg_version(module_name)
    except PackageNotFoundError:
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _render(groups: list[tuple[str, list[_Row]]]) -> None:
    all_rows = [row for _, rows in groups for row in rows]
    name_width = max((len(r.name) for r in all_rows), default=10)
    status_width = max((len(r.status) for r in all_rows), default=7)

    for index, (title, rows) in enumerate(groups):
        if index > 0:
            print("", file=sys.stdout)
        print(f"== {title} ==", file=sys.stdout)
        for row in rows:
            print(
                f"  {row.name.ljust(name_width)}  {row.status.ljust(status_width)}  {row.detail}",
                file=sys.stdout,
            )
