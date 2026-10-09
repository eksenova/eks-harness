"""Central hardware-acceleration detection + ffmpeg argument helpers.

Every direct ffmpeg invocation in the render pipeline routes its encode
and decode flags through this module so video work runs on the GPU where
the host supports it. Detection is probed once (``ffmpeg -encoders`` /
``-hwaccels``) and cached for the process.

Encoder preference (first whose ffmpeg codec is actually present wins):
AMD AMF → NVIDIA NVENC → Intel QSV → Apple VideoToolbox → libx264 (sw).

Decode uses ``-hwaccel auto``, which keeps decoded frames in system memory
so CPU filtergraphs (crop, gblur, overlay, …) still work - ffmpeg silently
falls back to software decode when the HW path can't feed the graph.

Disable everything (force software libx264, no hwaccel) with the env var
``EKS_HARNESS_HWACCEL=0`` - useful for reproducible cross-machine output or to
sidestep a flaky driver.
"""

from __future__ import annotations

import functools
import os
import subprocess

__all__ = [
    "decode_args",
    "encode_args",
    "hardware_encoder_available",
    "video_encoder_codec",
]


def _disabled() -> bool:
    return os.environ.get("EKS_HARNESS_HWACCEL", "1").strip().lower() in {"0", "false", "no"}


@functools.lru_cache(maxsize=1)
def _ffmpeg_encoders() -> str:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=False, capture_output=True, text=True, timeout=8,
        )
        return out.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=1)
def _ffmpeg_hwaccels() -> str:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            check=False, capture_output=True, text=True, timeout=8,
        )
        return out.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


# (codec family, ffmpeg encoder name) in descending preference.
_H264_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("amf", "h264_amf"),
    ("nvenc", "h264_nvenc"),
    ("qsv", "h264_qsv"),
    ("videotoolbox", "h264_videotoolbox"),
)
_HEVC_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("amf", "hevc_amf"),
    ("nvenc", "hevc_nvenc"),
    ("qsv", "hevc_qsv"),
    ("videotoolbox", "hevc_videotoolbox"),
)
_AV1_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("amf", "av1_amf"),
    ("nvenc", "av1_nvenc"),
    ("qsv", "av1_qsv"),
)


@functools.lru_cache(maxsize=8)
def video_encoder_codec(family: str = "h264") -> str:
    """Return the best available ffmpeg encoder name for ``family``.

    Falls back to the libx264 / software encoder when no HW encoder is
    present or when acceleration is disabled.
    """

    sw_fallback = {"h264": "libx264", "hevc": "libx265", "av1": "libaom-av1"}.get(
        family, "libx264"
    )
    if _disabled():
        return sw_fallback
    candidates = {
        "h264": _H264_CANDIDATES,
        "hevc": _HEVC_CANDIDATES,
        "av1": _AV1_CANDIDATES,
    }.get(family, _H264_CANDIDATES)
    listing = _ffmpeg_encoders()
    for _backend, codec in candidates:
        # Match the encoder token at a word boundary in `ffmpeg -encoders`.
        if f" {codec}" in listing or listing.find(codec) != -1:
            return codec
    return sw_fallback


def hardware_encoder_available(family: str = "h264") -> bool:
    return video_encoder_codec(family) not in {"libx264", "libx265", "libaom-av1"}


def encode_args(
    *,
    family: str = "h264",
    crf: int | None = None,
    bitrate_kbps: int | None = None,
) -> list[str]:
    """``-c:v`` + quality args for the best available encoder.

    ``crf`` is the software-encoder quality target (lower = better); it maps
    to the HW encoders' constant-quantizer (``qp``) controls which use a
    comparable 0–51 scale. ``bitrate_kbps`` forces CBR/VBR at that bitrate
    on any encoder. The caller still sets ``-pix_fmt`` separately.
    """

    codec = video_encoder_codec(family)
    quant = crf if crf is not None else 19

    if codec in {"h264_amf", "hevc_amf", "av1_amf"}:
        args = ["-c:v", codec, "-quality", "quality"]
        if bitrate_kbps is not None:
            args += ["-rc", "vbr_latency", "-b:v", f"{bitrate_kbps}k"]
        else:
            args += ["-rc", "cqp", "-qp_i", str(quant), "-qp_p", str(quant)]
        return args

    if codec in {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}:
        args = ["-c:v", codec, "-preset", "p5", "-tune", "hq"]
        if bitrate_kbps is not None:
            args += ["-b:v", f"{bitrate_kbps}k"]
        else:
            args += ["-rc", "vbr", "-cq", str(quant)]
        return args

    if codec in {"h264_qsv", "hevc_qsv", "av1_qsv"}:
        args = ["-c:v", codec]
        if bitrate_kbps is not None:
            args += ["-b:v", f"{bitrate_kbps}k"]
        else:
            args += ["-global_quality", str(quant)]
        return args

    if codec in {"h264_videotoolbox", "hevc_videotoolbox"}:
        args = ["-c:v", codec]
        if bitrate_kbps is not None:
            args += ["-b:v", f"{bitrate_kbps}k"]
        else:
            # VideoToolbox has no CRF; approximate with a quality fraction.
            args += ["-q:v", str(max(1, min(100, 100 - quant * 2)))]
        return args

    # Software fallback (libx264 / libx265 / libaom-av1).
    args = ["-c:v", codec, "-preset", "medium", "-crf", str(quant)]
    if bitrate_kbps is not None:
        args += ["-b:v", f"{bitrate_kbps}k"]
    return args


def decode_args() -> list[str]:
    """Input-side ``-hwaccel`` flags for decoding a video file.

    Use BEFORE ``-i <video>``. Returns ``["-hwaccel", "auto"]`` when any HW
    decode method is present; ``auto`` downloads frames to system memory so
    downstream CPU filters keep working. Empty when disabled or unavailable.
    Do NOT use for lavfi / image / rawvideo-pipe inputs (no benefit, and it
    can break the graph).
    """

    if _disabled():
        return []
    hwaccels = _ffmpeg_hwaccels()
    for method in ("d3d11va", "cuda", "vaapi", "videotoolbox", "qsv", "dxva2"):
        if method in hwaccels:
            return ["-hwaccel", "auto"]
    return []
