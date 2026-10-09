from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "eks-harness"

HOME_ENV = "EKS_HARNESS_HOME"
DIR_ENVS = {
    "config": "EKS_HARNESS_CONFIG_DIR",
    "data": "EKS_HARNESS_DATA_DIR",
    "cache": "EKS_HARNESS_CACHE_DIR",
    "state": "EKS_HARNESS_STATE_DIR",
    "log": "EKS_HARNESS_LOG_DIR",
}


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path
    log_dir: Path
    isolated: bool = False

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"

    @property
    def backends_dir(self) -> Path:
        return self.config_dir / "backends"

    @property
    def service_record(self) -> Path:
        return self.config_dir / "service.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "harness.db"

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "store"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def browser_dir(self) -> Path:
        return self.cache_dir / "browser"

    @property
    def avd_dir(self) -> Path:
        return self.cache_dir / "avd"

    @property
    def daemon_info_file(self) -> Path:
        return self.state_dir / "daemon.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "serve.lock"

    @property
    def pools_state_file(self) -> Path:
        return self.state_dir / "pools.json"

    @property
    def daemon_log(self) -> Path:
        return self.log_dir / "daemon.log"

    def all_dirs(self) -> list[Path]:
        return [self.config_dir, self.data_dir, self.cache_dir, self.state_dir, self.log_dir]

    def ensure(self) -> "Paths":
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
            private_dir(directory)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        return self


def private_dir(directory: Path) -> None:
    if platform.system() == "Windows":
        return
    try:
        if directory.stat().st_mode & 0o077:
            directory.chmod(0o700)
    except OSError:
        pass


def _env_path(env: dict, name: str) -> Path | None:
    value = env.get(name)
    return Path(value).expanduser() if value else None


def resolve_paths(env: dict | None = None) -> Paths:
    env = dict(os.environ if env is None else env)
    home = _env_path(env, HOME_ENV)
    if home is not None:
        defaults = {
            "config": home / "config",
            "data": home / "data",
            "cache": home / "cache",
            "state": home / "state",
            "log": home / "log",
        }
    else:
        defaults = {
            "config": Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True)),
            "data": Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)),
            "cache": Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False)),
            "state": Path(platformdirs.user_state_dir(APP_NAME, appauthor=False)),
            "log": Path(platformdirs.user_log_dir(APP_NAME, appauthor=False)),
        }
    resolved = {key: _env_path(env, DIR_ENVS[key]) or value for key, value in defaults.items()}
    return Paths(
        config_dir=resolved["config"],
        data_dir=resolved["data"],
        cache_dir=resolved["cache"],
        state_dir=resolved["state"],
        log_dir=resolved["log"],
        isolated=home is not None,
    )
