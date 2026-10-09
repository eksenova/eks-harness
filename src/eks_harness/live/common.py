from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_EDGE = 1280


class LiveError(RuntimeError):
    pass


class LiveUnavailable(LiveError):
    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


@dataclass(frozen=True)
class StreamSettings:
    quality: int = 70
    max_fps: int = 10
    max_edge: int = MAX_EDGE

    @classmethod
    def from_config(cls, config) -> "StreamSettings":
        return cls(quality=int(config["live.jpegQuality"]), max_fps=int(config["live.maxFps"]))

    @property
    def ffmpeg_qscale(self) -> int:
        quality = min(100, max(10, self.quality))
        return max(2, min(31, round(31 - (quality - 10) * 29 / 90)))


def ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def xcrun_binary() -> str | None:
    if platform.system() != "Darwin":
        return None
    return shutil.which("xcrun")


def android_sdk() -> Path | None:
    candidates = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                  str(Path.home() / "Library" / "Android" / "sdk"), str(Path.home() / "Android" / "Sdk")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Android" / "Sdk"))
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def adb_binary() -> str | None:
    sdk = android_sdk()
    if sdk is not None:
        name = "adb.exe" if platform.system() == "Windows" else "adb"
        path = sdk / "platform-tools" / name
        if path.exists():
            return str(path)
    return shutil.which("adb")
