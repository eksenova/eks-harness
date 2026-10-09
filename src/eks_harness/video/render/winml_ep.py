"""AMD GPU (MIGraphX) acceleration for ONNX Runtime via Windows ML EPs.

On this AMD Radeon iGPU + Ryzen-AI box the best ONNX GPU path is the
**MIGraphX** execution provider (ROCm-based). Unlike the DirectML EP - which
OOMs the big BiRefNet model on shared-VRAM APUs - MIGraphX runs FP32 ONNX on
the AMD GPU efficiently.

Getting there from an *unpackaged* Python process took some doing, and the
approach here is deliberately **not** the one Microsoft's quick-start shows:

* The Windows ML ``ExecutionProviderCatalog`` API (``GetDefault()`` /
  ``EnsureAndRegisterCertifiedAsync()``) is **identity-gated** - it fails with
  ``APPMODEL_ERROR_NO_PACKAGE`` (0x80073D54) in a plain ``python.exe`` because
  unpackaged processes have no package identity. Microsoft's own Python sample
  for that API literally says *"DO NOT use this API. It won't register EPs to
  the python ort env."* So the catalog path is a dead end here.
* Instead we use the documented **"bring your own EP"** path: the AMD GPU EP is
  already on disk as the system Appx package
  ``MicrosoftCorporationII.WinML.AMD.GPU.EP`` (installed/updated by Windows),
  and we register its ``migraphx-ep.dll`` **directly** with ONNX Runtime via
  :func:`onnxruntime.register_execution_provider_library` - no catalog, no
  identity.
* The EP is a **plugin EP** (ORT plugin-EP ABI v24, needs onnxruntime >= 1.24).
  Plugin EPs do **not** show up in the legacy ``get_available_providers()`` /
  ``providers=[...]`` surface; they are discovered via ``get_ep_devices()`` and
  selected per-session via ``SessionOptions.add_provider_for_devices()``.

Because rembg, optimum and friends build their own ``InferenceSession`` through
the legacy ``providers=[...]`` API (which can't name a plugin EP), the only way
to route them onto the GPU is :func:`install_autoep_session_hook`, which wraps
``onnxruntime.InferenceSession`` so every session built afterwards prefers the
MIGraphX device - explicitly (never ``PREFER_GPU``, which could pick the
OOM-prone DirectML device on this box) - and cleanly falls back to CPU if a GPU
session can't be built.

Everything degrades to CPU when acceleration is disabled
(``EKS_HARNESS_HWACCEL=0``), the EP package isn't installed, the runtime is too old
(< 1.24), or registration fails. All probes are cached for the process.
"""

from __future__ import annotations

import functools
import logging
import os
import subprocess
from typing import Any

_LOG = logging.getLogger(__name__)

__all__ = [
    "onnx_providers",
    "rembg_providers",
    "get_migraphx_device",
    "configure_gpu_session_options",
    "install_autoep_session_hook",
]

_AMD_GPU_EP_FAMILY = "MicrosoftCorporationII.WinML.AMD.GPU.EP"
_EP_REGISTRATION_NAME = "MIGraphXExecutionProvider"
_EP_DLL_NAME = "migraphx-ep.dll"

# MIGraphX JIT-compiles a graph on first inference (the result is cached to
# disk and reused across processes). For small/medium models that one-time cost
# is seconds and steady-state inference beats CPU. For very large models (e.g.
# the ~970MB BiRefNet matting graph) the first compile is pathological - tens of
# minutes and many GB of RAM on a shared-memory iGPU - so by default we keep
# models above this size on CPU. Override with ``EKS_HARNESS_WINML_LARGE=1`` (or
# raise ``EKS_HARNESS_WINML_MAX_MB``) to force the GPU and pay the one-time compile.
_DEFAULT_MAX_MODEL_MB = 500.0


def _disabled() -> bool:
    return os.environ.get("EKS_HARNESS_HWACCEL", "1").strip().lower() in {"0", "false", "no"}


