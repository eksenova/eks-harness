from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from eks_harness.paths import Paths, resolve_paths

API_KEY_ENV = "EKS_HARNESS_API_KEY"


@dataclass
class Credentials:
    api_key: str
    url: str | None = None
    username: str | None = None
    saved_at: float | None = None

    @property
    def prefix(self) -> str | None:
        parts = self.api_key.split("_")
        return parts[1] if len(parts) == 3 and parts[0] == "ehk" else None


def credentials_file(paths: Paths | None = None) -> Path:
    return (paths or resolve_paths()).credentials_file


def load(paths: Paths | None = None) -> Credentials | None:
    path = credentials_file(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("apiKey"):
        return None
    return Credentials(api_key=str(data["apiKey"]), url=data.get("url"), username=data.get("username"),
                       saved_at=data.get("savedAt"))


def save(credentials: Credentials, paths: Paths | None = None) -> Path:
    path = credentials_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    credentials.saved_at = credentials.saved_at or time.time()
    payload = {"apiKey": credentials.api_key, "url": credentials.url, "username": credentials.username,
               "savedAt": credentials.saved_at}
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def clear(paths: Paths | None = None) -> bool:
    path = credentials_file(paths)
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_api_key(paths: Paths | None = None) -> tuple[str | None, str]:
    env_key = os.environ.get(API_KEY_ENV, "").strip()
    if env_key:
        return env_key, "env"
    stored = load(paths)
    if stored:
        return stored.api_key, "file"
    return None, "none"
