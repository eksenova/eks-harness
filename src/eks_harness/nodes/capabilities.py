from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PROBE_TIMEOUT = 20


def _run(args: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def memory_bytes() -> int:
    try:
        if platform.system() == "Darwin":
            return int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
        pages = os.sysconf("SC_PHYS_PAGES")
        return int(pages * os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return 0


WSL_NVIDIA_SMI = "/usr/lib/wsl/lib/nvidia-smi"


def nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    return WSL_NVIDIA_SMI if Path(WSL_NVIDIA_SMI).is_file() else None


def nvidia_gpus() -> list[dict[str, Any]]:
    binary = nvidia_smi()
    if not binary:
        return []
    text = _run([binary, "--query-gpu=index,name,memory.total,uuid,driver_version",
                 "--format=csv,noheader,nounits"])
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        gpus.append({"index": int(parts[0]), "name": parts[1], "memoryMb": int(float(parts[2] or 0)),
                     "uuid": parts[3], "driver": parts[4], "vendor": "nvidia", "backends": ["CUDA", "OPTIX"]})
    return gpus


def apple_gpus() -> list[dict[str, Any]]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return []
    brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or "Apple silicon"
    return [{"index": 0, "name": brand, "vendor": "apple", "backends": ["METAL"], "memoryMb": memory_bytes() // 2**20}]


def blender_info(explicit: str | None = None) -> dict[str, Any] | None:
    candidates = [explicit, os.environ.get("EKS_HARNESS_BLENDER"), shutil.which("blender"),
                  "/Applications/Blender.app/Contents/MacOS/Blender"]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = str(Path(candidate).expanduser())
        if not Path(candidate).exists():
            continue
        text = _run([candidate, "--version"], timeout=60)
        match = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", text)
        if match:
            return {"path": candidate, "version": match.group(1)}
    return None


def chrome_path() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.exists() else None


def probe(extra: Mapping[str, Callable[[], dict[str, Any] | None]] | None = None,
          blender: str | None = None) -> dict[str, Any]:
    total, _, free = shutil.disk_usage(Path.home())
    gpus = nvidia_gpus() or apple_gpus()
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "adb", "emulator", "docker", "node", "uv",
                                                   "xcrun", "git")}
    caps: dict[str, Any] = {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "hostname": platform.node(),
        "python": platform.python_version(),
        "cpus": os.cpu_count() or 1,
        "memoryMb": memory_bytes() // 2**20,
        "diskFreeGb": round(free / 2**30, 1),
        "diskTotalGb": round(total / 2**30, 1),
        "gpus": gpus,
        "tools": {k: v for k, v in tools.items() if v},
        "wsl": "microsoft" in platform.release().lower(),
    }
    found_blender = blender_info(blender)
    if found_blender:
        caps["blender"] = found_blender
    chrome = chrome_path()
    if chrome:
        caps["chrome"] = chrome
    plugins: dict[str, Any] = {}
    for name, fn in (extra or {}).items():
        try:
            value = fn()
        except Exception as error:
            value = {"error": str(error)}
        if value is not None:
            plugins[name] = value
    if plugins:
        caps["plugins"] = plugins
    return caps


def names(caps: Mapping[str, Any]) -> set[str]:
    found = {caps.get("os", ""), caps.get("arch", "")}
    found |= set((caps.get("tools") or {}).keys())
    found |= set((caps.get("plugins") or {}).keys())
    if caps.get("blender"):
        found.add("blender")
    if caps.get("chrome"):
        found.add("chrome")
    for gpu in caps.get("gpus") or []:
        found.add("gpu")
        found |= {b.lower() for b in gpu.get("backends") or []}
    found.discard("")
    return found


def default_slots(caps: Mapping[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for gpu in caps.get("gpus") or []:
        backend = (gpu.get("backends") or ["CPU"])[0]
        env: dict[str, str] = {}
        if gpu.get("vendor") == "nvidia":
            env["CUDA_VISIBLE_DEVICES"] = str(gpu["index"])
            backend = "OPTIX" if "OPTIX" in (gpu.get("backends") or []) else backend
        slots.append({"id": f"gpu{gpu['index']}", "gpu": gpu.get("name"), "backend": backend, "workers": 1,
                      "env": env, "tags": ["gpu", backend.lower()]})
    slots.append({"id": "cpu", "workers": max(1, int(caps.get("cpus") or 2) // 4), "env": {}, "tags": ["cpu"]})
    return slots


def satisfies(caps: Mapping[str, Any], slot: Mapping[str, Any], requirements: Mapping[str, Any]) -> bool:
    available = names(caps) | set(slot.get("tags") or [])
    for need in requirements.get("capabilities") or []:
        if need not in available:
            return False
    if requirements.get("gpu") and "gpu" not in (slot.get("tags") or []):
        return False
    if requirements.get("gpu") is False and "gpu" in (slot.get("tags") or []):
        return False
    backend = requirements.get("backend")
    if backend and str(slot.get("backend", "")).upper() != str(backend).upper():
        return False
    if requirements.get("os") and caps.get("os") != requirements["os"]:
        return False
    if requirements.get("slot") and slot.get("id") != requirements["slot"]:
        return False
    return True