def _max_model_mb() -> float:
    if os.environ.get("EKS_HARNESS_WINML_LARGE", "").strip().lower() in {"1", "true", "yes"}:
        return float("inf")
    raw = os.environ.get("EKS_HARNESS_WINML_MAX_MB", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_MAX_MODEL_MB


def _model_size_mb(args: tuple[Any, ...], kwargs: dict[str, Any]) -> float | None:
    """Best-effort size (MB) of the model an ``InferenceSession`` is being built for.

    Returns None when the size can't be determined (in which case the caller
    should *allow* the GPU - unknown size is treated as small).
    """

    model = args[0] if args else kwargs.get("path_or_bytes")
    if isinstance(model, (bytes, bytearray)):
        return len(model) / 1e6
    if isinstance(model, (str, os.PathLike)):
        try:
            return os.path.getsize(model) / 1e6
        except OSError:
            return None
    return None


@functools.lru_cache(maxsize=1)
def _find_amd_gpu_ep_dll() -> str | None:
    """Resolve the on-disk path of the AMD GPU EP's ``migraphx-ep.dll``.

    The EP ships as a system Appx package under ``C:\\Program
    Files\\WindowsApps`` - a directory whose listing is ACL-restricted, so a
    plain ``glob`` returns nothing. We ask the package manager for the install
    location instead (cheap, cached, once per process).
    """

    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-AppxPackage | Where-Object { $_.Name -like "
                f"'{_AMD_GPU_EP_FAMILY}*' }} | Sort-Object Version -Descending | "
                "Select-Object -First 1).InstallLocation",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        _LOG.debug("AMD GPU EP discovery (Get-AppxPackage) failed", exc_info=True)
        return None

    location = completed.stdout.strip()
    if not location:
        return None
    dll = os.path.join(location, "ExecutionProvider", _EP_DLL_NAME)
    return dll if os.path.isfile(dll) else None


@functools.lru_cache(maxsize=1)
def get_migraphx_device() -> Any | None:
    """Register the AMD MIGraphX plugin EP and return its ``OrtEpDevice``.

    Returns the MIGraphX :class:`onnxruntime.OrtEpDevice` on success, or
    ``None`` when acceleration is disabled, the EP package is missing, the
    runtime is too old, or registration fails. Cached for the process - the EP
    library is registered exactly once.
    """

    if _disabled():
        return None
    dll = _find_amd_gpu_ep_dll()
    if dll is None:
        _LOG.info(
            "AMD GPU EP package (%s) not found; ONNX workloads will run on CPU.",
            _AMD_GPU_EP_FAMILY,
        )
        return None

    try:
        import onnxruntime as ort
    except Exception:
        return None

    if not hasattr(ort, "register_execution_provider_library") or not hasattr(
        ort, "get_ep_devices"
    ):
        _LOG.info(
            "onnxruntime %s lacks the plugin-EP API (needs >= 1.24); "
            "ONNX workloads will run on CPU.",
            getattr(ort, "__version__", "?"),
        )
        return None

    ep_dir = os.path.dirname(dll)
    try:
        # The EP DLL's sibling ROCm/MIGraphX/HIP DLLs must be on the loader path.
        os.add_dll_directory(ep_dir)
        os.environ["PATH"] = ep_dir + os.pathsep + os.environ.get("PATH", "")
        ort.register_execution_provider_library(_EP_REGISTRATION_NAME, dll)
        device = next(
            (d for d in ort.get_ep_devices() if d.ep_name == _EP_REGISTRATION_NAME),
            None,
        )
        if device is None:
            _LOG.info("MIGraphX EP registered but no device discovered; using CPU.")
            return None
        _LOG.info("MIGraphX (AMD GPU) execution provider registered for ONNX workloads")
        return device
    except Exception:
        _LOG.info(
            "MIGraphX EP registration failed; ONNX workloads will run on CPU.",
            exc_info=True,
        )
        return None


def configure_gpu_session_options(session_options: Any) -> bool:
    """Pin ``session_options`` to the MIGraphX (AMD GPU) device, if available.

    Returns True when the GPU device was added (CPU is kept as automatic
    fallback by ORT), False when no GPU EP is available or the call fails.
    """

    device = get_migraphx_device()
    if device is None:
        return False
    try:
        session_options.add_provider_for_devices([device], {})
        return True
    except Exception:
        _LOG.debug("add_provider_for_devices(MIGraphX) failed", exc_info=True)
        return False


_HOOK_INSTALLED = False


def install_autoep_session_hook() -> bool:
    """Route every subsequently-created ONNX session onto the AMD GPU.

    rembg/optimum/etc. construct their own ``InferenceSession`` via the legacy
    ``providers=[...]`` surface, which cannot name a plugin EP. This wraps
    ``onnxruntime.InferenceSession`` so each new session first *tries* the
    MIGraphX device (explicit selection - never ``PREFER_GPU``, to avoid the
    OOM-prone DirectML device), and transparently falls back to the caller's
    original (CPU) configuration if a GPU session can't be built.

    Idempotent. Returns True if the hook is active (GPU available), else False.
    """

    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return True
    if get_migraphx_device() is None:
        return False

    import onnxruntime as ort

    if getattr(ort.InferenceSession, "_eks_autoep", False):
        _HOOK_INSTALLED = True
        return True

    original_cls = ort.InferenceSession

    def _gpu_session_options() -> Any | None:
        device = get_migraphx_device()
        if device is None:
            return None
        try:
            so = ort.SessionOptions()
            so.add_provider_for_devices([device], {})
            return so
        except Exception:
            return None

    class _AutoEPInferenceSession(original_cls):  # type: ignore[misc, valid-type]
        """``InferenceSession`` that prefers the MIGraphX device, CPU on failure."""

        _eks_autoep = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            size_mb = _model_size_mb(args, kwargs)
            limit = _max_model_mb()
            if size_mb is not None and size_mb > limit:
                _LOG.info(
                    "ONNX model is %.0fMB > %.0fMB cap; keeping on CPU (MIGraphX's "
                    "one-time compile is impractical for graphs this large on this "
                    "GPU). Set EKS_HARNESS_WINML_LARGE=1 to force the GPU.",
                    size_mb,
                    limit,
                )
                super().__init__(*args, **kwargs)
                return
            gpu_so = _gpu_session_options()
            if gpu_so is not None:
                gpu_kwargs = dict(kwargs)
                gpu_kwargs["sess_options"] = gpu_so
                gpu_kwargs.pop("providers", None)
                gpu_kwargs.pop("provider_options", None)
                try:
                    super().__init__(*args, **gpu_kwargs)
                    return
                except Exception:
                    _LOG.warning(
                        "MIGraphX session creation failed; falling back to CPU.",
                        exc_info=True,
                    )
            super().__init__(*args, **kwargs)

    ort.InferenceSession = _AutoEPInferenceSession  # type: ignore[misc]
    _HOOK_INSTALLED = True
    _LOG.info("ONNX autoEP hook installed - sessions will prefer the AMD GPU (MIGraphX)")
    return True


@functools.lru_cache(maxsize=1)
def onnx_providers() -> tuple[str, ...]:
    """Legacy provider list for callers that still pass ``providers=[...]``.

    GPU acceleration for plugin EPs (MIGraphX) is delivered through
    :func:`install_autoep_session_hook`, **not** through this list - a plugin
    EP cannot be named in the legacy ``providers`` surface. This therefore
    always returns CPU-valid strings (so optimum's provider validation passes);
    the hook upgrades the resulting session to the GPU. Installs the hook as a
    side effect so callers that only read this list still get acceleration.
    """

    install_autoep_session_hook()
    return ("CPUExecutionProvider",)


def rembg_providers() -> tuple[str, ...]:
    """Backwards-compatible alias for :func:`onnx_providers`."""

    return onnx_providers()
